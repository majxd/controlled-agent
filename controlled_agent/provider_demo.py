"""One explicit proposal through the existing governed path; Mock by default."""

import argparse
from contextlib import ExitStack
import getpass
import importlib.util
import os
import sys
import termios
import warnings

from .approval import ApprovalReview, HumanApprovalAdapter
from .audit import JsonlAuditWriter
from .authorization import AuthorizationService
from .cli import InputTooLong, Terminal
from .contracts import CallerContext
from .controller import Controller, ControllerReport
from .demo import TOOLS, WORK_ORDERS, format_work_order_approval, scripted_proposal
from .demo_tools import FakeWorkOrderTools
from .executor import Executor
from .mock_provider import MockProvider
from .provider import ProposalAdapter, ProviderError, ProviderFailure, ProviderRequest
from .provider_host import ProviderHost


SCENARIOS = {"read": "1", "update": "2", "protected-update": "3"}


class DemoTerminal(Terminal):
    """Real input uses an unbuffered controlling TTY, never redirected stdin.

    tty_fd=None permits trusted injected text streams in offline tests only.
    Output remains borrowed and usable until operational cleanup is reported.
    """

    def __init__(self, source, output, *, tty_fd=None):
        super().__init__(source, output)
        self.tty_fd = tty_fd

    def fresh(self):
        if self.tty_fd is not None:
            flags = termios.tcgetattr(self.tty_fd)[3]
            if not flags & termios.ICANON or not flags & termios.ISIG:
                raise OSError("Interactive terminal required")
            termios.tcflush(self.tty_fd, termios.TCIFLUSH)

    def read(self):
        if self.tty_fd is None:
            return super().read()
        data = bytearray()
        while len(data) <= 256:
            char = os.read(self.tty_fd, 1)
            if not char:
                self.ended = True
                raise EOFError
            if char == b"\n":
                return data.decode("utf-8", errors="strict") + "\n"
            data.extend(char)
        self.ended = True
        raise InputTooLong("Terminal input exceeds limit")

    def review(self, review):
        # Validate bounded canonical display data, not the model's intended path.
        if type(review) is not ApprovalReview:
            raise ValueError("Unsupported review")
        action, change = review.action, review.change
        if (action.caller.caller_id != "demo_operator" or action.target not in WORK_ORDERS
                or action.tool_name != "update_work_order_status"
                or set(action.arguments) != {"work_order_id", "new_status"}
                or action.arguments["work_order_id"] != action.target
                or change != format_work_order_approval(action, WORK_ORDERS[action.target])):
            raise ValueError("Unsupported review")
        self.show(
            f"Review action {action.action_id}\nCaller: {action.caller.caller_id}\n"
            f"Tool: {action.tool_name}\nTarget: {action.target}\n"
            f"Arguments: work_order_id={action.target}, new_status={change.proposed_state['status']}\n"
            f"Fixed reviewed baseline: {change.current_state['status']}\n"
            f"Proposed status: {change.proposed_state['status']}\n"
            f"Intended effect: {change.intended_effect}"
        )
        # Flush queued/typeahead input AFTER complete display and flush. No
        # buffered reader can retain an earlier approval across this boundary.
        self.fresh()
        self.show("Enter approve or decline (one fresh response):")

    def key(self):
        if self.tty_fd is None or not os.isatty(self.tty_fd):
            raise OSError("Hidden terminal input required")
        self.fresh()
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("Groq key (hidden): ", stream=self.output)
        if (type(key) is not str or not 0 < len(key) <= 512 or not key.isascii()
                or any(char.isspace() for char in key)):
            raise ValueError("Hidden key input invalid")
        return key


def _groq_preflight():
    # Presence only. Never read/unset sensitive values or change logging.
    if "SSLKEYLOGFILE" in os.environ:
        raise ProviderError(ProviderFailure.UNAVAILABLE)
    from .groq_provider import _configuration_guard
    _configuration_guard()  # Existing header/logging guards, before SDK import.
    if not all(importlib.util.find_spec(name) for name in ("groq", "httpx")):
        raise ProviderError(ProviderFailure.UNAVAILABLE)


def _new_groq(key):
    _groq_preflight()  # Recheck after trusted terminal input, before construction.
    from .groq_provider import GroqProvider
    return GroqProvider(api_key=key)


def _render(terminal, report, no_action, stopped, notices):
    if report is not None:
        terminal.report(report)  # Existing allowlisted rendering, unchanged report.
    elif no_action:
        terminal.show("No action proposed; no submission or execution.")
    if stopped:
        terminal.show("Provider host stopped; no further invocation.")
    for notice in notices:
        terminal.show(notice)


