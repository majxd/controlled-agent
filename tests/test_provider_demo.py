"""One-shot host wiring and real PTY input; all provider traffic is offline."""

from contextlib import ExitStack, redirect_stderr
from io import StringIO
import importlib.util
import getpass
import json
import logging
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import termios
import time
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from controlled_agent import provider_demo as demo
from controlled_agent.audit import JsonlAuditWriter
from controlled_agent.controller import Controller, Outcome
from controlled_agent.demo_tools import FakeWorkOrderTools
from controlled_agent.executor import ExecutionOutcome
from controlled_agent.mock_provider import MockProvider
from controlled_agent.provider import ProviderError, ProviderFailure
from controlled_agent.provider_host import ProviderHost


ROOT = Path(__file__).resolve().parents[1]
SDK_AVAILABLE = all(importlib.util.find_spec(name) for name in ("groq", "httpx"))
NETWORK = ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
           "socket.getnameinfo", "socket.create_connection", "socket.socket.connect",
           "socket.socket.connect_ex", "socket.socket.sendto", "socket.socket.sendmsg")
DENIED = ["action_submitted", "governance_decision"]
READ = DENIED + ["authorization_issued", "execution_started", "execution_succeeded"]
REVIEW = DENIED + ["approval_requested", "approval_result"]
UPDATE = REVIEW + READ[2:]


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.blockers = [self.enterContext(patch(name, side_effect=AssertionError("Network forbidden")))
                         for name in NETWORK]
        self.fixtures, self.hosts, self.reports, self.requests = [], [], [], []
        self.calls = 0
        self.behavior = None
        original_fixture = FakeWorkOrderTools
        def fixture(**kwargs):
            value = original_fixture(**kwargs)
            value.handlers = {k: Mock(wraps=v) for k, v in value.handlers.items()}
            if self.behavior:
                value.handlers["update_work_order_status"].side_effect = lambda a: self.behavior(value, a)
            self.fixtures.append(value)
            return value
        self.factory = self.enterContext(patch.object(demo, "FakeWorkOrderTools", side_effect=fixture))
        original_run = ProviderHost.run_once
        def run(host, request):
            self.hosts.append(host)
            try:
                report = original_run(host, request)
            except BaseException as error:
                self.reports.append(getattr(error, "controller_report", None))
                raise
            self.reports.append(report)
            return report
        self.enterContext(patch.object(ProviderHost, "run_once", run))
        original_propose = MockProvider.propose
        def propose(provider, request):
            self.requests.append(request)
            return original_propose(provider, request)
        self.enterContext(patch.object(MockProvider, "propose", propose))
        self.new_id = self.enterContext(patch("controlled_agent.controller.uuid4", wraps=uuid4))
        self.submit = self.enterContext(patch.object(Controller, "submit", autospec=True,
                                                     side_effect=Controller.submit))

    def tearDown(self):
        for blocker in self.blockers:
            blocker.assert_not_called()

    def run_demo(self, scenario="read", response="", *, provider="mock", terminal=None):
        self.calls += 1
        self.path = Path(self.temp) / f"{self.calls}.jsonl"
        self.output, self.errors = StringIO(), StringIO()
        self.terminal = terminal or demo.DemoTerminal(StringIO(response), self.output)
        with redirect_stderr(self.errors):
            return demo.run_demo(audit_path=self.path, terminal=self.terminal,
                                 provider=provider, scenario=scenario)

    def records(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def assert_lifecycle(self, expected):
        records = self.records()
        report = self.reports[-1]
        fields = {
            "action_submitted": {"submitted_tool_name"},
            "governance_decision": {"decision", "risk", "reason_code", "tool_name", "resolved_target", "action_validated"},
            "approval_requested": {"tool_name", "resolved_target"},
            "approval_result": {"tool_name", "resolved_target", "outcome", "reason_code"},
            "authorization_issued": {"tool_name", "target", "authorization_id"},
            "execution_started": {"tool_name", "target", "outcome", "reason_code"},
            "execution_succeeded": {"tool_name", "target", "outcome", "reason_code"},
        }
        self.assertEqual([r["event_type"] for r in records], expected)
        self.assertEqual({r["action_id"] for r in records}, {str(report.action_id)})
        for record in records:
            self.assertEqual(set(record), {"action_id", "timestamp", "event_type", "caller", "details"})
            self.assertEqual(record["caller"], "demo_operator")
            self.assertEqual(set(record["details"]), fields[record["event_type"]])
            UUID(record["action_id"])
        self.assertEqual(records[0]["details"], {"submitted_tool_name": None})
        if report.decision is not None:
            self.assertEqual(records[1]["details"]["decision"], report.decision.value)
        self.assertLessEqual(max(map(len, self.path.read_bytes().splitlines(keepends=True))), 4096)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def assert_effects(self, reads=0, updates=0, status="open"):
        fixture = self.fixtures[-1]
        self.assertEqual(fixture.handlers["get_work_order"].call_count, reads)
        self.assertEqual(fixture.handlers["update_work_order_status"].call_count, updates)
        self.assertEqual(fixture._statuses, {"WO-1001": status, "WO-9001": "open"})

    def test_default_read_uses_one_session_and_public_request(self):
        with ExitStack() as stack:
            constructors = [stack.enter_context(patch.object(demo, name, wraps=getattr(demo, name)))
                            for name in ("JsonlAuditWriter", "AuthorizationService", "Executor",
                                         "Controller", "ProposalAdapter", "ProviderHost", "MockProvider")]
            self.assertEqual(self.run_demo(), 0)
            for constructor in constructors:
                constructor.assert_called_once()
        self.factory.assert_called_once()
        self.submit.assert_called_once()
        self.new_id.assert_called_once()
        self.assertEqual(len(self.hosts), 1)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.prompt, "Read synthetic work order WO-1001.")
        self.assertEqual(request.tools, (demo.TOOLS["get_work_order"].definition,))
        self.assert_lifecycle(READ)
        self.assert_effects(reads=1)
        self.assertIn("Work order WO-1001: open", self.output.getvalue())
        self.assertFalse(hasattr(MockProvider(), "close"))

    def test_approved_update_and_decline_use_actual_human_response(self):
        for response, expected, updates in ((" approve \n", UPDATE, 1), ("decline\n", REVIEW, 0)):
            with self.subTest(response=response):
                self.assertEqual(self.run_demo("update", response), 0)
                self.assert_lifecycle(expected)
                self.assertEqual(self.reports[-1].decision.value, "REQUIRE_APPROVAL")
                self.assert_effects(updates=updates, status="closed" if updates else "open")
                service = self.hosts[-1]._controller._authorization
                self.assertIsNone(service._pending)
                self.assertEqual(len(service._records), updates)
                self.assertTrue(all(entry.consumed for entry in service._records.values()))
                for entry in service._records.values():
                    self.assertEqual(entry.authorization.action.action_id, self.reports[-1].action_id)
                    self.assertEqual(entry.authorization.action.arguments,
                                     {"work_order_id": "WO-1001", "new_status": "closed"})
                self.assertEqual(self.records()[3]["details"]["outcome"], "approved" if updates else "declined")
                text = self.output.getvalue()
                for field in ("Caller: demo_operator", "Target: WO-1001", "new_status=closed",
                              "Fixed reviewed baseline: open", "Proposed status: closed", "Intended effect:"):
                    self.assertIn(field, text)

    def test_protected_update_denied_without_review(self):
        with patch.object(demo.DemoTerminal, "review") as review, patch.object(demo.DemoTerminal, "read") as read:
            self.assertEqual(self.run_demo("protected-update"), 0)
        review.assert_not_called()
        read.assert_not_called()
        self.assert_lifecycle(DENIED)
        self.assertEqual(self.records()[1]["details"]["reason_code"], "PROTECTED_TARGET")
        self.assert_effects()

    def test_no_action_and_provider_failures_never_submit_or_retry(self):
        sources = [MockProvider()] + [MockProvider(failure=reason) for reason in ProviderFailure]
        sources += [MockProvider("no_action"), MockProvider({"approved": True})]
        for source in sources:
            with self.subTest(source=source), patch.object(demo, "MockProvider", return_value=source):
                before = len(self.requests)
                code = self.run_demo()
                self.assertEqual(code, 0 if source is sources[0] else 1)
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.records(), [])
                self.assert_effects()
                service = self.hosts[-1]._controller._authorization
                self.assertFalse(service._records)
                self.assertFalse(service._seen)
        self.submit.assert_not_called()
        self.new_id.assert_not_called()

    def test_semantics_remain_governance_owned_and_permissions_are_narrow(self):
        for proposal in ({"tool_name": "unknown", "arguments": {}},
                         {"tool_name": "get_work_order", "arguments": {"work_order_id": "unknown"}},
                         demo.scripted_proposal("2")):
            with self.subTest(proposal=proposal), patch.object(demo, "MockProvider", return_value=MockProvider(proposal)):
                self.assertEqual(self.run_demo(), 0)
                self.assert_lifecycle(DENIED)
                self.assert_effects()
        self.assertEqual(self.records()[1]["details"]["reason_code"], "PERMISSION_DENIED")

    def test_different_valid_proposal_is_not_repaired_or_forced_to_scenario(self):
        proposal = demo.scripted_proposal("2")
        proposal["arguments"]["new_status"] = "in_progress"
        with patch.object(demo, "MockProvider", return_value=MockProvider(proposal)):
            self.assertEqual(self.run_demo("protected-update", "approve\n"), 0)
        self.assert_effects(updates=1, status="in_progress")
        self.assertIn("new_status=in_progress", self.output.getvalue())

    def test_review_input_eof_partial_empty_and_oversize_fail_closed(self):
        for response, code in (("", 0), ("approve", 0), ("\n", 0), ("APPROVE\n", 0),
                               ("approve" + " " * 260 + "\napprove\n", 1)):
            with self.subTest(response=response):
                self.assertEqual(self.run_demo("update", response), code)
                self.assert_lifecycle(REVIEW)
                self.assert_effects()
                self.assertNotIn("authorization_issued", self.path.read_text())

    def test_review_flush_failure_never_reads_or_dispatches(self):
        output = StringIO()
        terminal = demo.DemoTerminal(StringIO("approve\n"), output)
        def flush():
            if "Review action" in output.getvalue():
                raise OSError("PRIVATE_DISPLAY")
        output.flush = flush
        with patch.object(terminal, "read", wraps=terminal.read) as read:
            self.assertEqual(self.run_demo("update", terminal=terminal), 1)
        read.assert_not_called()
        self.assert_effects()
        self.assertNotIn("PRIVATE_DISPLAY", self.errors.getvalue())

    def test_audit_failure_before_and_after_dispatch_preserves_effects(self):
        original = JsonlAuditWriter.__call__
        for gate in ("action_submitted", "authorization_issued", "execution_started", "execution_succeeded"):
            def write(writer, event):
                if event.event_type.value == gate:
                    raise OSError("PRIVATE_AUDIT")
                return original(writer, event)
            with self.subTest(gate=gate), patch.object(JsonlAuditWriter, "__call__", write):
                self.assertEqual(self.run_demo("update", "approve\n"), 1)
            completed = gate == "execution_succeeded"
            self.assert_effects(updates=int(completed), status="closed" if completed else "open")
            self.assertTrue(self.reports[-1].audit_failed)
            self.assertTrue(self.hosts[-1].stopped)
            self.assertNotIn("PRIVATE_AUDIT", self.output.getvalue())
            if completed:
                self.assertIn("Handler completed; its effects remain", self.output.getvalue())

    def test_handler_interrupt_and_partial_error_retain_attached_report(self):
        for error, code in ((KeyboardInterrupt(), 130), (SystemExit("PRIVATE_EXIT"), 1),
                            (ValueError("PRIVATE_HANDLER"), 1)):
            def behavior(fixture, action):
                fixture._statuses[action.target] = "closed"
                raise error
            self.behavior = behavior
            with self.subTest(kind=type(error)):
                self.assertEqual(self.run_demo("update", "approve\n"), code)
                self.assert_effects(updates=1, status="closed")
                self.assertEqual(self.reports[-1].execution_outcome, ExecutionOutcome.UNCERTAIN)
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    self.assertIs(error.controller_report, self.reports[-1])
                self.assertIn("effects may be partial", self.output.getvalue())
                self.assertNotIn("PRIVATE", self.output.getvalue())

    def test_cleanup_failure_retains_report_and_primary_interrupt(self):
        close = JsonlAuditWriter.close
        def broken(writer):
            close(writer)
            raise OSError("PRIVATE_CLOSE")
        with patch.object(JsonlAuditWriter, "close", broken):
            self.assertEqual(self.run_demo("update", "approve\n"), 1)
            self.assert_effects(updates=1, status="closed")
            self.assertIn("Handler completed successfully", self.output.getvalue())
            self.assertIn("Audit writer cleanup failed", self.output.getvalue())
            terminal = demo.DemoTerminal(StringIO(), StringIO())
            with patch.object(terminal, "read", side_effect=KeyboardInterrupt):
                self.assertEqual(self.run_demo("update", terminal=terminal), 130)
            self.assert_effects()

    def test_output_failure_uses_separate_channel_without_reexecution(self):
        output = StringIO()
        original = output.write
        def write(text):
            if "Handler completed" in text:
                raise OSError("PRIVATE_OUTPUT")
            return original(text)
        output.write = write
        terminal = demo.DemoTerminal(StringIO("approve\n"), output)
        self.assertEqual(self.run_demo("update", terminal=terminal), 1)
        self.assert_effects(updates=1, status="closed")
        self.assertEqual(len(self.hosts), 1)
        self.assertIn("Output failed", self.errors.getvalue())
        self.assertIn("Handler completed successfully", self.errors.getvalue())
        self.assertNotIn("PRIVATE_OUTPUT", self.errors.getvalue())

    def test_stopped_host_with_nonterminal_success_is_still_failure(self):
        original = ProviderHost.run_once
        def stop(host, request):
            result = original(host, request)
            host._stopped = True  # Existing host tests cover actual swallowed reentry.
            return result
        with patch.object(ProviderHost, "run_once", stop):
            self.assertEqual(self.run_demo(), 1)
        self.assertIn("Handler completed successfully", self.output.getvalue())
        self.assertIn("Provider host stopped", self.output.getvalue())

    def test_unsafe_environment_presence_stops_before_key_client_or_submission(self):
        for name in ("SSLKEYLOGFILE", "GROQ_LOG", "GROQ_CUSTOM_HEADERS"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "PRIVATE"}), \
                 patch.object(demo.DemoTerminal, "key") as key, patch.object(demo, "_new_groq") as client:
                self.assertEqual(self.run_demo(provider="groq"), 1)
                key.assert_not_called()
                client.assert_not_called()
                self.assertEqual(os.environ[name], "PRIVATE")
                self.assertFalse(self.path.exists())
                self.assertNotIn("PRIVATE", self.output.getvalue())
        self.submit.assert_not_called()

    def test_unsafe_logging_and_missing_sdk_do_not_prompt_or_change_logging(self):
        logger = logging.getLogger("groq")
        before = (logger.level, logger.handlers[:])
        with patch.object(logger, "isEnabledFor", return_value=True), patch.object(demo.DemoTerminal, "key") as key:
            self.assertEqual(self.run_demo(provider="groq"), 1)
            key.assert_not_called()
        self.assertEqual((logger.level, logger.handlers[:]), before)
        with patch.object(demo.importlib.util, "find_spec", return_value=None), \
             patch.object(demo.DemoTerminal, "key") as key:
            self.assertEqual(self.run_demo(provider="groq"), 1)
            key.assert_not_called()

    def test_ssl_guard_rechecked_after_key_input(self):
        terminal = demo.DemoTerminal(StringIO(), StringIO())
        def key():
            os.environ["SSLKEYLOGFILE"] = "PRIVATE"
            return "dummy"
        with patch.object(demo.importlib.util, "find_spec", return_value=object()), \
             patch.object(terminal, "key", side_effect=key), \
             patch("controlled_agent.groq_provider.GroqProvider") as client:
            self.assertEqual(self.run_demo(provider="groq", terminal=terminal), 1)
        client.assert_not_called()
        self.assertEqual(self.records(), [])

    def test_required_arguments_and_terminal_fail_before_resources(self):
        with redirect_stderr(StringIO()) as error:
            for args in ([], ["--audit-path", "x", "--key", "PRIVATE"], ["--audit", "x"]):
                with self.assertRaises(SystemExit) as caught:
                    demo.main(args)
                self.assertEqual(caught.exception.code, 2)
            self.assertNotIn("PRIVATE", error.getvalue())
        with patch.object(demo.sys, "stdin", StringIO()), patch.object(demo.sys, "stdout", StringIO()), \
             patch.object(demo, "run_demo") as runner:
            self.assertEqual(demo.main(["--audit-path", "x"]), 2)
            runner.assert_not_called()

    def test_missing_audit_parent_prevents_provider_and_key_access(self):
        terminal = demo.DemoTerminal(StringIO(), StringIO())
        with patch.object(terminal, "key") as key, patch.object(demo, "_new_groq") as source, \
             patch.object(demo, "_groq_preflight"):
            code = demo.run_demo(audit_path=Path(self.temp) / "missing" / "audit.jsonl",
                                 terminal=terminal, provider="groq")
        self.assertEqual(code, 1)
        self.factory.assert_not_called()
        key.assert_not_called()
        source.assert_not_called()
        self.assertFalse((Path(self.temp) / "missing").exists())

    def test_main_defaults_and_terminal_close_failure_preserve_prior_result(self):
        reader = Mock()
        reader.isatty.return_value = True
        reader.close.side_effect = OSError("PRIVATE_CLOSE")
        source, output = Mock(), StringIO()
        source.isatty.return_value = True
        output.isatty = lambda: True
        with patch.object(demo.sys, "stdin", source), patch.object(demo.sys, "stdout", output), \
             patch("builtins.open", return_value=reader) as opening, \
             patch.object(demo.DemoTerminal, "fresh"), patch.object(demo, "run_demo", return_value=0) as run:
            self.assertEqual(demo.main(["--audit-path", "trusted.jsonl"]), 1)
        opening.assert_called_once_with("/dev/tty", "rb", buffering=0)
        self.assertEqual(run.call_args.kwargs["provider"], "mock")
        self.assertEqual(run.call_args.kwargs["scenario"], "read")
        reader.close.assert_called_once()
        self.assertIn("Terminal cleanup failed", output.getvalue())
        self.assertNotIn("PRIVATE_CLOSE", output.getvalue())

    def test_dependency_free_import_and_mock_session_have_no_sdk(self):
        program = '''
import sys, tempfile, pathlib, io
from unittest.mock import patch
def block(event, args):
    if event == 'import' and args[0].split('.')[0] in ('groq', 'httpx'):
        raise AssertionError('SDK import forbidden')
    if event.startswith('socket.'):
        raise AssertionError('Network forbidden')
sys.addaudithook(block)
from controlled_agent.provider_demo import run_demo, DemoTerminal
with tempfile.TemporaryDirectory() as folder:
    with patch('getpass.getpass', side_effect=AssertionError('Key forbidden')):
        assert run_demo(audit_path=pathlib.Path(folder)/'audit.jsonl',
                        terminal=DemoTerminal(io.StringIO(), io.StringIO())) == 0
assert 'groq' not in sys.modules and 'httpx' not in sys.modules
'''
        result = subprocess.run([sys.executable, "-B", "-S", "-c", program], cwd=ROOT,
                                env={}, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    @unittest.skipUnless(SDK_AVAILABLE, "optional groq/httpx extra not installed")
    def test_actual_sdk_one_attempt_failure_mapping_and_cleanup(self):
        import httpx
        from controlled_agent.groq_provider import GroqProvider
        responses = [(200, "proposal", None), (200, "no_action", None),
                     (200, "refusal", "refusal"), (429, None, "rate_limit"),
                     (503, None, "unavailable"), (408, None, "timeout")]
        for status, kind, failure in responses:
            with self.subTest(status=status, kind=kind):
                requests = []
                def respond(request):
                    requests.append(request)
                    wire = {"type": kind}
                    if kind == "proposal":
                        wire["proposal"] = demo.scripted_proposal("1")
                    return httpx.Response(status, json={"choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(wire)}}]})
                transport = httpx.MockTransport(respond)
                transport.close = Mock(wraps=transport.close)
                with patch.object(demo.DemoTerminal, "key", return_value="dummy-demo-key"), \
                     patch.object(demo, "_new_groq", side_effect=lambda key: GroqProvider(api_key=key, transport=transport)):
                    self.assertEqual(self.run_demo(provider="groq"), int(failure is not None))
                transport.close.assert_called_once()
                self.assertEqual(len(requests), 1)
                body = json.loads(requests[0].content)
                self.assertEqual(body["reasoning_effort"], "low")
                self.assertEqual(body["max_completion_tokens"], 2048)
                self.assertFalse(body["include_reasoning"])
                self.assertNotIn("reasoning_format", body)
                for secret in ("dummy-demo-key", "demo_operator", "protected", "critical"):
                    self.assertNotIn(secret, requests[0].content.decode())
                self.assertNotIn("dummy-demo-key", self.output.getvalue() + self.path.read_text())
                if failure:
                    self.assertIn(f"Provider failure: {failure}", self.output.getvalue())
                if kind != "proposal":
                    self.assertEqual(self.records(), [])
                    self.assert_effects()
                else:
                    self.assert_lifecycle(READ)
                    self.assert_effects(reads=1)

    @unittest.skipUnless(SDK_AVAILABLE, "optional groq/httpx extra not installed")
    def test_sdk_cleanup_failure_and_interrupt_preserve_completed_update(self):
        import httpx
        from controlled_agent.groq_provider import GroqProvider
        for cleanup_error, code in ((OSError("PRIVATE_CLOSE"), 1), (KeyboardInterrupt(), 130)):
            requests = []
            def respond(request):
                requests.append(request)
                return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps({
                        "type": "proposal", "proposal": demo.scripted_proposal("2")})}}]})
            transport = httpx.MockTransport(respond)
            close = transport.close
            def fail_close():
                close()
                raise cleanup_error
            transport.close = Mock(side_effect=fail_close)
            with self.subTest(kind=type(cleanup_error)), \
                 patch.object(demo.DemoTerminal, "key", return_value="dummy-demo-key"), \
                 patch.object(demo, "_new_groq", side_effect=lambda key: GroqProvider(api_key=key, transport=transport)), \
                 patch.object(JsonlAuditWriter, "close", autospec=True, side_effect=JsonlAuditWriter.close) as audit_close:
                self.assertEqual(self.run_demo("update", "approve\n", provider="groq"), code)
            self.assertEqual(len(requests), 1)
            transport.close.assert_called_once()
            audit_close.assert_called_once()
            self.assertTrue(audit_close.call_args.args[0]._closed)
            self.assert_lifecycle(UPDATE)
            self.assert_effects(updates=1, status="closed")
            self.assertEqual(self.reports[-1].outcome, Outcome.SUCCEEDED)
            self.assertIn("Handler completed successfully", self.output.getvalue())
            self.assertIn("Provider cleanup failed", self.output.getvalue())
            self.assertNotIn("PRIVATE_CLOSE", self.output.getvalue())


