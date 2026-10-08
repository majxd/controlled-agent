"""Trusted issuance, optional human approval, and one-use consumption; no dispatch.

The host owns one sequential service per run. Its configuration and synchronous
audit writer are trusted; no service or opaque reference belongs in agent data.
Python privacy is an application boundary, not a sandbox for hostile code.
"""

from collections.abc import Callable, Mapping, Set
from contextlib import contextmanager
from dataclasses import dataclass, replace
from re import fullmatch
from types import MappingProxyType
from uuid import UUID, uuid4

from .approval import ApprovalChange, ApprovalReview, HumanApprovalAdapter
from .contracts import (
    ActionRequest, AuditEvent, AuditEventType, Authorization, AuthorizationState,
    Decision, GovernanceResult, JSONValue, _freeze_mapping,
)
from .governance import ToolRules, _evaluate_action


def _audit_identifier(value: str | None) -> str | None:
    """Retain only bounded public configuration identifiers, never raw claims.

    Callers must first establish membership in trusted configuration. A matching
    spelling alone does not make arbitrary proposal text safe to retain.
    """
    if value is not None and len(value) <= 128 and fullmatch(r"[A-Za-z0-9_.:-]+", value):
        return value
    return None


class AuthorizationError(RuntimeError):
    """The operation supplied no new execution authority."""


class DuplicateActionError(AuthorizationError):
    """The action ID was already admitted, including unsuccessful attempts."""


class InvalidReferenceError(AuthorizationError):
    """The reference was not issued here or has already been consumed."""


class ReentrantCallError(AuthorizationError):
    """A nested service operation was rejected and the outer operation aborted."""


class AuditWriteError(AuthorizationError):
    """The mandatory audit writer failed its synchronous write contract."""


class ApprovalError(AuthorizationError):
    """Trusted review could not complete; the action ID remains terminal."""


class _Reference:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<internal authorization reference>"


@dataclass(slots=True)
class _Entry:
    authorization: Authorization
    consumed: bool = False


@dataclass(slots=True)
class _PendingApproval:
    action: ActionRequest
    result: GovernanceResult
    closed: bool = False


