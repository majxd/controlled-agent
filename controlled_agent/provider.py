"""Offline-capable proposal boundary. Structural validity grants no authority."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .contracts import ToolDefinition
from .submission import MalformedProposal, parse_proposal


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderRequest:
    """Host-selected public metadata and synthetic prompt only.

    No handler, caller, policy, audit, or authorization is part of this API.
    Text checks cannot detect secrets; the host must curate public content.
    """

    prompt: str
    tools: tuple[ToolDefinition, ...]

    def __post_init__(self):
        if type(self.prompt) is not str or not 1 <= len(self.prompt) <= 4096:
            raise ValueError("Expected bounded public prompt")
        if type(self.tools) is not tuple or any(type(t) is not ToolDefinition for t in self.tools):
            raise TypeError("Expected public tool definitions")


class ProviderFailure(StrEnum):
    INVALID_OUTPUT = "invalid_output"
    REFUSAL = "refusal"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    UNAVAILABLE = "unavailable"


class ProviderError(Exception):
    """Fixed public diagnostic; never include provider response or exception text."""

    def __init__(self, reason: ProviderFailure):
        if type(reason) is not ProviderFailure:
            raise TypeError("Expected provider failure code")
        self.reason = reason
        super().__init__(reason.value)


class ProposalProvider(Protocol):
    def propose(self, request: ProviderRequest) -> object:
        """Return an untrusted plain proposal or None (no_action), or raise.

        Future SDK-specific decoding belongs in the provider implementation.
        No SDK tool dispatch, automatic retry, or fallback is allowed.
        """
        ...


class ProposalAdapter:
    """Call once, copy/check the envelope, return data to the trusted host.

    The host submits each non-None result once through Controller.submit().
    Unknown tools and invalid tool semantics are left to governance. This
    adapter neither receives nor constructs execution components or action IDs.
    """

    def __init__(self, provider: ProposalProvider):
        self._provider = provider

    def propose(self, request: ProviderRequest) -> dict | None:
        if type(request) is not ProviderRequest:
            raise TypeError("Expected public provider request")
        try:
            output = self._provider.propose(request)
        except ProviderError as error:
            reason = getattr(error, "reason", None)
            if type(reason) is not ProviderFailure:
                reason = ProviderFailure.UNAVAILABLE
            raise ProviderError(reason) from None
        except TimeoutError:
            raise ProviderError(ProviderFailure.TIMEOUT) from None
        except Exception:
            raise ProviderError(ProviderFailure.UNAVAILABLE) from None
        # Interruptions propagate; no retry or trusted-state reset occurs.
        if output is None:
            return None
        try:
            tool, arguments = parse_proposal(output)
        except MalformedProposal:
            raise ProviderError(ProviderFailure.INVALID_OUTPUT) from None
        return {"tool_name": tool, "arguments": arguments}
