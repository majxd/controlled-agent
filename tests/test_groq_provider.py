"""Actual SDK over MockTransport only; every SDK test blocks sockets and DNS."""

from contextlib import ExitStack
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from controlled_agent.approval import HumanApprovalAdapter
from controlled_agent.audit import JsonlAuditWriter
from controlled_agent.authorization import AuthorizationService
from controlled_agent.contracts import CallerContext, ToolDefinition
from controlled_agent.controller import Controller, Outcome
from controlled_agent.demo import TOOLS, PERMISSIONS, WORK_ORDERS, format_work_order_approval
from controlled_agent.demo_tools import FakeWorkOrderTools
from controlled_agent.executor import Executor
from controlled_agent.groq_provider import GroqProvider, _decode, _strict_json
from controlled_agent.provider import ProviderRequest, ProposalAdapter, ProviderError, ProviderFailure


SDK_AVAILABLE = all(importlib.util.find_spec(name) for name in ("groq", "httpx"))
DUMMY_KEY = "dummy-offline-key-not-a-credential"
ROOT = Path(__file__).resolve().parents[1]
DENIED = ["action_submitted", "governance_decision"]
READ = DENIED + ["authorization_issued", "execution_started", "execution_succeeded"]
UPDATED = DENIED + ["approval_requested", "approval_result"] + READ[2:]


def proposal(tool="get_work_order", target="WO-1001"):
    arguments = {"work_order_id": target}
    if tool == "update_work_order_status":
        arguments["new_status"] = "closed"
    return {"tool_name": tool, "arguments": arguments}


def content(data=None):
    return json.dumps({"type": "proposal", "proposal": proposal() if data is None else data})


def envelope(text=None, *, finish="stop", **metadata):
    return {"id": "synthetic-response", "object": "chat.completion", "choices": [
        {"index": 0, "finish_reason": finish,
         "message": {"role": "assistant", "content": content() if text is None else text,
                     **metadata}}]}


def encoded(value):
    return json.dumps(value).encode()


