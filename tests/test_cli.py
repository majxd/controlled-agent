"""Terminal demo integration with injected streams and real temporary JSONL."""

from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import DEFAULT, Mock, patch
from uuid import uuid4

from controlled_agent import cli
from controlled_agent.audit import JsonlAuditWriter
from controlled_agent.authorization import ReentrantCallError
from controlled_agent.contracts import AuditEventType as E
from controlled_agent.demo_tools import FakeWorkOrderTools
from controlled_agent.demo import scripted_proposal
from controlled_agent.executor import RejectionReason, ToolRejected


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "audit.jsonl"
        self.output = StringIO()
        self.fixtures = []
        def make_fixture(**kwargs):
            fixture = FakeWorkOrderTools(**kwargs)
            fixture.handlers = {key: Mock(wraps=value) for key, value in fixture.handlers.items()}
            self.fixtures.append(fixture)
            return fixture
        self.factory = patch("controlled_agent.cli.FakeWorkOrderTools", side_effect=make_fixture).start()
        self.addCleanup(patch.stopall)

    def run_cli(self, text=None, source=None):
        return cli.run_session(audit_path=self.path, source=source or StringIO(text or ""), output=self.output)

    def records(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def no_calls(self):
        for fixture in self.fixtures:
            for handler in fixture.handlers.values():
                handler.assert_not_called()
            self.assertEqual(fixture._statuses, {"WO-1001": "open", "WO-9001": "open"})

    def test_full_menu_one_lifetime_with_state_and_stale_rejection(self):
        self.assertEqual(self.run_cli("1\n2\napprove\n1\n2\napprove\n3\nq\n"), 0)
        self.factory.assert_called_once()
        fixture = self.fixtures[0]
        self.assertEqual(fixture.handlers["get_work_order"].call_count, 2)
        self.assertEqual(fixture.handlers["update_work_order_status"].call_count, 2)
        self.assertEqual(fixture._statuses, {"WO-1001": "closed", "WO-9001": "open"})
        text = self.output.getvalue()
        for fragment in ("WO-1001: open", "WO-1001: closed", "STALE_BASELINE", "Governance denied",
                         "Caller: demo_operator", "Tool: update_work_order_status", "Target: WO-1001",
                         "Fixed reviewed baseline: open", "Proposed status: closed", "Intended effect:"):
            self.assertIn(fragment, text)
        self.assertEqual(text.count("Enter approve or decline"), 2)
        self.assertNotIn("internal authorization reference", text)
        records = self.records()
        ids = {r["action_id"] for r in records}
        self.assertEqual(len(ids), 5)
        for action_id in ids:
            events = [r for r in records if r["action_id"] == action_id]
            self.assertEqual(events[0]["event_type"], "action_submitted")
            self.assertTrue(all(r["caller"] == "demo_operator" for r in events))
        self.assertNotIn("intended_effect", self.path.read_text())
        self.assertNotIn("new_status", self.path.read_text())

    def test_invalid_menu_quit_and_eof_never_submit_fake_actions(self):
        self.assertEqual(self.run_cli("bogus\napprove\n\nSECRET_MENU\nq\n"), 0)
        self.assertEqual(self.records(), [])
        self.assertNotIn("SECRET_MENU", self.output.getvalue())
        self.no_calls()
        self.assertEqual(self.run_cli(""), 0)
        self.assertEqual(self.records(), [])

    def test_reentrant_rejection_stops_before_next_menu_action(self):
        make_fixture = self.factory.side_effect
        services = []
        def configure(**kwargs):
            fixture = make_fixture(**kwargs)
            service = kwargs["authorization"]
            services.append(service)
            def reject(action):
                with self.assertRaises(ReentrantCallError):
                    service.issue_proposal(scripted_proposal("1"), action_id=uuid4(), caller=action.caller)
                error = ToolRejected(RejectionReason.INVALID_ACTION)
                error.args = ("SECRET_HANDLER",)
                raise error
            fixture.handlers["get_work_order"].side_effect = reject
            return fixture
        self.factory.side_effect = configure
        source = StringIO("1\n1\nq\n")
        self.assertEqual(self.run_cli(source=source), 1)
        self.assertEqual(source.tell(), 2)  # The next menu choice remains unread.
        self.factory.assert_called_once()
        fixture = self.fixtures[0]
        fixture.handlers["get_work_order"].assert_called_once()
        fixture.handlers["update_work_order_status"].assert_not_called()
        self.assertEqual(fixture._statuses, {"WO-1001": "open", "WO-9001": "open"})
        service = services[0]
        self.assertEqual(len(service._seen), 1)
        self.assertEqual(len(service._records), 1)
        self.assertTrue(next(iter(service._records.values())).consumed)
        self.assertEqual([r["event_type"] for r in self.records()], [
            "action_submitted", "governance_decision", "authorization_issued", "execution_started",
        ])
        text = self.output.getvalue()
        self.assertIn("Handler rejected the action without mutation.", text)
        self.assertIn("Execution completion processing failed; session stopped. No automatic retry.", text)
        for forbidden in ("SECRET_HANDLER", "Traceback", "internal authorization reference",
                          "Required audit failed", "effects may be partial"):
            self.assertNotIn(forbidden, text)

    def test_normally_audited_rejection_allows_next_menu_action(self):
        make_fixture = self.factory.side_effect
        def configure(**kwargs):
            fixture = make_fixture(**kwargs)
            fixture.handlers["get_work_order"].side_effect = [
                ToolRejected(RejectionReason.INVALID_ACTION), DEFAULT,
            ]
            return fixture
        self.factory.side_effect = configure
        self.assertEqual(self.run_cli("1\n1\nq\n"), 0)
        fixture = self.fixtures[0]
        self.assertEqual(fixture.handlers["get_work_order"].call_count, 2)
        fixture.handlers["update_work_order_status"].assert_not_called()
        self.assertEqual(fixture._statuses, {"WO-1001": "open", "WO-9001": "open"})
        records = self.records()
        first_id = records[0]["action_id"]
        first = [r for r in records if r["action_id"] == first_id]
        self.assertEqual(first[-1]["event_type"], "execution_failed")
        self.assertEqual(first[-1]["details"]["reason_code"], "INVALID_ACTION")
        self.assertEqual(records[-1]["event_type"], "execution_succeeded")
        self.assertEqual(len({r["action_id"] for r in records}), 2)
        self.assertNotIn("session stopped", self.output.getvalue())

    def test_decline_blank_invalid_and_eof_approval_never_dispatch(self):
        for response in ("decline\nq\n", "\nq\n", "APPROVE\nq\n", "", "approve"):
            with self.subTest(response=response):
                before = len(self.records()) if self.path.exists() else 0
                self.assertEqual(self.run_cli("2\n" + response), 0)
                fresh = self.records()[before:]
                self.assertEqual([r["event_type"] for r in fresh], [
                    "action_submitted", "governance_decision", "approval_requested", "approval_result",
                ])
                self.assertIn(fresh[-1]["details"]["outcome"], {"declined", "cancelled"})
                self.no_calls()

    def test_oversized_input_never_truncates_to_approval_or_reuses_remainder(self):
        self.assertEqual(self.run_cli("2\napprove" + " " * 260 + "\napprove\n1\n"), 1)
        self.no_calls()
        self.assertEqual(self.records()[-1]["details"]["outcome"], "error")
        self.assertNotIn("authorization_issued", self.path.read_text())

    def test_oversized_menu_input_stops_without_admission(self):
        self.assertEqual(self.run_cli("1" + " " * 300 + "\n2\napprove\n"), 1)
        self.assertEqual(self.records(), [])
        self.no_calls()

    def test_review_is_completely_flushed_before_one_fresh_response(self):
        class Output(StringIO):
            def flush(inner):
                inner.flushed = inner.getvalue()
        output = Output()
        self.output = output
        class Input(StringIO):
            def readline(inner, limit=-1):
                if inner.tell() == 2:
                    self.assertIn("Proposed status: closed", output.flushed)
                    self.assertIn("Intended effect:", output.flushed)
                    self.assertIn("Enter approve or decline", output.flushed)
                return super().readline(limit)
        self.assertEqual(self.run_cli(source=Input("2\napprove\nq\n")), 0)
        self.fixtures[0].handlers["update_work_order_status"].assert_called_once()

    def test_display_flush_failure_never_reads_approval_or_dispatches(self):
        class Output(StringIO):
            def flush(inner):
                if "Review action" in inner.getvalue():
                    raise OSError("SECRET_DISPLAY")
        self.output = Output()
        source = StringIO("2\napprove\n")
        self.assertEqual(self.run_cli(source=source), 1)
        self.assertEqual(source.tell(), 2)
        self.no_calls()
        self.assertNotIn("SECRET_DISPLAY", self.output.getvalue())
        self.assertEqual(self.records()[-1]["details"]["reason_code"], "REVIEW_FAILED")

    def test_keyboard_interrupt_at_menu_and_approval_preserved_as_exit_130(self):
        class Input(StringIO):
            def readline(inner, limit=-1):
                if inner.tell() == len(inner.getvalue()):
                    raise KeyboardInterrupt
                return super().readline(limit)
        for prefix in ("", "2\n"):
            with self.subTest(prefix=prefix):
                self.assertEqual(self.run_cli(source=Input(prefix)), 130)
                self.no_calls()
        self.assertEqual(self.records()[-1]["details"]["reason_code"], "INTERRUPTED")

    def test_initialization_failure_never_reads_or_builds_tools(self):
        source = Mock()
        with patch("controlled_agent.cli.JsonlAuditWriter", side_effect=OSError("SECRET_PATH")):
            self.assertEqual(self.run_cli(source=source), 1)
        source.readline.assert_not_called()
        self.factory.assert_not_called()
        self.assertNotIn("SECRET_PATH", self.output.getvalue())

    def test_system_exit_in_input_is_sanitized_and_never_authorizes(self):
        class Input(StringIO):
            def readline(inner, limit=-1):
                if inner.tell() == 2:
                    raise SystemExit("SECRET_EXIT")
                return super().readline(limit)
        self.assertEqual(self.run_cli(source=Input("2\n")), 1)
        self.no_calls()
        self.assertEqual(self.records()[-1]["details"]["reason_code"], "INTERRUPTED")
        self.assertNotIn("SECRET_EXIT", self.output.getvalue())

    def test_missing_parent_is_not_created_or_repaired(self):
        self.path = self.path / "missing" / "audit.jsonl"
        self.assertEqual(self.run_cli("1\nq\n"), 1)
        self.assertFalse(self.path.parent.exists())
        self.factory.assert_not_called()

    def test_close_failure_reports_error_without_erasing_completed_effect(self):
        close = JsonlAuditWriter.close
        def broken(writer):
            close(writer)
            raise OSError("SECRET_CLOSE")
        with patch.object(JsonlAuditWriter, "close", broken):
            self.assertEqual(self.run_cli("2\napprove\nq\n"), 1)
        fixture = self.fixtures[0]
        fixture.handlers["update_work_order_status"].assert_called_once()
        self.assertEqual(fixture._statuses["WO-1001"], "closed")
        self.assertIn("storage or terminal error", self.output.getvalue())
        self.assertNotIn("SECRET_CLOSE", self.output.getvalue())

    def test_interrupt_and_close_failure_do_not_mask_original_exit(self):
        close = JsonlAuditWriter.close
        def broken(writer):
            close(writer)
            raise OSError("SECRET_CLOSE")
        source = Mock()
        source.readline.side_effect = KeyboardInterrupt
        with patch.object(JsonlAuditWriter, "close", broken):
            self.assertEqual(self.run_cli(source=source), 130)
        self.no_calls()
        self.assertNotIn("SECRET_CLOSE", self.output.getvalue())
        self.assertIn("Audit writer cleanup also failed", self.output.getvalue())

    def test_real_fsync_failure_gates_before_and_after_effects(self):
        import os
        real_sync = os.fsync
        for gate in ("authorization_issued", "execution_started", "execution_succeeded"):
            self.path = self.path.with_name(gate + ".jsonl")
            def sync(fd):
                if self.records()[-1]["event_type"] == gate:
                    raise OSError("SECRET_SYNC")
                real_sync(fd)
            with patch("controlled_agent.audit.os.fsync", side_effect=sync):
                self.assertEqual(self.run_cli("2\napprove\n1\nq\n"), 1)
            fixture = self.fixtures[-1]
            calls = int(gate == "execution_succeeded")
            self.assertEqual(fixture.handlers["update_work_order_status"].call_count, calls)
            fixture.handlers["get_work_order"].assert_not_called()
            self.assertEqual(fixture._statuses["WO-1001"], "closed" if calls else "open")
        self.assertIn("Handler completed; its effects remain", self.output.getvalue())
        self.assertNotIn("SECRET_SYNC", self.output.getvalue())

    def test_output_failure_after_mutation_does_not_dispatch_again(self):
        class Output(StringIO):
            def write(inner, text):
                if "Handler completed" in text:
                    raise OSError("SECRET_OUTPUT")
                return super().write(text)
        self.output = Output()
        self.assertEqual(self.run_cli("2\napprove\n2\napprove\n"), 1)
        self.fixtures[0].handlers["update_work_order_status"].assert_called_once()
        self.assertEqual(self.fixtures[0]._statuses["WO-1001"], "closed")
        self.assertNotIn("SECRET_OUTPUT", self.output.getvalue())

    def test_result_rendering_never_dumps_unknown_fields_or_control_sequences(self):
        from uuid import uuid4
        from controlled_agent.controller import ControllerReport, Outcome
        report = ControllerReport(action_id=uuid4(), outcome=Outcome.SUCCEEDED,
                                  result={"status": "\x1b[2JSECRET", "work_order_id": "WO-1001", "secret": "SECRET"})
        cli.Terminal(StringIO(), self.output).report(report)
        self.assertNotIn("SECRET", self.output.getvalue())
        self.assertNotIn("\x1b", self.output.getvalue())

    def test_main_requires_interactive_terminal_and_exposes_no_auto_approve_flag(self):
        with patch("controlled_agent.cli.sys.stdin", StringIO()), \
             patch("controlled_agent.cli.sys.stdout", StringIO()), \
             patch("controlled_agent.cli.sys.stderr", StringIO()), \
             patch("controlled_agent.cli.run_session") as session:
            self.assertEqual(cli.main([]), 2)
            session.assert_not_called()
            with self.assertRaises(SystemExit) as caught:
                cli.main(["--approve"])
            self.assertEqual(caught.exception.code, 2)

    def test_main_accepts_trusted_audit_path_only_and_wires_interactive_streams(self):
        source, output = Mock(), Mock()
        source.isatty.return_value = output.isatty.return_value = True
        with patch("controlled_agent.cli.sys.stdin", source), patch("controlled_agent.cli.sys.stdout", output), \
             patch("controlled_agent.cli.run_session", return_value=0) as session:
            self.assertEqual(cli.main(["--audit-path", str(self.path)]), 0)
        session.assert_called_once_with(audit_path=str(self.path), source=source, output=output)


if __name__ == "__main__":
    unittest.main()
