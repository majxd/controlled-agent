"""Host orchestration; no handler access, policy evaluation, or independent audit."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid4

from .authorization import ApprovalError, AuditWriteError, AuthorizationService
from .contracts import CallerContext, Decision, JSONValue
from .executor import ExecutionError, ExecutionOutcome, ExecutionReport, Executor, RejectionReason


class Outcome(StrEnum):
    MALFORMED = "malformed"
    DENIED = "denied"
    APPROVAL_NOT_GRANTED = "approval_not_granted"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected_without_mutation"
    UNCERTAIN = "possibly_partial"
    BLOCKED = "blocked"
    AUDIT_FAILED = "audit_failed"
    REVIEW_FAILED = "review_failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class ControllerReport:
    """No references or exceptions. Result is immutable, untrusted tool data.

    Renderers must allowlist result fields, never dump this record or use its
    data as authority. execution_outcome remains separate from audit failure.
    """

    action_id: UUID
    outcome: Outcome
    decision: Decision | None = None
    execution_outcome: ExecutionOutcome | None = None
    audit_failed: bool = False
    stale_baseline: bool = False
    terminal: bool = False
    result: Mapping[str, JSONValue] | None = None


def _execution_report(action_id, report: ExecutionReport, decision, *, failed=False):
    outcome = {
        ExecutionOutcome.NO_DISPATCH: Outcome.BLOCKED,
        ExecutionOutcome.SUCCEEDED: Outcome.SUCCEEDED,
        ExecutionOutcome.REJECTED: Outcome.REJECTED,
        ExecutionOutcome.UNCERTAIN: Outcome.UNCERTAIN,
    }[report.outcome]
    # No-mutation describes effects, not successful completion processing.
    normal_rejection = (
        report.outcome is ExecutionOutcome.REJECTED and report.completion_audited
        and report.reason_code in RejectionReason
    )
    return ControllerReport(
        action_id=action_id, outcome=Outcome.AUDIT_FAILED if report.audit_failed else outcome,
        decision=decision, execution_outcome=report.outcome, audit_failed=report.audit_failed,
        stale_baseline=report.reason_code == "STALE_BASELINE",
        terminal=(report.audit_failed or not report.completion_audited
                  or (failed and not normal_rejection)),
        result=report.result,
    )


class Controller:
    """One configured caller and matching service/executor per sequential run."""

    def __init__(self, *, authorization: AuthorizationService, executor: Executor,
                 caller: CallerContext):
        if (type(authorization) is not AuthorizationService or type(executor) is not Executor
                or executor._authorization is not authorization or type(caller) is not CallerContext):
            raise TypeError("Controller requires matching trusted components")
        self._authorization = authorization
        self._executor = executor
        self._caller = caller

    def submit(self, proposal: object) -> ControllerReport:
        action_id = uuid4()  # Before inspecting any proposal fields.
        decision = None
        dispatch_attempted = False
        report = None
        try:
            result, reference = self._authorization.issue_proposal(
                proposal, action_id=action_id, caller=self._caller,
            )
            decision = result.decision
            if reference is None:
                if decision is Decision.DENY:
                    outcome = (Outcome.MALFORMED if result.reason_code == "MALFORMED_PROPOSAL"
                               else Outcome.DENIED)
                else:
                    outcome = Outcome.APPROVAL_NOT_GRANTED
                return ControllerReport(action_id=action_id, outcome=outcome, decision=decision)
            # No external callback/output between issuance and execution.
            dispatch_attempted = True
            report = self._executor.execute(reference)
            return _execution_report(action_id, report, decision)
        except ExecutionError as error:
            return _execution_report(action_id, error.report, decision, failed=True)
        except AuditWriteError:
            return ControllerReport(action_id=action_id, outcome=Outcome.AUDIT_FAILED,
                                    decision=decision, audit_failed=True, terminal=True)
        except ApprovalError:
            return ControllerReport(action_id=action_id, outcome=Outcome.REVIEW_FAILED,
                                    decision=decision, terminal=True)
        except Exception:
            # Admission/host faults supply no new dispatched action. Executor
            # failures are separately caught above with their effect reports.
            if report is not None:
                return _execution_report(action_id, report, decision, failed=True)
            return ControllerReport(
                action_id=action_id, outcome=Outcome.UNCERTAIN if dispatch_attempted else Outcome.BLOCKED,
                execution_outcome=ExecutionOutcome.UNCERTAIN if dispatch_attempted else None,
                decision=decision, terminal=True,
            )
        except BaseException as error:
            execution = getattr(error, "execution_report", report)
            if type(execution) is ExecutionReport:
                report = _execution_report(action_id, execution, decision, failed=True)
            else:
                cause = error.__cause__
                failed_audit = (getattr(error, "_audit_write_interrupted", False)
                                or isinstance(cause, AuditWriteError)
                                or getattr(cause, "_audit_write_interrupted", False))
                report = ControllerReport(
                    action_id=action_id,
                    outcome=(Outcome.AUDIT_FAILED if failed_audit else
                             Outcome.UNCERTAIN if dispatch_attempted else Outcome.BLOCKED),
                    execution_outcome=ExecutionOutcome.UNCERTAIN if dispatch_attempted else None,
                    decision=decision, audit_failed=bool(failed_audit), terminal=True,
                )
            error.controller_report = report
            raise
