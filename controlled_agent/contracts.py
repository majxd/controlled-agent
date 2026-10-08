"""Immutable data contracts, assembled by trusted application code.

Construction checks representation, not permission or provenance. A future
controller must supply caller context and an action ID generated before parsing
the proposal; it must never deserialize model output directly into these types.
Constructors do not evaluate tool schemas, policy, or execution permission.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from re import fullmatch
from types import MappingProxyType
from typing import cast
from uuid import UUID


type JSONValue = (
    None
    | bool
    | int
    | float
    | str
    | Mapping[str, JSONValue]
    | list[JSONValue]
    | tuple[JSONValue, ...]
)


class Decision(StrEnum):
    """Governance outcomes, never execution credentials."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class Risk(StrEnum):
    """Descriptive classifications with no permission or decision mapping."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class AuthorizationState(StrEnum):
    """Recorded lifecycle labels, not proof of issuance or execution permission.

    USABLE describes recorded state in the trusted registry. On a
    caller-created record it is an unverified label, never a permission check.
    """

    USABLE = "USABLE"
    CONSUMED = "CONSUMED"


class AuditEventType(StrEnum):
    """Only the lifecycle events specified for the MVP."""

    ACTION_SUBMITTED = "action_submitted"
    GOVERNANCE_DECISION = "governance_decision"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESULT = "approval_result"
    AUTHORIZATION_ISSUED = "authorization_issued"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_SUCCEEDED = "execution_succeeded"
    EXECUTION_FAILED = "execution_failed"
    EXECUTION_BLOCKED = "execution_blocked"


def _require_text(value: object, name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be a plain string")
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _require_type(value: object, expected: type, name: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{name} must be a {expected.__name__}")


def _freeze(value: JSONValue, active: set[int]) -> JSONValue:
    """Copy JSON-like data into immutable containers without retaining aliases."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if not isinstance(value, (Mapping, list, tuple)):
        raise TypeError("Expected JSON-like data, not an executable or custom object")

    identity = id(value)
    if identity in active:
        raise ValueError("JSON-like data must not contain cycles")
    active.add(identity)
    try:
        if isinstance(value, Mapping):
            frozen = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError("JSON object keys must be strings")
                frozen[key] = _freeze(item, active)
            return MappingProxyType(frozen)
        return tuple(_freeze(item, active) for item in value)
    finally:
        active.remove(identity)


def _freeze_mapping(
    value: Mapping[str, JSONValue], name: str
) -> Mapping[str, JSONValue]:
    _require_type(value, Mapping, name)
    # Freezing preserves a checked top-level mapping as a mapping.
    return cast(Mapping[str, JSONValue], _freeze(value, set()))


@dataclass(frozen=True, slots=True, kw_only=True)
class CallerContext:
    """Trusted application identity, not a model claim or a permission grant."""

    caller_id: str

    def __post_init__(self) -> None:
        _require_text(self.caller_id, "caller_id")


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionRequest:
    """An immutable host-assembled snapshot of a proposed action.

    The application generates action_id before parsing and supplies caller from
    trusted context; constructor type checks cannot establish their provenance.
    Governance derives a resource target from arguments and checks any supplied
    target for consistency.
    This constructor does not resolve targets, validate tool schemas, or mark
    an action as governed. The same ID can correlate a prior submission event.
    """

    action_id: UUID
    tool_name: str
    arguments: Mapping[str, JSONValue]
    caller: CallerContext
    target: str | None = None

    def __post_init__(self) -> None:
        _require_type(self.action_id, UUID, "action_id")
        _require_text(self.tool_name, "tool_name")
        _require_type(self.caller, CallerContext, "caller")
        if self.target is not None:
            _require_text(self.target, "target")
        object.__setattr__(
            self, "arguments", _freeze_mapping(self.arguments, "arguments")
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolDefinition:
    """Agent-facing description and argument-schema data, with no handler.

    The schema is descriptive JSON-like metadata here. Its interpretation and
    enforcement belong to governance, not contract construction. Risk is
    contextual and therefore is not a fixed property of a tool definition.
    Descriptions and schemas must contain public metadata only, without secrets
    or internal execution references. This contract does not detect or redact
    secrets embedded in text, defaults, or examples, or resolve strings as code.
    """

    name: str
    description: str
    argument_schema: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        _require_text(self.description, "description")
        object.__setattr__(
            self,
            "argument_schema",
            _freeze_mapping(self.argument_schema, "argument_schema"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class GovernanceResult:
    """A correlated evaluation outcome, never approval or execution authority.

    DENY is terminal for this evaluation. Risk can be absent when classification
    is impossible. Later approval handling must never turn DENY into permission.
    """

    action_id: UUID
    decision: Decision
    reason_code: str
    explanation: str
    risk: Risk | None = None

    def __post_init__(self) -> None:
        _require_type(self.action_id, UUID, "action_id")
        _require_type(self.decision, Decision, "decision")
        if self.risk is not None:
            _require_type(self.risk, Risk, "risk")
        _require_text(self.reason_code, "reason_code")
        if fullmatch(r"[A-Z][A-Z0-9_]*", self.reason_code) is None:
            raise ValueError("reason_code must be an uppercase machine-readable code")
        _require_text(self.explanation, "explanation")


@dataclass(frozen=True, slots=True, kw_only=True)
class Authorization:
    """Internal record data, never an execution credential or executor input.

    Holds the full immutable action, including ID, caller, tool, arguments, and
    target. The authorization service publishes references only after trusted
    ALLOW evaluation or explicit human approval of a validated REQUIRE_APPROVAL
    action, and mandatory audit writes. DENY never permits issuance.
    Its private registry resolves and consumes identity-bound references. The
    future executor must use that boundary. Constructing this dataclass is not
    issuance, evidence of approval, or execution authority.
    Neither authorization_id nor a USABLE state proves governance occurred.
    The identifier is record data, not a registered execution reference merely
    because it is a UUID. Construction does not enforce identifier uniqueness.
    Future code must load the governance-owned record, never trust a supplied
    instance or its fields as proof of issuance. This record is not agent-facing.
    """

    authorization_id: UUID
    action: ActionRequest
    state: AuthorizationState

    def __post_init__(self) -> None:
        _require_type(self.authorization_id, UUID, "authorization_id")
        _require_type(self.action, ActionRequest, "action")
        _require_type(self.state, AuthorizationState, "state")


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEvent:
    """Correlated lifecycle data; no sink, dispatch, or authorization semantics.

    details contains JSON-like event data, such as decision/risk values, reason
    codes, approval outcomes, or tool results. Event-specific payload rules and
    redaction belong to the later producers. Sensitive payload/result retention
    policy is not defined or enforced here. Malformed submissions and blocked
    attempts need only an application-assigned ID, not a valid ActionRequest.
    """

    action_id: UUID
    event_type: AuditEventType
    caller: CallerContext | None = None
    details: Mapping[str, JSONValue] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _require_type(self.action_id, UUID, "action_id")
        _require_type(self.event_type, AuditEventType, "event_type")
        if self.caller is not None:
            _require_type(self.caller, CallerContext, "caller")
        _require_type(self.timestamp, datetime, "timestamp")
        if self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        object.__setattr__(self, "details", _freeze_mapping(self.details, "details"))
