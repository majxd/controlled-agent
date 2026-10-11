"""Trusted local terminal wiring for three scripted proposals; no model loop."""

import argparse
import sys
from typing import TextIO

from .approval import ApprovalReview, HumanApprovalAdapter
from .audit import JsonlAuditWriter
from .authorization import AuthorizationService
from .contracts import CallerContext
from .controller import Controller, ControllerReport, Outcome
from .demo import PERMISSIONS, STATUSES, TOOLS, WORK_ORDERS, format_work_order_approval, scripted_proposal
from .demo_tools import FakeWorkOrderTools
from .executor import Executor, ExecutionOutcome


class InputTooLong(ValueError):
    pass


class Terminal:
    """Trusted streams; bounded reads never turn a truncated prefix into approval."""

    def __init__(self, source: TextIO, output: TextIO):
        self.source = source
        self.output = output
        self.ended = False

    def show(self, text: str) -> None:
        if self.output.write(text + "\n") != len(text) + 1:
            raise OSError("Incomplete terminal output")
        self.output.flush()

    def read(self) -> str:
        line = self.source.readline(258)
        if line == "":
            self.ended = True
            raise EOFError
        if len(line) > 257 or len(line.rstrip("\r\n")) > 256:
            self.ended = True
            raise InputTooLong("Terminal input exceeds limit")
        if not line.endswith("\n"):
            self.ended = True
            raise EOFError  # A partial EOF line cannot become approval.
        return line  # Service alone interprets approval; no normalization here.

    def review(self, review: ApprovalReview) -> None:
        action, change = review.action, review.change
        # Fixed demo formatter supplies bounded public identity and enum values.
        # Do not render proposal descriptions or arbitrary argument dictionaries.
        self.show(
            f"Review action {action.action_id}\nCaller: {action.caller.caller_id}\n"
            f"Tool: {action.tool_name}\nTarget: {action.target}\n"
            f"Fixed reviewed baseline: {change.current_state['status']}\n"
            f"Proposed status: {change.proposed_state['status']}\n"
            f"Intended effect: {change.intended_effect}\n"
            "Enter approve or decline (one response):"
        )

    def report(self, report: ControllerReport) -> None:
        messages = {
            Outcome.MALFORMED: "Malformed proposal denied; no execution.",
            Outcome.DENIED: "Governance denied the action; no execution.",
            Outcome.APPROVAL_NOT_GRANTED: "Approval not granted (declined/cancelled); no execution.",
            Outcome.SUCCEEDED: "Handler completed successfully.",
            Outcome.REJECTED: "Handler rejected the action without mutation.",
            Outcome.UNCERTAIN: "Handler failed; effects may be partial. No automatic retry.",
            Outcome.BLOCKED: "Execution blocked; no handler dispatched.",
            Outcome.REVIEW_FAILED: "Human review failed; no execution.",
            Outcome.AUDIT_FAILED: "Required audit failed; session stopped.",
        }
        text = f"Action {report.action_id}: {messages[report.outcome]}"
        if report.stale_baseline:
            text += " STALE_BASELINE: live status differs from the fixed reviewed baseline."
        if report.audit_failed:
            effect = {
                None: "No handler dispatched.",
                ExecutionOutcome.NO_DISPATCH: "No handler dispatched.",
                ExecutionOutcome.SUCCEEDED: "Handler completed; its effects remain.",
                ExecutionOutcome.REJECTED: "Handler rejected without mutation.",
                ExecutionOutcome.UNCERTAIN: "Handler effects may be partial or uncertain.",
            }[report.execution_outcome]
            text += " " + effect + " No automatic retry."
        elif report.terminal and report.execution_outcome is ExecutionOutcome.SUCCEEDED:
            text += " Subsequent processing failed; effects remain. Session stopped."
        elif report.terminal and report.execution_outcome is ExecutionOutcome.REJECTED:
            text += " Execution completion processing failed; session stopped. No automatic retry."
        # Tool output is data. Only known demo IDs/statuses may enter terminal text.
        if report.result is not None:
            target, status = report.result.get("work_order_id"), report.result.get("status")
            if type(target) is str and target in WORK_ORDERS and type(status) is str and status in STATUSES:
                text += f" Work order {target}: {status}."
        self.show(text)


def run_session(*, audit_path, source: TextIO, output: TextIO) -> int:
    """Host/test API with trusted streams. One complete application lifetime."""
    terminal = Terminal(source, output)
    last_report = None
    try:
        with JsonlAuditWriter(audit_path) as writer:
            service = AuthorizationService(
                tools=TOOLS, permissions=PERMISSIONS, resources=WORK_ORDERS, audit_write=writer,
                approval_formatter=format_work_order_approval,
                human_approval=HumanApprovalAdapter(display=terminal.review, read_response=terminal.read),
            )
            fixture = FakeWorkOrderTools(authorization=service)
            executor = Executor(authorization=service, handlers=fixture.handlers)
            controller = Controller(authorization=service, executor=executor,
                                    caller=CallerContext(caller_id="demo_operator"))
            while not terminal.ended:
                terminal.show("1 Read WO-1001 | 2 Close WO-1001 | 3 Attempt close WO-9001 | q Quit")
                try:
                    choice = terminal.read().strip()
                except EOFError:
                    break
                if choice == "q":
                    break
                proposal = scripted_proposal(choice)
                if proposal is None:
                    terminal.show("Invalid menu choice; no action submitted.")
                    continue
                last_report = controller.submit(proposal)
                terminal.report(last_report)
                if last_report.terminal:
                    return 1
        return 0
    except (KeyboardInterrupt, SystemExit) as error:
        report = getattr(error, "controller_report", last_report)
        text = "Interrupted; session stopped."
        if "Audit writer cleanup also failed" in getattr(error, "__notes__", ()):
            text += " Audit writer cleanup also failed."
        _safe_notice(terminal, text, report)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    except Exception as error:
        # Includes initialization, close, terminal I/O, and overlong menu input.
        # Prior effects must not be rewritten as a no-dispatch failure.
        text = "Session stopped by a storage or terminal error. No automatic retry."
        if "Audit writer cleanup also failed" in getattr(error, "__notes__", ()):
            text += " Audit writer cleanup also failed."
        _safe_notice(terminal, text, last_report)
        return 1


def _safe_notice(terminal, text, report=None):
    try:
        terminal.show(text)
        if report is not None:
            terminal.report(report)
    except Exception:
        pass  # Output itself may be broken; never retry the action or the audit.


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Controlled Agent: local scripted demo")
    parser.add_argument("--audit-path", default="var/audit.jsonl",
                        help="trusted local path; existing trusted parent required")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Interactive terminal input and output are required.", file=sys.stderr)
        return 2
    return run_session(audit_path=args.audit_path, source=sys.stdin, output=sys.stdout)