class AuthorizationService:
    """Trusted host API. Neither issue()'s tuple nor consume() is agent-facing.

    audit_write(event) must return None only after the required write and flush
    succeed, or raise. There is no default/no-op writer or concrete audit sink.
    Callbacks must be synchronous and configuration closures must remain fixed.
    This guard handles reentrancy, not concurrent use from multiple threads.
    """

    def __init__(
        self, *, tools: Mapping[str, ToolRules], permissions: Mapping[str, Set[str]],
        resources: Mapping[str, Mapping[str, JSONValue]],
        audit_write: Callable[[AuditEvent], None],
        approval_formatter: Callable[[ActionRequest, Mapping[str, JSONValue]], ApprovalChange] | None = None,
        human_approval: HumanApprovalAdapter | None = None,
    ) -> None:
        if not callable(audit_write):
            raise TypeError("audit_write must be a synchronous audit writer")
        if (approval_formatter is None) != (human_approval is None):
            raise TypeError("approval formatter and human adapter must be configured together")
        if human_approval is not None and (
            type(human_approval) is not HumanApprovalAdapter or not callable(approval_formatter)
        ):
            raise TypeError("approval requires a trusted formatter and HumanApprovalAdapter")
        self._tools = MappingProxyType(dict(tools))
        for name, rules in self._tools.items():
            if not isinstance(rules, ToolRules) or rules.definition.name != name:
                raise TypeError("tools must contain matching ToolRules")
        permission_copy = {}
        for caller, names in permissions.items():
            if type(caller) is not str or not isinstance(names, Set):
                raise TypeError("permissions must map caller strings to sets of tool strings")
            if any(type(name) is not str for name in names):
                raise TypeError("permission entries must be tool strings")
            permission_copy[caller] = frozenset(names)
        self._permissions = MappingProxyType(permission_copy)
        self._resources = _freeze_mapping(resources, "resources")
        if any(not isinstance(context, Mapping) for context in self._resources.values()):
            raise TypeError("resources must contain metadata mappings")
        self._audit_write = audit_write
        self._approval_formatter = approval_formatter
        self._human_approval = human_approval
        self._pending: _PendingApproval | None = None
        self._seen: set[UUID] = set()
        self._records: dict[_Reference, _Entry] = {}
        self._busy = False
        self._reentered = False

    @contextmanager
    def _operation(self):
        if self._busy:
            self._reentered = True
            raise ReentrantCallError("Reentrant authorization operations are forbidden")
        self._busy = True
        self._reentered = False
        try:
            yield
        finally:
            self._busy = False

    def _check_reentrancy(self) -> None:
        if self._reentered:
            raise ReentrantCallError("A callback attempted a reentrant authorization operation")

    def _write(self, event: AuditEvent) -> None:
        try:
            result = self._audit_write(event)
        except BaseException as error:
            if not isinstance(error, Exception):
                # Preserve interruption identity while allowing the executor to
                # distinguish interrupted rejected-reference auditing from a
                # failure during consumption itself. No payload is attached.
                error._audit_write_interrupted = True
                raise
            self._check_reentrancy()
            raise AuditWriteError("A required audit write failed") from error
        self._check_reentrancy()
        if result is not None:
            raise AuditWriteError("The audit writer must return None on successful write and flush")

    def _audit_tool_name(self, request: ActionRequest) -> str | None:
        return _audit_identifier(request.tool_name) if request.tool_name in self._tools else None

    def _decision_event(
        self, request: ActionRequest, result: GovernanceResult,
        action: ActionRequest | None = None, resolved_target: str | None = None,
    ) -> AuditEvent:
        return AuditEvent(
            action_id=request.action_id, caller=request.caller,
            event_type=AuditEventType.GOVERNANCE_DECISION,
            details={"decision": result.decision.value,
                     "risk": result.risk.value if result.risk is not None else None,
                     "reason_code": result.reason_code,
                     "tool_name": self._audit_tool_name(request),
                     "resolved_target": _audit_identifier(resolved_target),
                     "action_validated": action is not None},
        )

    def _publish(self, reference: _Reference, authorization: Authorization) -> None:
        self._records[reference] = _Entry(authorization)

    def _approval_event(
        self, action: ActionRequest, event_type: AuditEventType,
        *, outcome: str | None = None, reason_code: str | None = None,
    ) -> AuditEvent:
        details = {"tool_name": self._audit_tool_name(action),
                   "resolved_target": _audit_identifier(action.target)}
        if outcome is not None:
            details.update(outcome=outcome, reason_code=reason_code)
        return AuditEvent(action_id=action.action_id, caller=action.caller,
                          event_type=event_type, details=details)

    def _review_action(self, action: ActionRequest, result: GovernanceResult) -> bool:
        """One private review attempt; only internal evaluation can reach here.

        False supplies no authority. True is usable only in this issue() call,
        whose original action and publication rollback boundary remain intact.
        """
        if self._human_approval is None:
            return False
        pending = _PendingApproval(action, result)
        try:
            self._pending = pending
            self._write(self._approval_event(action, AuditEventType.APPROVAL_REQUESTED))
            try:
                # Same immutable resource snapshot used by governance; no re-resolution.
                change = self._approval_formatter(action, self._resources[action.target])
                self._check_reentrancy()
                review = ApprovalReview(action=action, change=change)
                displayed = self._human_approval.display(review)
                self._check_reentrancy()
                if displayed is not None:
                    raise ApprovalError("Display must return None after presenting the review")
                response = self._human_approval.read_response()
                self._check_reentrancy()
            except BaseException as error:
                pending.closed = True
                self._pending = None
                # A poisoned operation invokes no further external callbacks.
                self._check_reentrancy()
                if isinstance(error, EOFError):
                    self._write(self._approval_event(
                        action, AuditEventType.APPROVAL_RESULT,
                        outcome="cancelled", reason_code="END_OF_INPUT",
                    ))
                    return False
                interrupted = not isinstance(error, Exception)
                try:
                    self._write(self._approval_event(
                        action, AuditEventType.APPROVAL_RESULT,
                        outcome="cancelled" if interrupted else "error",
                        reason_code="INTERRUPTED" if interrupted else "REVIEW_FAILED",
                    ))
                except BaseException as audit_error:
                    if interrupted:
                        raise error from audit_error
                    raise
                if interrupted:
                    raise
                raise ApprovalError("Human review could not complete") from error

            # Close before result auditing, including an affirmative result.
            pending.closed = True
            self._pending = None
            if type(response) is str and response.strip() == "approve":
                outcome, reason = "approved", "HUMAN_APPROVED"
            elif type(response) is str and response.strip() == "decline":
                outcome, reason = "declined", "HUMAN_DECLINED"
            elif response is None or (type(response) is str and not response.strip()):
                outcome, reason = "cancelled", "NO_RESPONSE"
            else:
                outcome, reason = "cancelled", "INVALID_RESPONSE"
            self._write(self._approval_event(action, AuditEventType.APPROVAL_RESULT,
                                             outcome=outcome, reason_code=reason))
            return outcome == "approved"
        finally:
            pending.closed = True
            self._pending = None

    def issue(self, request: ActionRequest) -> tuple[GovernanceResult, object | None]:
        """Evaluate internally and return (reportable result, host-only reference).

        DENY never issues. REQUIRE_APPROVAL can issue only after configured human
        review; its original decision stays REQUIRE_APPROVAL in the return tuple.
        All admitted IDs are terminal, including review/issuance failures.
        Raw proposal parsing/ID assignment belong to the later controller.
        """
        reference = None
        try:
            with self._operation():
                if not isinstance(request, ActionRequest):
                    raise TypeError("request must be a host-assembled ActionRequest")
                duplicate = request.action_id in self._seen
                self._seen.add(request.action_id)  # Before any audit or policy callback.
                self._write(AuditEvent(
                    action_id=request.action_id, caller=request.caller,
                    event_type=AuditEventType.ACTION_SUBMITTED,
                    # A claimed selection of a public registered tool, not validation.
                    details={"submitted_tool_name": self._audit_tool_name(request)},
                ))
                if duplicate:
                    result = GovernanceResult(
                        action_id=request.action_id, decision=Decision.DENY,
                        reason_code="DUPLICATE_ACTION_ID",
                        explanation="This action ID has already been submitted.",
                    )
                    self._write(self._decision_event(request, result))
                    raise DuplicateActionError("An action ID cannot be reused")

                result, action, resolved_target = _evaluate_action(
                    request, tools=self._tools, permissions=self._permissions,
                    resources=self._resources,
                )
                self._check_reentrancy()
                self._write(self._decision_event(request, result, action, resolved_target))
                if result.decision is not Decision.ALLOW and result.decision is not Decision.REQUIRE_APPROVAL:
                    return result, None
                if action is None:
                    raise AuthorizationError("Evaluation did not retain a validated action")
                if result.decision is Decision.REQUIRE_APPROVAL and not self._review_action(action, result):
                    return result, None

                authorization = Authorization(
                    authorization_id=uuid4(), action=action, state=AuthorizationState.USABLE,
                )
                self._write(AuditEvent(
                    action_id=action.action_id, caller=action.caller,
                    event_type=AuditEventType.AUTHORIZATION_ISSUED,
                    details={"authorization_id": str(authorization.authorization_id),
                             "tool_name": self._audit_tool_name(action),
                             "target": _audit_identifier(action.target)},
                ))
                reference = _Reference()
                self._publish(reference, authorization)
                self._check_reentrancy()
            return result, reference
        except BaseException:
            # Also cover interruption and partial publication. The audit may
            # already say 'issued'; it cannot reconstruct registry authority.
            if reference is not None:
                self._records.pop(reference, None)
            raise

    def consume(self, reference: object) -> ActionRequest:
        """Execution-side only: irreversibly consume and return the stored action.

        No handler is invoked and no execution-start event is fabricated here.
        The executor MUST write execution_started successfully after consumption
        and before dispatch, using the shared guarded execution scope. Failure
        must never restore this reference.
        """
        with self._operation():
            return self._consume(reference)

    @contextmanager
    def _execution_scope(self, reference: object):
        """Executor-only scope: hold the same guard through dispatch and auditing."""
        with self._operation():
            yield self._consume(reference)

    def _consume(self, reference: object) -> ActionRequest:
        """Called only while the shared operation guard is held."""
        # Exact type avoids attacker-controlled hash/equality callbacks.
        entry = self._records.get(reference) if type(reference) is _Reference else None
        if entry is None or entry.consumed:
            action = entry.authorization.action if entry is not None else None
            self._write(AuditEvent(
                action_id=action.action_id if action is not None else uuid4(),
                caller=action.caller if action is not None else None,
                event_type=AuditEventType.EXECUTION_BLOCKED,
                details={"reason_code": "AUTHORIZATION_CONSUMED" if entry is not None
                         else "UNKNOWN_AUTHORIZATION"},
            ))
            raise InvalidReferenceError("Authorization is unknown or already consumed")
        # Burn before constructing the replacement record or returning the
        # action. If either fails, the entry still cannot be used again.
        entry.consumed = True
        entry.authorization = replace(entry.authorization, state=AuthorizationState.CONSUMED)
        return entry.authorization.action
