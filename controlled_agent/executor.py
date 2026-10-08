"""Sequential dispatch from service-issued references, never from action claims.

Handlers and the synchronous audit writer are trusted host configuration. Reports
are data, not execution authority. Host exceptions may retain private diagnostic
causes; do not expose their tracebacks or result payloads as audit data.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from .authorization import AuthorizationService, AuditWriteError, ReentrantCallError, _audit_identifier
from .contracts import ActionRequest, AuditEvent, AuditEventType, JSONValue, _freeze_mapping


class ExecutionOutcome(StrEnum):
    NO_DISPATCH = "no_dispatch"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected_without_mutation"
    UNCERTAIN = "possibly_partial"


class RejectionReason(StrEnum):
    INVALID_ACTION = "INVALID_ACTION"
    PROTECTED_TARGET = "PROTECTED_TARGET"
    STALE_BASELINE = "STALE_BASELINE"


class ToolRejected(RuntimeError):
    """Trusted handler guarantee: rejected before any mutation.

    A handler must never raise this after effects. Other exceptions carry no
    no-mutation guarantee. Only fixed reason codes may enter execution audits.
    """

    def __init__(self, reason: RejectionReason):
        if type(reason) is not RejectionReason:
            raise TypeError("A fixed rejection reason is required")
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionReport:
    """Host-facing outcome data; result is never automatically audited."""

    action_id: UUID | None
    outcome: ExecutionOutcome
    reason_code: str
    result: Mapping[str, JSONValue] | None = None
    completion_audited: bool = False
    audit_failed: bool = False

    def __post_init__(self) -> None:
        if self.result is not None:
            object.__setattr__(self, "result", _freeze_mapping(self.result, "result"))


class ExecutionError(RuntimeError):
    """Execution did not complete normally; inspect report, not exception text.

    A succeeded outcome can accompany failed completion auditing or detected
    reentrancy. No exception implies rollback. audit_error and chained causes
    are host-only diagnostics and can contain sensitive callback data.
    """

    def __init__(self, report: ExecutionReport, *, audit_error: BaseException | None = None,
                 handler_error: BaseException | None = None):
        self.report = report
        self.audit_error = audit_error
        self.handler_error = handler_error
        super().__init__(f"Execution stopped: {report.reason_code}")


class Executor:
    """One host-owned executor using one service and fixed synchronous handlers.

    execute accepts only a reference. Runtime provenance comes exclusively from
    the service registry, not Python types or immutable report/record objects.
    """

    def __init__(self, *, authorization: AuthorizationService,
                 handlers: Mapping[str, Callable[[ActionRequest], Mapping[str, JSONValue]]]):
        if type(authorization) is not AuthorizationService:
            raise TypeError("Executor requires the trusted AuthorizationService")
        copied = dict(handlers)
        if any(type(name) is not str or not name or not callable(handler)
               for name, handler in copied.items()):
            raise TypeError("Handlers must map tool names to trusted callables")
        self._authorization = authorization
        self._handlers = MappingProxyType(copied)

    def execute(self, reference: object) -> ExecutionReport:
        report = ExecutionReport(action_id=None, outcome=ExecutionOutcome.NO_DISPATCH,
                                 reason_code="AUTHORIZATION_REJECTED")
        audit_error = None
        handler_error = None
        consumption_completed = False

        def write(action, event_type):
            nonlocal report, audit_error
            try:
                self._authorization._write(AuditEvent(
                    action_id=action.action_id, caller=action.caller, event_type=event_type,
                    details={"tool_name": self._authorization._audit_tool_name(action),
                             "target": _audit_identifier(action.target),
                             "outcome": report.outcome.value,
                             "reason_code": report.reason_code},
                ))
            except BaseException as error:
                audit_error = error
                report = replace(report, audit_failed=True)
                raise

        try:
            # Shares the service guard even across callbacks into another executor.
            with self._authorization._execution_scope(reference) as action:
                consumption_completed = True
                report = replace(report, action_id=action.action_id)
                handler = self._handlers.get(action.tool_name)
                if handler is None:
                    report = replace(report, reason_code="HANDLER_UNAVAILABLE")
                    write(action, AuditEventType.EXECUTION_BLOCKED)
                    raise RuntimeError("No registered execution handler")

                report = replace(report, reason_code="EXECUTION_START")
                write(action, AuditEventType.EXECUTION_STARTED)
                self._authorization._check_reentrancy()
                # Once entering a handler, an exception may follow a partial effect.
                report = replace(report, outcome=ExecutionOutcome.UNCERTAIN,
                                 reason_code="HANDLER_FAILED")
                handler_error = None
                try:
                    result = handler(action)
                except ToolRejected as error:
                    handler_error = error
                    if type(error) is ToolRejected and type(error.reason) is RejectionReason:
                        report = replace(report, outcome=ExecutionOutcome.REJECTED,
                                         reason_code=error.reason.value)
                except BaseException as error:
                    handler_error = error
                    if not isinstance(error, Exception):
                        report = replace(report, reason_code="HANDLER_INTERRUPTED")
                else:
                    # Record completion before result copying: invalid result data
                    # cannot erase the fact that the handler returned normally.
                    report = replace(report, outcome=ExecutionOutcome.SUCCEEDED,
                                     reason_code="RESULT_INVALID")
                    try:
                        if not isinstance(result, Mapping):
                            raise TypeError("A handler must return a result mapping")
                        report = replace(report, result=result, reason_code="EXECUTED")
                    except BaseException as error:
                        handler_error = error

                # A poisoned operation invokes no further audit callbacks. Preserve
                # interruption identity if the handler was interrupted as well.
                try:
                    self._authorization._check_reentrancy()
                except ReentrantCallError as error:
                    report = replace(report, reason_code="REENTRANT_EXECUTION")
                    if handler_error is not None and not isinstance(handler_error, Exception):
                        raise handler_error from error
                    raise

                if handler_error is not None:
                    try:
                        write(action, AuditEventType.EXECUTION_FAILED)
                    except BaseException as error:
                        if not isinstance(handler_error, Exception):
                            raise handler_error from error
                        raise
                    report = replace(report, completion_audited=True)
                    raise handler_error

                write(action, AuditEventType.EXECUTION_SUCCEEDED)
                return replace(report, completion_audited=True)
        except BaseException as error:
            if not consumption_completed and audit_error is None and (
                isinstance(error, AuditWriteError) or getattr(error, "_audit_write_interrupted", False)
            ):
                # Only consumption-stage audit failures reach this fallback.
                # Later audit failures are captured by write(); a handler raising
                # the same exception type does not establish audit provenance.
                audit_error = error
                report = replace(report, audit_failed=True)
            if not isinstance(error, Exception):
                # Preserve KeyboardInterrupt/SystemExit rather than swallowing them.
                error.execution_report = report
                error.audit_error = audit_error
                error.handler_error = handler_error
                raise
            raise ExecutionError(report, audit_error=audit_error, handler_error=handler_error) from error