class DecoderTests(unittest.TestCase):
    def invalid(self, raw):
        with self.assertRaises(ProviderError) as caught:
            _decode(raw)
        self.assertEqual(caught.exception.reason, ProviderFailure.INVALID_OUTPUT)

    def test_exact_wire_variants_and_copied_plain_proposal(self):
        self.assertEqual(_decode(encoded(envelope())), proposal())
        self.assertIsNone(_decode(encoded(envelope('{"type":"no_action"}'))))
        with self.assertRaises(ProviderError) as caught:
            _decode(encoded(envelope('{"type":"refusal"}')))
        self.assertEqual(caught.exception.reason, ProviderFailure.REFUSAL)
        self.assertEqual(_decode(encoded(envelope(content(proposal("unknown"))))), proposal("unknown"))

    def test_wire_shape_authority_and_argument_bounds(self):
        cases = ['"no_action"', "null", "[]", "{}", '{"type":"no_action","approved":true}',
                 '{"type":"refusal","reason":"text"}', content(proposal() | {"caller": "admin"}),
                 content({"tool_name": "x", "arguments": {"x": {}}}),
                 content({"tool_name": "x", "arguments": {"x": "a" * 257}}),
                 content({"tool_name": "x", "arguments": {"x": 2 ** 63}}),
                 content({"tool_name": "x", "arguments": {str(n): n for n in range(9)}}),
                 content({"tool_name": "x" * 129, "arguments": {}})]
        for text in cases:
            with self.subTest(text=text[:60]):
                self.invalid(encoded(envelope(text)))

    def test_ambiguous_and_malformed_json_at_both_layers(self):
        malformed = ['{"type":"proposal","type":"no_action"}',
                     '{"type":"no_action","t\\u0079pe":"no_action"}',
                     '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e999}',
                     '{"x":"\\ud800"}', '{"\\udfff":1}', '{} {}',
                     '```json\n{}\n```', 'Here is {}', '{"x":', '"bad\x01"',
                     '[' * 17 + '0' + ']' * 17]
        for text in malformed:
            with self.subTest(text=text[:60]):
                self.invalid(text.encode())
                self.invalid(encoded(envelope(text)))
        self.invalid(b'\xff')
        self.invalid(b'\xef\xbb\xbf{}')
        self.invalid(encoded(envelope())[:-1])

    def test_missing_truncated_and_unsupported_api_responses(self):
        cases = [[], {}, {"choices": []}, {"choices": [None]},
                 envelope() | {"choices": envelope()["choices"] * 2},
                 envelope() | {"error": {}}, envelope() | {"object": "chat.completion.chunk"},
                 envelope() | {"mcp_list_tools": [{"tools": []}]}]
        for finish in (None, "length", "tool_calls", "function_call", "unknown"):
            cases.append(envelope('{"type":"no_action"}', finish=finish))
        for key, value in (("content", None), ("content", ""), ("content", "  "),
                           ("content", []), ("role", "tool"), ("refusal", False),
                           ("tool_calls", [{}]), ("tool_calls", {}),
                           ("executed_tools", [{}]), ("function_call", {}),
                           ("annotations", [{}]), ("reasoning", "hidden reasoning")):
            item = envelope()
            item["choices"][0]["message"][key] = value
            cases.append(item)
        for key in ("content", "role"):
            item = envelope()
            del item["choices"][0]["message"][key]
            cases.append(item)
        for key, value in (("index", True), ("index", 1), ("message", [])):
            item = envelope()
            item["choices"][0][key] = value
            cases.append(item)
        item = envelope()
        del item["choices"][0]["finish_reason"]
        cases.append(item)
        for value in cases:
            with self.subTest(value=value):
                self.invalid(encoded(value))

    def test_harmless_metadata_is_accepted_but_explicit_refusal_is_not_no_action(self):
        for empty in (None, []):
            value = envelope(tool_calls=empty, executed_tools=empty, annotations=empty,
                             function_call=None, reasoning=None, refusal=None)
            value.update(usage=None, system_fingerprint=None, mcp_list_tools=empty)
            self.assertEqual(_decode(encoded(value)), proposal())
        self.assertEqual(_decode(encoded(envelope(reasoning="", refusal=""))), proposal())
        for value in (envelope(refusal="Declined"), envelope(finish="content_filter")):
            with self.assertRaises(ProviderError) as caught:
                _decode(encoded(value))
            self.assertEqual(caught.exception.reason, ProviderFailure.REFUSAL)

    def test_depth_size_unicode_and_finite_boundaries(self):
        self.assertEqual(_strict_json('[' * 16 + '0' + ']' * 16, 100),
                         json.loads('[' * 16 + '0' + ']' * 16))
        self.assertEqual(_strict_json('{"x":"[\\\"{}]","y":"\\ud83d\\ude00"}', 100),
                         {"x": '["{}]', "y": "😀"})
        self.invalid(b' ' * 65537)
        self.invalid(encoded(envelope(' ' * 8193)))
        self.invalid(encoded(envelope(content({"tool_name": "x", "arguments": {
            str(i): "😀" * 256 for i in range(8)}}))))
        valid = {"tool_name": "x", "arguments": {"low": -(2 ** 63), "high": 2 ** 63 - 1,
                 "number": 1.5, "bool": True, "null": None, "unicode": "😀"}}
        self.assertEqual(_decode(encoded(envelope(content(valid)))), valid)

    def test_default_operation_without_site_packages_or_sdk(self):
        script = '''
import importlib.util, sys
assert importlib.util.find_spec("groq") is None
assert importlib.util.find_spec("httpx") is None
from controlled_agent.groq_provider import GroqProvider
from controlled_agent.provider import ProviderRequest, ProposalAdapter, ProviderError
from controlled_agent.mock_provider import MockProvider
from controlled_agent import cli
assert ProposalAdapter(MockProvider()).propose(ProviderRequest(prompt="synthetic", tools=())) is None
assert "groq" not in sys.modules and "httpx" not in sys.modules
try:
    GroqProvider(api_key="dummy")
except ProviderError as error:
    assert str(error) == "unavailable"
else:
    raise AssertionError("Optional dependencies unexpectedly available")
'''
        result = subprocess.run([sys.executable, "-B", "-S", "-c", script], cwd=ROOT,
                                env={"PATH": os.defpath}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(SDK_AVAILABLE, "optional groq/httpx extra not installed")
class GroqSDKTests(unittest.TestCase):
    def setUp(self):
        self.stack = self.enterContext(ExitStack())
        # Empty trusted test environment: do not read real credentials or SDK config.
        self.stack.enter_context(patch("os.environ", {}))
        self.network = []
        for target in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
                       "socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
                       "socket.socket.sendto"):
            self.network.append(self.stack.enter_context(patch(
                target, side_effect=AssertionError("Network forbidden in offline tests"))))
        import groq
        import httpx
        self.groq, self.httpx = groq, httpx
        self.request = ProviderRequest(prompt="Read synthetic WO-1001.", tools=tuple(
            rule.definition for rule in TOOLS.values()))
        self.requests = []
        self.responses = []
        self.transports = []

    def tearDown(self):
        for blocked in self.network:
            blocked.assert_not_called()

    def provider(self, *responses):
        queue = list(responses)

        def handle(request):
            self.requests.append(request)
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            response = item if isinstance(item, self.httpx.Response) else self.httpx.Response(200, json=item)
            self.responses.append(response)
            return response

        transport = self.httpx.MockTransport(handle)
        transport.close = Mock(wraps=transport.close)
        self.transports.append(transport)
        provider = GroqProvider(api_key=DUMMY_KEY, transport=transport)
        self.addCleanup(provider.close)
        return provider, queue

    def failure(self, provider, code):
        with self.assertRaises(ProviderError) as caught:
            ProposalAdapter(provider).propose(self.request)
        self.assertEqual(str(caught.exception), code.value)
        self.assertEqual(caught.exception.reason, code)

    def test_actual_sdk_request_parameters_and_single_attempt(self):
        provider, queue = self.provider(envelope())
        result = ProposalAdapter(provider).propose(self.request)
        self.assertEqual(result, proposal())
        self.assertEqual(queue, [])
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["authorization"], "Bearer " + DUMMY_KEY)
        body = json.loads(request.content)
        self.assertEqual(set(body), {"model", "messages", "response_format", "n", "stream",
                                    "tool_choice", "include_reasoning", "reasoning_effort",
                                    "max_completion_tokens"})
        self.assertEqual({key: value for key, value in body.items() if key != "messages"}, {
            "model": "openai/gpt-oss-20b", "response_format": {"type": "json_object"},
            "n": 1, "stream": False, "tool_choice": "none", "include_reasoning": False,
            "reasoning_effort": "low", "max_completion_tokens": 2048})
        self.assertEqual(body["messages"][1], {"role": "user", "content": self.request.prompt})
        descriptions = json.loads(body["messages"][0]["content"].split("Public tool descriptions follow: ")[1])
        self.assertEqual([t["name"] for t in descriptions], [t.name for t in self.request.tools])
        self.assertEqual(set(descriptions[0]), {"name", "description", "argument_schema"})
        for forbidden in (DUMMY_KEY, "demo_operator", "authorization_issued"):
            self.assertNotIn(forbidden, request.content.decode())
        self.assertEqual(request.extensions["timeout"], {"connect": 5.0, "read": 20.0, "write": 10.0, "pool": 5.0})
        self.assertEqual(provider._client.max_retries, 0)
        self.assertTrue(self.responses[0].is_closed)

    def test_status_mapping_no_retry_redirect_or_queued_success_consumption(self):
        for status in (301, 307, 400, 401, 403, 404, 408, 409, 422, 429, 500, 503):
            with self.subTest(status=status):
                before = len(self.requests)
                response = self.httpx.Response(status, json={"error": {"message": "SECRET"}},
                                               headers={"location": "https://example.invalid/", "retry-after": "0"})
                provider, queue = self.provider(response, envelope())
                reason = {408: ProviderFailure.TIMEOUT, 429: ProviderFailure.RATE_LIMIT}.get(
                    status, ProviderFailure.UNAVAILABLE)
                self.failure(provider, reason)
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(queue, [envelope()])
                self.assertTrue(response.is_closed)

    def test_default_transport_safety_settings(self):
        transport = self.httpx.MockTransport(lambda _: self.fail("Unexpected HTTP"))
        options = {}
        class ObservedClient(self.httpx.Client):
            def __init__(self, **kwargs):
                options.update(kwargs)
                super().__init__(**kwargs)
        with patch.object(self.httpx, "HTTPTransport", return_value=transport) as create_transport:
            with patch.object(self.httpx, "Client", ObservedClient):
                with GroqProvider(api_key=DUMMY_KEY) as provider:
                    create_transport.assert_called_once_with(retries=0, verify=True, trust_env=False)
                    self.assertFalse(options["follow_redirects"])
                    self.assertFalse(options["trust_env"])
                    self.assertEqual(provider._client.max_retries, 0)

    def test_transport_failure_mapping_and_no_retry(self):
        errors = [self.httpx.ConnectError("SECRET"), RuntimeError("SECRET")]
        errors += [kind("SECRET") for kind in (self.httpx.ConnectTimeout, self.httpx.ReadTimeout,
                                               self.httpx.WriteTimeout, self.httpx.PoolTimeout)]
        for error in errors:
            with self.subTest(kind=type(error)):
                provider, queue = self.provider(error, envelope())
                before = len(self.requests)
                self.failure(provider, ProviderFailure.TIMEOUT if isinstance(error, self.httpx.TimeoutException)
                             else ProviderFailure.UNAVAILABLE)
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(queue, [envelope()])

    def test_raw_interface_preserves_duplicates_and_rejects_wrong_content_type(self):
        good = encoded(envelope())
        duplicate = good[:-1] + b',"choices":[]}'
        for data, media in ((duplicate, "application/json"), (good, "text/plain"),
                            (b'\xff', "application/json"), (good[:-1], "application/json")):
            provider, queue = self.provider(self.httpx.Response(200, content=data, headers={"content-type": media}), envelope())
            self.failure(provider, ProviderFailure.INVALID_OUTPUT)
            self.assertEqual(queue, [envelope()])
            self.assertTrue(self.responses[-1].is_closed)

    def test_request_bounds_fail_before_http(self):
        provider, queue = self.provider(envelope())
        for request in (ProviderRequest(prompt="\ud800", tools=()),
                        ProviderRequest(prompt="synthetic", tools=(ToolDefinition(
                            name="x", description="x" * 33000, argument_schema={}),))):
            with self.assertRaises(ProviderError) as caught:
                provider.propose(request)
            self.assertEqual(caught.exception.reason, ProviderFailure.UNAVAILABLE)
        with self.assertRaises(TypeError):
            provider.propose({})
        self.assertEqual(self.requests, [])
        self.assertEqual(queue, [envelope()])

    def test_close_ownership_idempotence_and_no_reopening(self):
        provider, queue = self.provider(envelope())
        with provider:
            self.assertFalse(provider._client.is_closed())
        self.transports[-1].close.assert_called_once()
        provider.close()
        self.transports[-1].close.assert_called_once()
        self.failure(provider, ProviderFailure.UNAVAILABLE)
        with self.assertRaises(ProviderError):
            provider.__enter__()
        self.assertEqual(self.requests, [])
        self.assertEqual(queue, [envelope()])

    def test_interruptions_and_cleanup_failure_preserve_original(self):
        for error in (KeyboardInterrupt(), SystemExit(7)):
            provider, queue = self.provider(error, envelope())
            original_close = self.transports[-1].close

            def bad_close():
                original_close()
                raise RuntimeError("SECRET")

            self.transports[-1].close = Mock(side_effect=bad_close)
            with self.assertRaises(type(error)) as caught:
                with provider:
                    provider.propose(self.request)
            self.assertIs(caught.exception, error)
            self.assertEqual(queue, [envelope()])
            self.transports[-1].close.assert_called_once()
            self.assertTrue(provider._client.is_closed())

    def test_cleanup_failure_is_sanitized_and_terminal(self):
        provider, _ = self.provider(envelope())
        original_close = self.transports[-1].close
        def bad_close():
            original_close()
            raise RuntimeError("SECRET")
        self.transports[-1].close = Mock(side_effect=bad_close)
        with self.assertRaises(ProviderError) as caught:
            provider.close()
        self.assertEqual(str(caught.exception), "unavailable")
        provider.close()
        self.transports[-1].close.assert_called_once()
        self.failure(provider, ProviderFailure.UNAVAILABLE)

    def test_constructor_failure_closes_owned_transport(self):
        transport = self.httpx.MockTransport(lambda _: self.fail("Unexpected HTTP"))
        transport.close = Mock(wraps=transport.close)
        with patch.object(self.groq, "Groq", side_effect=RuntimeError("SECRET")):
            with self.assertRaises(ProviderError) as caught:
                GroqProvider(api_key=DUMMY_KEY, transport=transport)
        self.assertEqual(str(caught.exception), "unavailable")
        transport.close.assert_called_once()
        for key in (None, "", "   "):
            with self.assertRaises(ProviderError):
                GroqProvider(api_key=key, transport=transport)

    def test_raw_cleanup_does_not_replace_invalid_output(self):
        provider, _ = self.provider(envelope())
        raw = Mock(headers={"content-type": "application/json"})
        raw.read.return_value = b'{}'
        raw.close.side_effect = RuntimeError("SECRET")
        # The transport tests above exercise the real SDK. This narrow fault
        # injection covers cleanup failure in its documented raw interface.
        with patch.object(provider._client.chat.completions.with_raw_response, "create", return_value=raw):
            self.failure(provider, ProviderFailure.INVALID_OUTPUT)
        raw.close.assert_called_once()
        self.assertEqual(self.requests, [])

    def test_configuration_guards_do_not_change_logging_or_use_ambient_secrets(self):
        provider, queue = self.provider(envelope())
        for name in ("GROQ_LOG", "GROQ_CUSTOM_HEADERS"):
            with patch.dict(os.environ, {name: "dummy-unsafe-setting"}):
                with self.assertRaises(ProviderError):
                    GroqProvider(api_key=DUMMY_KEY)
                self.failure(provider, ProviderFailure.UNAVAILABLE)
        logger = logging.getLogger("groq._base_client")
        previous_level = logger.level
        try:
            logger.setLevel(logging.DEBUG)
            with self.assertRaises(ProviderError):
                GroqProvider(api_key=DUMMY_KEY)
            self.failure(provider, ProviderFailure.UNAVAILABLE)
            self.assertEqual(logger.level, logging.DEBUG)
        finally:
            logger.setLevel(previous_level)
        self.assertEqual(self.requests, [])
        self.assertEqual(queue, [envelope()])
        with patch.dict(os.environ, {"GROQ_API_KEY": "ignored-dummy-key", "GROQ_BASE_URL": "https://example.invalid",
                                     "HTTPS_PROXY": "http://example.invalid:1234"}):
            safe, _ = self.provider(envelope())
            safe.propose(self.request)
        self.assertEqual(self.requests[-1].url.host, "api.groq.com")
        self.assertEqual(self.requests[-1].headers["authorization"], "Bearer " + DUMMY_KEY)

    def test_logs_contain_no_bodies_credentials_or_raw_failures(self):
        records = []
        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)
        handler = Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        for reply in (envelope(), self.httpx.Response(500, json={"error": {"message": "SECRET"}}),
                      self.httpx.ConnectError("SECRET")):
            provider, _ = self.provider(reply)
            try:
                provider.propose(self.request)
            except ProviderError:
                pass
        messages = " ".join(record.getMessage() for record in records)
        for forbidden in (DUMMY_KEY, "SECRET", self.request.prompt, '"proposal"'):
            self.assertNotIn(forbidden, messages)
        self.assertFalse(any(record.exc_info for record in records))

    def session(self, *, permissions=None):
        temp = self.enterContext(TemporaryDirectory())
        self.path = Path(temp) / "audit.jsonl"
        self.writer = self.enterContext(JsonlAuditWriter(self.path))
        self.new_id = self.enterContext(patch("controlled_agent.controller.uuid4", wraps=uuid4))
        self.review, self.approve = Mock(return_value=None), Mock(return_value="approve")
        self.service = AuthorizationService(
            tools=TOOLS, permissions=PERMISSIONS if permissions is None else permissions,
            resources=WORK_ORDERS, audit_write=self.writer,
            approval_formatter=format_work_order_approval,
            human_approval=HumanApprovalAdapter(display=self.review, read_response=self.approve))
        self.fixture = FakeWorkOrderTools(authorization=self.service)
        self.handlers = {name: Mock(wraps=handler) for name, handler in self.fixture.handlers.items()}
        self.controller = Controller(authorization=self.service,
                                     executor=Executor(authorization=self.service, handlers=self.handlers),
                                     caller=CallerContext(caller_id="demo_operator"))
        self.submit = Mock(wraps=self.controller.submit)

    def host_call(self, provider):
        data = ProposalAdapter(provider).propose(self.request)
        return None if data is None else self.submit(data)

    def lifecycle(self, report, expected):
        records = [json.loads(line) for line in self.path.read_text().splitlines()]
        action = [r for r in records if r["action_id"] == str(report.action_id)]
        self.assertEqual([r["event_type"] for r in action], expected)
        self.assertTrue(all(r["caller"] == "demo_operator" for r in action))
        self.assertEqual(action[1]["details"]["decision"], report.decision.value)
        self.assertEqual({r["action_id"] for r in records}, {str(i) for i in self.service._seen})
        self.assertNotIn(DUMMY_KEY, self.path.read_text())
        return action

    def test_failures_and_no_action_never_submit_create_id_or_audit(self):
        self.session()
        replies = [(envelope('{"type":"no_action"}'), None),
                   (envelope('{"type":"refusal"}'), ProviderFailure.REFUSAL),
                   (envelope(content(proposal() | {"approved": True})), ProviderFailure.INVALID_OUTPUT),
                   (envelope(finish="length"), ProviderFailure.INVALID_OUTPUT),
                   (self.httpx.ReadTimeout("SECRET"), ProviderFailure.TIMEOUT),
                   (self.httpx.Response(429), ProviderFailure.RATE_LIMIT),
                   (self.httpx.Response(500), ProviderFailure.UNAVAILABLE)]
        for reply, reason in replies:
            provider, queue = self.provider(reply, envelope())
            if reason is None:
                self.assertIsNone(self.host_call(provider))
            else:
                with self.assertRaises(ProviderError) as caught:
                    self.host_call(provider)
                self.assertEqual(caught.exception.reason, reason)
            self.assertEqual(queue, [envelope()])
        self.submit.assert_not_called()
        self.new_id.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertEqual(self.service._seen, set())
        self.assertEqual(self.service._records, {})
        self.assertEqual(self.fixture._statuses, {"WO-1001": "open", "WO-9001": "open"})
        self.review.assert_not_called()
        for handler in self.handlers.values():
            handler.assert_not_called()

    def test_jsonl_read_approval_failure_and_preserved_session_state(self):
        self.session()
        update = content(proposal("update_work_order_status"))
        provider, queue = self.provider(envelope(update), self.httpx.Response(503), envelope(), envelope(update))
        first = self.host_call(provider)
        self.assertEqual(first.outcome, Outcome.SUCCEEDED)
        self.lifecycle(first, UPDATED)
        review = self.review.call_args.args[0]
        self.assertEqual(review.action.arguments, proposal("update_work_order_status")["arguments"])
        self.assertEqual(review.action.action_id, first.action_id)
        self.assertEqual(review.action.caller.caller_id, "demo_operator")
        before = (self.path.read_bytes(), self.service._seen.copy(), dict(self.service._records),
                  {ref: entry.consumed for ref, entry in self.service._records.items()},
                  self.fixture._statuses.copy())
        with self.assertRaises(ProviderError):
            self.host_call(provider)
        after = (self.path.read_bytes(), self.service._seen.copy(), dict(self.service._records),
                 {ref: entry.consumed for ref, entry in self.service._records.items()},
                 self.fixture._statuses.copy())
        self.assertEqual(before, after)
        self.assertTrue(all(before[3].values()))
        self.assertEqual(queue, [envelope(), envelope(update)])
        self.assertEqual(len(self.requests), 2)
        self.submit.assert_called_once()
        self.new_id.assert_called_once()
        self.handlers["get_work_order"].assert_not_called()
        self.handlers["update_work_order_status"].assert_called_once()
        self.approve.assert_called_once()
        read = self.host_call(provider)  # New explicit host call; never an automatic retry.
        self.assertEqual(read.result["status"], "closed")
        self.lifecycle(read, READ)
        stale = self.host_call(provider)
        self.assertTrue(stale.stale_baseline)
        self.assertEqual(stale.outcome, Outcome.REJECTED)
        self.lifecycle(stale, UPDATED[:-1] + ["execution_failed"])
        self.assertEqual(self.fixture._statuses, {"WO-1001": "closed", "WO-9001": "open"})
        self.assertEqual(self.handlers["update_work_order_status"].call_count, 2)
        self.handlers["get_work_order"].assert_called_once()
        self.assertEqual(self.submit.call_count, 3)
        self.assertEqual(self.new_id.call_count, 3)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(queue, [])
        self.assertTrue(all(entry.consumed for entry in self.service._records.values()))

    def test_governance_denials_and_human_decline_remain_authoritative(self):
        self.session()
        claim = proposal("update_work_order_status")
        claim["arguments"]["approved"] = True
        cases = [proposal("unknown"), proposal(target="WO-UNKNOWN"),
                 proposal("update_work_order_status", "WO-9001"), claim]
        for value in cases:
            provider, _ = self.provider(envelope(content(value)))
            report = self.host_call(provider)
            self.assertEqual(report.outcome, Outcome.DENIED)
            self.lifecycle(report, DENIED)
        self.review.assert_not_called()
        self.approve.return_value = "decline"
        provider, _ = self.provider(envelope(content(proposal("update_work_order_status"))))
        declined = self.host_call(provider)
        self.assertEqual(declined.outcome, Outcome.APPROVAL_NOT_GRANTED)
        self.lifecycle(declined, UPDATED[:4])
        for handler in self.handlers.values():
            handler.assert_not_called()
        self.assertEqual(self.fixture._statuses, {"WO-1001": "open", "WO-9001": "open"})

    def test_missing_permissions_cannot_be_granted_by_structural_output(self):
        self.session(permissions={})
        provider, _ = self.provider(envelope())
        report = self.host_call(provider)
        self.assertEqual(report.outcome, Outcome.DENIED)
        self.lifecycle(report, DENIED)
        for handler in self.handlers.values():
            handler.assert_not_called()
        self.assertEqual(self.fixture._statuses, {"WO-1001": "open", "WO-9001": "open"})
