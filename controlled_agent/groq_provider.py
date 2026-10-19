"""Optional one-shot Groq proposal source; no execution or session authority.

Only trusted host code configures credentials and owns this context manager.
Size checks are post-buffering; transport timeouts are not a wall-clock deadline.
"""

from collections.abc import Mapping
import json
import logging
from math import isfinite
import os
import sys

from .provider import ProviderError, ProviderFailure, ProviderRequest
from .submission import MalformedProposal, parse_proposal


_MODEL = "openai/gpt-oss-20b"
_CONTRACT = (
    'Return exactly one JSON object, without prose or markdown: '
    '{"type":"proposal","proposal":{"tool_name":"NAME","arguments":{}}}, '
    '{"type":"no_action"}, or {"type":"refusal"}. '
    'Proposal arguments must be flat JSON scalars. Include no other keys. '
    'You only propose; you cannot approve, authorize, or execute actions. '
    'Public tool descriptions follow: '
)


def _invalid():
    raise ProviderError(ProviderFailure.INVALID_OUTPUT)


def _strict_json(text: str, limit: int):
    """Bound depth before parsing; validate Unicode and numbers at every level."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _invalid()
            result[key] = value
        return result

    def check(value):
        if type(value) is str:
            value.encode("utf-8", errors="strict")
        elif type(value) is float and not isfinite(value):
            _invalid()
        elif type(value) is dict:
            for key, item in value.items():
                check(key)
                check(item)
        elif type(value) is list:
            for item in value:
                check(item)

    try:
        if len(text.encode("utf-8", errors="strict")) > limit:
            _invalid()
        depth = 0
        quoted = escaped = False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > 16:
                    _invalid()
            elif char in "]}":
                depth -= 1
        value = json.loads(text, object_pairs_hook=pairs,
                           parse_constant=lambda _: _invalid())
        check(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ProviderError(ProviderFailure.INVALID_OUTPUT) from None


def _decode(raw: bytes) -> dict | None:
    try:
        if len(raw) > 65536:
            _invalid()
        response = _strict_json(raw.decode("utf-8", errors="strict"), 65536)
        if type(response) is not dict or "error" in response:
            _invalid()
        if response.get("object", "chat.completion") != "chat.completion":
            _invalid()
        if response.get("mcp_list_tools") not in (None, []):
            _invalid()
        choices = response.get("choices")
        if type(choices) is not list or len(choices) != 1:
            _invalid()
        choice = choices[0]
        if (type(choice) is not dict or type(choice.get("index")) is not int
                or choice["index"] != 0):
            _invalid()
        message = choice.get("message")
        if type(message) is not dict or message.get("role") != "assistant":
            _invalid()
        # These are API metadata, not the exact-key model-authored wire object.
        for key in ("tool_calls", "executed_tools"):
            if key in message and message[key] is not None and message[key] != []:
                _invalid()
        if message.get("function_call") is not None:
            _invalid()
        if message.get("annotations") not in (None, []):
            _invalid()
        if message.get("reasoning") not in (None, ""):
            _invalid()
        refusal = message.get("refusal")
        if refusal is not None and type(refusal) is not str:
            _invalid()
        finish = choice.get("finish_reason")
        if finish == "content_filter" or (type(refusal) is str and refusal.strip()):
            raise ProviderError(ProviderFailure.REFUSAL)
        if finish != "stop":
            _invalid()
        content = message.get("content")
        if type(content) is not str or not content.strip():
            _invalid()
        wire = _strict_json(content, 8192)
        if type(wire) is not dict:
            _invalid()
        if wire == {"type": "no_action"}:
            return None
        if wire == {"type": "refusal"}:
            raise ProviderError(ProviderFailure.REFUSAL)
        if set(wire) != {"type", "proposal"} or wire["type"] != "proposal":
            _invalid()
        tool, arguments = parse_proposal(wire["proposal"])
        return {"tool_name": tool, "arguments": arguments}
    except (UnicodeError, MalformedProposal):
        raise ProviderError(ProviderFailure.INVALID_OUTPUT) from None


def _public_data(value, depth=0):
    if depth > 16:
        raise ValueError("Invalid public metadata")
    if isinstance(value, Mapping):
        return {key: _public_data(item, depth + 1) for key, item in value.items()}
    if type(value) is tuple:
        return [_public_data(item, depth + 1) for item in value]
    return value


def _configuration_guard():
    # Presence only: never read ambient keys/header values. SDK import can enable
    # logging from GROQ_LOG, and construction can merge GROQ_CUSTOM_HEADERS.
    if any(name in os.environ for name in ("GROQ_LOG", "GROQ_CUSTOM_HEADERS")):
        raise ProviderError(ProviderFailure.UNAVAILABLE)
    # Refuse unsafe diagnostics rather than changing application logging. Host
    # logging configuration must stay fixed throughout a synchronous call.
    loggers = [logging.getLogger(name) for name in ("groq", "httpx", "httpcore")]
    while loggers:
        logger = loggers.pop()
        if logger.isEnabledFor(logging.DEBUG):
            raise ProviderError(ProviderFailure.UNAVAILABLE)
        loggers.extend(logger.getChildren())


class GroqProvider:
    """Own one SDK/client and transport. Injected transport is trusted test wiring.

    No environment key lookup, tools, dispatch, retry, fallback, or state reset.
    Close is terminal and idempotent, including after a failed cleanup attempt.
    """

    def __init__(self, *, api_key: str, transport=None):
        self._closed = True
        _configuration_guard()
        if type(api_key) is not str or not api_key.strip():
            raise ProviderError(ProviderFailure.UNAVAILABLE)
        http = None
        try:
            import groq
            import httpx

            self._sdk = groq
            timeout = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
            http = httpx.Client(
                transport=transport if transport is not None else httpx.HTTPTransport(
                    retries=0, verify=True, trust_env=False),
                timeout=timeout, follow_redirects=False, trust_env=False,
            )
            self._client = groq.Groq(api_key=api_key, base_url="https://api.groq.com",
                                     max_retries=0, timeout=timeout, http_client=http)
            self._closed = False
        except BaseException as error:
            if http is not None:
                try:
                    http.close()
                except BaseException:
                    pass  # Preserve the original failure or interruption.
            if not isinstance(error, Exception):
                raise
            raise ProviderError(ProviderFailure.UNAVAILABLE) from None

    def propose(self, request: ProviderRequest) -> dict | None:
        if type(request) is not ProviderRequest:
            raise TypeError("Expected public provider request")
        if self._closed:
            raise ProviderError(ProviderFailure.UNAVAILABLE)
        _configuration_guard()
        try:
            tools = [{"name": tool.name, "description": tool.description,
                      "argument_schema": _public_data(tool.argument_schema)}
                     for tool in request.tools]
            messages = [
                {"role": "system", "content": _CONTRACT + json.dumps(
                    tools, ensure_ascii=False, allow_nan=False)},
                {"role": "user", "content": request.prompt},
            ]
            if len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) > 32768:
                raise ValueError("Public request too large")
            raw = self._client.chat.completions.with_raw_response.create(
                model=_MODEL, messages=messages, response_format={"type": "json_object"},
                n=1, stream=False, tool_choice="none", include_reasoning=False,
                reasoning_effort="low", max_completion_tokens=2048,
            )
            try:
                if raw.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    _invalid()
                return _decode(raw.read())
            finally:
                self._cleanup(raw.close, preserve=sys.exc_info()[0] is not None)
        except ProviderError:
            raise
        except self._sdk.APITimeoutError:
            raise ProviderError(ProviderFailure.TIMEOUT) from None
        except self._sdk.APIStatusError as error:
            reason = {408: ProviderFailure.TIMEOUT, 429: ProviderFailure.RATE_LIMIT}.get(
                error.status_code, ProviderFailure.UNAVAILABLE)
            raise ProviderError(reason) from None
        except Exception:
            raise ProviderError(ProviderFailure.UNAVAILABLE) from None

    @staticmethod
    def _cleanup(close, *, preserve=False):
        try:
            close()
        except BaseException as error:
            if not preserve:
                if not isinstance(error, Exception):
                    raise
                raise ProviderError(ProviderFailure.UNAVAILABLE) from None

    def close(self):
        if not self._closed:
            self._closed = True
            self._cleanup(self._client.close)

    def __enter__(self):
        if self._closed:
            raise ProviderError(ProviderFailure.UNAVAILABLE)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._cleanup(self.close, preserve=exc_type is not None)
        return False