def run_demo(*, audit_path, terminal, provider="mock", scenario="read") -> int:
    """Own one session; terminal/output are borrowed from the entry-point owner."""
    report = host = key = None
    no_action = False
    notices, cleanup = [], []
    code, interruption = 0, None
    try:
        if provider not in ("mock", "groq") or scenario not in SCENARIOS:
            raise ValueError("Invalid demo configuration")
        if provider == "groq":
            _groq_preflight()
        proposal = scripted_proposal(SCENARIOS[scenario])
        tool, arguments = proposal["tool_name"], proposal["arguments"]
        target = arguments["work_order_id"]
        prompt = (f"Read synthetic work order {target}." if scenario == "read" else
                  f"Propose setting synthetic work order {target} to status closed.")
        request = ProviderRequest(prompt=prompt, tools=(TOOLS[tool].definition,))
        audit_owner = ExitStack()
        cleanup.append(("Audit writer cleanup failed; prior effects remain.", audit_owner.close))
        writer = audit_owner.enter_context(JsonlAuditWriter(audit_path))
        service = AuthorizationService(
            tools=TOOLS, permissions={"demo_operator": frozenset({tool})},
            resources=WORK_ORDERS, audit_write=writer,
            approval_formatter=format_work_order_approval,
            human_approval=HumanApprovalAdapter(display=terminal.review, read_response=terminal.read),
        )
        fixture = FakeWorkOrderTools(authorization=service)
        executor = Executor(authorization=service, handlers=fixture.handlers)
        controller = Controller(authorization=service, executor=executor,
                                caller=CallerContext(caller_id="demo_operator"))
        if provider == "groq":
            terminal.show("Groq selected: one live synthetic request; no retry or fallback.")
            key = terminal.key()
            source = _new_groq(key)
            cleanup.append(("Provider cleanup failed; prior effects remain.", source.close))
            key = None
        else:
            source = MockProvider(proposal)  # No owned resources/close interface.
        host = ProviderHost(controller=controller, adapter=ProposalAdapter(source))
        report = host.run_once(request)
        no_action = report is None
        if host.stopped or (report is not None and report.terminal):
            code = 1
    except BaseException as error:
        attached = getattr(error, "controller_report", None)
        if type(attached) is ControllerReport:
            report = attached  # Retain actual effects before any cleanup.
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            interruption = error
            code = 130 if isinstance(error, KeyboardInterrupt) else 1
            notices.append("Interrupted; no automatic retry.")
        elif isinstance(error, ProviderError):
            reason = error.reason if type(error.reason) is ProviderFailure else ProviderFailure.UNAVAILABLE
            notices.append(f"Provider failure: {reason.value}. No automatic retry.")
            code = 1
        else:
            notices.append("Demo failed; no automatic retry.")
            code = 1
    finally:
        key = None
        for notice, close in reversed(cleanup):
            try:
                close()
            except BaseException as error:
                notices.append(notice)
                if interruption is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interruption = error
                    notices.append("Interrupted during cleanup; no automatic retry.")
                code = 130 if isinstance(interruption, KeyboardInterrupt) else 1
    try:
        _render(terminal, report, no_action, host is not None and host.stopped, notices)
    except BaseException as error:
        if interruption is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
            interruption = error
        code = 130 if isinstance(interruption, KeyboardInterrupt) else 1
        # A separate borrowed channel can still report known effects. Retrying
        # display is not retrying execution; neither provider nor audit is called.
        try:
            fallback = Terminal(None, sys.stderr)
            fallback.show("Output failed; prior effects remain. No automatic retry.")
            _render(fallback, report, no_action, host is not None and host.stopped, notices)
        except BaseException:
            pass  # Neither output channel is guaranteed to remain usable.
    return code


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's usual diagnostics echo arbitrary arguments, potentially keys.
        self.exit(2, "Invalid demo arguments; use --help.\n")


def main(argv=None) -> int:
    parser = _Parser(description="One-shot governed provider demo (Mock by default)", allow_abbrev=False)
    parser.add_argument("--audit-path", required=True, help="existing trusted parent required")
    parser.add_argument("--provider", choices=("mock", "groq"), default="mock")
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), default="read")
    args = parser.parse_args(argv)
    reader = None
    code = 2
    terminal = Terminal(None, sys.stdout)
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise OSError("Interactive terminal required")
        reader = open("/dev/tty", "rb", buffering=0)
        if not reader.isatty():
            raise OSError("Interactive terminal required")
        terminal = DemoTerminal(reader, sys.stdout, tty_fd=reader.fileno())
        terminal.fresh()
    except BaseException as error:
        code = 130 if isinstance(error, KeyboardInterrupt) else 1 if isinstance(error, SystemExit) else 2
        try:
            terminal.show("Interactive controlling terminal required.")
        except BaseException:
            pass
    else:
        code = run_demo(audit_path=args.audit_path, terminal=terminal,
                        provider=args.provider, scenario=args.scenario)
    finally:
        if reader is not None:
            try:
                reader.close()
            except BaseException as error:
                if code == 0:
                    code = 130 if isinstance(error, KeyboardInterrupt) else 1
                try:
                    terminal.show("Terminal cleanup failed; prior effects remain.")
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        terminal.show("Interrupted during terminal cleanup; no automatic retry.")
                except BaseException:
                    pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