@unittest.skipUnless(os.name == "posix", "controlling terminal requires POSIX")
class PtyTests(unittest.TestCase):
    def run_pty(self, mode):
        import errno
        import pty
        with TemporaryDirectory() as folder:
            audit = str(Path(folder) / "audit.jsonl")
            # Pipes provide a deterministic barrier: input is queued while the
            # provider is paused, then the real review performs its input flush.
            ready_r, ready_w = os.pipe()
            go_r, go_w = os.pipe()
            pid, master = pty.fork()
            if pid == 0:
                os.close(ready_r)
                os.close(go_w)
                try:
                    with ExitStack() as stack:
                        stack.enter_context(patch.dict(os.environ, {}, clear=True))
                        for name in NETWORK:
                            stack.enter_context(patch(name, side_effect=AssertionError("Network forbidden")))
                        if mode.startswith("key"):
                            reader = open("/dev/tty", "rb", buffering=0)
                            terminal = demo.DemoTerminal(reader, sys.stdout, tty_fd=reader.fileno())
                            if mode == "key_warning":
                                stack.enter_context(patch("termios.tcsetattr", side_effect=termios.error("PRIVATE")))
                            try:
                                key = terminal.key()
                                print("KEY_OK" if key == "dummy-pty-key" else "KEY_BAD", flush=True)
                            except getpass.GetPassWarning:
                                print("KEY_REJECTED", flush=True)
                            finally:
                                reader.close()
                            os._exit(0)
                        original = MockProvider.propose
                        def paused(provider, request):
                            os.write(ready_w, b"ready")
                            os.read(go_r, 1)
                            return original(provider, request)
                        stack.enter_context(patch.object(MockProvider, "propose", paused))
                        code = demo.main(["--audit-path", audit, "--scenario", "update"])
                        print(f"EXIT={code}", flush=True)
                    os._exit(0)
                except BaseException:
                    os._exit(90)  # No raw exception diagnostics, even in PTY tests.
            os.close(ready_w)
            os.close(go_r)
            captured = b""
            deadline = time.monotonic() + 15
            sent = False
            try:
                if not mode.startswith("key"):
                    self.assertTrue(select.select([ready_r], [], [], 10)[0])
                    self.assertEqual(os.read(ready_r, 5), b"ready")
                    if mode == "pretyped":
                        os.write(master, b"approve\n")
                        # Wait for terminal echo, proving the input was accepted
                        # before allowing the provider to return and review start.
                        while b"approve\r\n" not in captured:
                            self.assertTrue(select.select([master], [], [], 5)[0])
                            captured += os.read(master, 4096)
                    os.write(go_w, b"x")
                while time.monotonic() < deadline:
                    if not select.select([master], [], [], 0.1)[0]:
                        continue
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    captured += chunk
                    if not sent and b"Groq key (hidden): " in captured:
                        self.assertFalse(termios.tcgetattr(master)[3] & termios.ECHO)
                        os.write(master, b"dummy-pty-key\n")
                        sent = True
                    if not sent and b"Enter approve or decline (one fresh response):" in captured:
                        self.assertIn(b"Arguments: work_order_id=WO-1001, new_status=closed", captured)
                        self.assertIn(b"Intended effect:", captured)
                        answer = {"pretyped": b"decline\n", "approve": b"approve\n",
                                  "eof": b"\x04", "partial": b"approve\x04\x04",
                                  "oversize": b"approve" + b" " * 260 + b"\n",
                                  "interrupt": b"\x03"}[mode]
                        os.write(master, answer)
                        sent = True
                else:
                    self.fail("PTY timeout")
                _, status = os.waitpid(pid, 0)
                pid = None
                self.assertEqual(os.waitstatus_to_exitcode(status), 0, "PTY child failed")
            finally:
                if pid is not None:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                for fd in (master, ready_r, go_w):
                    os.close(fd)
            self.assertNotIn(b"dummy-pty-key", captured)
            self.assertNotIn(b"PRIVATE", captured)
            self.assertNotIn(b"Traceback", captured)
            records = [json.loads(line) for line in Path(audit).read_text().splitlines()] if Path(audit).exists() else []
            return captured, records

    def test_real_pty_discards_pretyped_approval(self):
        captured, records = self.run_pty("pretyped")
        self.assertIn(b"EXIT=0", captured)
        self.assertEqual([r["event_type"] for r in records], REVIEW)
        self.assertEqual(records[-1]["details"]["outcome"], "declined")

    def test_real_pty_fresh_approval_eof_oversize_and_interrupt(self):
        for mode in ("approve", "eof", "partial", "oversize", "interrupt"):
            with self.subTest(mode=mode):
                captured, records = self.run_pty(mode)
                self.assertEqual([r["event_type"] for r in records], UPDATE if mode == "approve" else REVIEW)
                self.assertIn(f"EXIT={130 if mode == 'interrupt' else 1 if mode == 'oversize' else 0}".encode(), captured)

    def test_real_pty_hidden_key_and_warning_rejection(self):
        captured, records = self.run_pty("key")
        self.assertIn(b"KEY_OK", captured)
        self.assertEqual(records, [])
        captured, records = self.run_pty("key_warning")
        self.assertIn(b"KEY_REJECTED", captured)
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
