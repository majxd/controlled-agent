"""Proposal admission and controller boundaries, including real JSONL gates."""

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from controlled_agent.approval import HumanApprovalAdapter
from controlled_agent.audit import JsonlAuditWriter
from controlled_agent.authorization import (
    AuditWriteError, AuthorizationService, DuplicateActionError, ReentrantCallError,
)
from controlled_agent.contracts import ActionRequest, AuditEventType as E, CallerContext, Decision
from controlled_agent.controller import Controller, Outcome
from controlled_agent.demo import TOOLS, PERMISSIONS, WORK_ORDERS, format_work_order_approval
from controlled_agent.demo_tools import FakeWorkOrderTools
from controlled_agent.executor import ExecutionOutcome, Executor, RejectionReason, ToolRejected
from controlled_agent.submission import MalformedProposal, parse_proposal


def proposal(tool="get_work_order", target="WO-1001"):
    args = {"work_order_id": target}
    if tool == "update_work_order_status":
        args["new_status"] = "closed"
    return {"tool_name": tool, "arguments": args}


class ParserTests(unittest.TestCase):
    def test_flat_scalars_are_copied_without_semantic_normalization(self):
        data = {"tool_name": "unknown", "arguments": {"s": "x", "i": 1, "f": 1.25, "b": True, "n": None}}
        name, args = parse_proposal(data)
        self.assertEqual(name, "unknown")
        self.assertEqual(args, data["arguments"])
        data["arguments"]["s"] = "changed"
        self.assertEqual(args["s"], "x")

    def test_envelope_and_authority_fields_rejected(self):
        for data in (None, [], "{}", {}, {"tool_name": "read"}, proposal() | {"target": "WO-9001"}):
            with self.subTest(data=data), self.assertRaises(MalformedProposal):
                parse_proposal(data)
        for key in ("caller", "action_id", "permissions", "policy", "risk", "decision", "approved",
                    "approval", "resources", "handler", "audit_path"):
            with self.subTest(key=key), self.assertRaises(MalformedProposal):
                parse_proposal(proposal() | {key: "SECRET"})

    def test_nested_cyclic_custom_and_nonfinite_data_rejected_without_hooks(self):
        class Hostile:
            def __str__(self):
                raise AssertionError("string hook")
        class CustomDict(dict):
            def items(self):
                raise AssertionError("mapping hook")
        cycle = []
        cycle.append(cycle)
        for value in (Hostile(), CustomDict(), [], {}, cycle, (), b"x", float("nan"), float("inf")):
            with self.subTest(value_type=type(value)), self.assertRaises(MalformedProposal):
                parse_proposal({"tool_name": "read", "arguments": {"field": value}})
        with self.assertRaises(MalformedProposal):
            parse_proposal(CustomDict(proposal()))

    def test_length_numeric_and_encoded_size_limits(self):
        cases = [
            {"tool_name": "x" * 129, "arguments": {}},
            {"tool_name": " ", "arguments": {}},
            {"tool_name": "x", "arguments": {"k" * 129: "v"}},
            {"tool_name": "x", "arguments": {"k": "v" * 257}},
            {"tool_name": "x", "arguments": {str(i): i for i in range(9)}},
            {"tool_name": "x", "arguments": {"k": 2 ** 63}},
            {"tool_name": "x", "arguments": {"k": -(2 ** 63) - 1}},
            {"tool_name": "x", "arguments": {"k": "\ud800"}},
            {"tool_name": "x", "arguments": {str(i): "é" * 256 for i in range(8)}},
        ]
        for data in cases:
            with self.subTest(tool_length=len(data["tool_name"])), self.assertRaises(MalformedProposal):
                parse_proposal(data)
        parse_proposal({"tool_name": "x" * 128, "arguments": {"k" * 128: "v" * 256, "n": -(2 ** 63)}})


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "audit.jsonl"
        self.writer = JsonlAuditWriter(self.path)
        self.addCleanup(self.writer.close)
        self.events = []
        self.fail = None
        self.failure = OSError("SECRET_AUDIT")
        def audit(event):
            self.events.append(event)
            self.writer(event)
            if event.event_type is self.fail:
                raise self.failure
        self.audit = audit
        self.read = Mock(return_value="approve")
        self.display = Mock()
        self.service = AuthorizationService(
            tools=TOOLS, permissions=PERMISSIONS, resources=WORK_ORDERS, audit_write=audit,
            approval_formatter=format_work_order_approval,
            human_approval=HumanApprovalAdapter(display=self.display, read_response=self.read),
        )
        # Mock's default return value is not a valid successful display.
        self.display.return_value = None
        self.fixture = FakeWorkOrderTools(authorization=self.service)
        self.handlers = {key: Mock(wraps=value) for key, value in self.fixture.handlers.items()}
        self.executor = Executor(authorization=self.service, handlers=self.handlers)
        self.caller = CallerContext(caller_id="demo_operator")
        self.controller = Controller(authorization=self.service, executor=self.executor, caller=self.caller)

    def admit(self, data, action_id=None):
        return self.service.issue_proposal(data, action_id=action_id or uuid4(), caller=self.caller)

    def request(self, action_id):
        return ActionRequest(action_id=action_id, caller=self.caller, **proposal())

    def no_effects(self):
        self.assertEqual(self.fixture._statuses, {"WO-1001": "open", "WO-9001": "open"})
        for handler in self.handlers.values():
            handler.assert_not_called()

    def test_host_id_and_caller_precede_parse_submission_is_first(self):
        action_id = uuid4()
        def parsing(data):
            self.assertIn(action_id, self.service._seen)
            self.assertEqual([e.event_type for e in self.events], [E.ACTION_SUBMITTED])
            self.assertEqual(self.events[0].action_id, action_id)
            self.assertIs(self.events[0].caller, self.caller)
            return parse_proposal(data)
        with patch("controlled_agent.controller.uuid4", return_value=action_id), \
             patch("controlled_agent.authorization.parse_proposal", side_effect=parsing):
            report = self.controller.submit(proposal())
        self.assertIs(report.outcome, Outcome.SUCCEEDED)
        self.assertEqual(report.action_id, action_id)
        self.assertEqual(self.events[0].details, {"submitted_tool_name": None})
        self.handlers["get_work_order"].assert_called_once()
        action = self.handlers["get_work_order"].call_args.args[0]
        self.assertIs(action.caller, self.caller)
        self.assertEqual(action.target, "WO-1001")

    def test_malformed_audited_without_fake_action_or_governance(self):
        with patch("controlled_agent.authorization.ActionRequest", side_effect=AssertionError("no fake record")), \
             patch("controlled_agent.authorization._evaluate_action", side_effect=AssertionError("no policy")):
            report = self.controller.submit({"approved": True})
        self.assertIs(report.outcome, Outcome.MALFORMED)
        self.assertEqual([e.event_type for e in self.events], [E.ACTION_SUBMITTED, E.GOVERNANCE_DECISION])
        self.assertTrue(all(e.action_id == report.action_id for e in self.events))
        self.assertEqual(dict(self.events[-1].details), {
            "decision": "DENY", "risk": None, "reason_code": "MALFORMED_PROPOSAL",
            "tool_name": None, "resolved_target": None, "action_validated": False,
        })
        self.no_effects()
        self.assertIn(report.action_id, self.service._seen)

    def test_shared_registry_rejects_duplicates_in_both_api_directions(self):
        for first in ("raw", "record", "malformed"):
            action_id = uuid4()
            if first == "record":
                _, ref = self.service.issue(self.request(action_id))
            else:
                _, ref = self.admit(None if first == "malformed" else proposal(), action_id)
            with self.assertRaises(DuplicateActionError):
                self.admit(proposal(), action_id)
            with self.assertRaises(DuplicateActionError):
                self.service.issue(self.request(action_id))
            if ref is not None:
                self.assertEqual(self.service.consume(ref).action_id, action_id)
        self.no_effects()

    def test_malformed_audit_failure_and_parser_interruption_reserve_id(self):
        for gate in (E.ACTION_SUBMITTED, E.GOVERNANCE_DECISION):
            action_id = uuid4()
            self.fail = gate
            with self.assertRaises(AuditWriteError):
                self.admit(None, action_id)
            self.fail = None
            with self.assertRaises(DuplicateActionError):
                self.admit(proposal(), action_id)
        action_id = uuid4()
        with patch("controlled_agent.authorization.parse_proposal", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.admit(proposal(), action_id)
        with self.assertRaises(DuplicateActionError):
            self.service.issue(self.request(action_id))
        self.no_effects()

    def test_submission_audit_failure_does_not_parse(self):
        self.fail = E.ACTION_SUBMITTED
        with patch("controlled_agent.authorization.parse_proposal") as parsing:
            report = self.controller.submit(proposal())
        parsing.assert_not_called()
        self.assertIs(report.outcome, Outcome.AUDIT_FAILED)
        self.assertTrue(report.terminal)
        self.no_effects()

    def test_proposal_cannot_supply_identity_target_configuration_or_approval(self):
        for key in ("caller", "action_id", "target", "permissions", "approved", "audit_path", "handler"):
            report = self.controller.submit(proposal() | {key: "SECRET"})
            self.assertIs(report.outcome, Outcome.MALFORMED)
        data = proposal("update_work_order_status", "WO-9001")
        data["arguments"].update(approved=True, risk="LOW", decision="ALLOW")
        report = self.controller.submit(data)
        self.assertIs(report.outcome, Outcome.DENIED)
        self.assertEqual(self.events[-1].details["risk"], "HIGH")
        self.assertFalse(self.events[-1].details["action_validated"])
        self.read.assert_not_called()
        self.no_effects()
        self.assertNotIn("SECRET", self.path.read_text())

    def test_approved_action_identity_and_single_run_stale_baseline(self):
        data = proposal("update_work_order_status")
        first = self.controller.submit(data)
        self.assertIs(first.outcome, Outcome.SUCCEEDED)
        action = self.display.call_args.args[0].action
        self.assertIs(action, self.handlers["update_work_order_status"].call_args.args[0])
        second = self.controller.submit(data)
        self.assertIs(second.outcome, Outcome.REJECTED)
        self.assertTrue(second.stale_baseline)
        self.assertNotEqual(first.action_id, second.action_id)
        self.assertEqual(self.handlers["update_work_order_status"].call_count, 2)
        read = self.controller.submit(proposal())
        self.assertEqual(read.result["status"], "closed")
        self.assertEqual(self.fixture._statuses["WO-9001"], "open")
        self.assertTrue(all(entry.consumed for entry in self.service._records.values()))

    def test_decline_cancel_and_denial_do_not_call_executor(self):
        with patch.object(self.executor, "execute") as execute:
            for response in ("decline", "", None, "APPROVE"):
                self.read.return_value = response
                report = self.controller.submit(proposal("update_work_order_status"))
                self.assertIs(report.outcome, Outcome.APPROVAL_NOT_GRANTED)
            report = self.controller.submit(proposal("update_work_order_status", "WO-9001"))
            self.assertIs(report.outcome, Outcome.DENIED)
        execute.assert_not_called()
        self.no_effects()

    def test_each_audit_gate_preserves_actual_handler_counts_and_effects(self):
        for gate in (E.ACTION_SUBMITTED, E.GOVERNANCE_DECISION, E.APPROVAL_REQUESTED,
                     E.APPROVAL_RESULT, E.AUTHORIZATION_ISSUED, E.EXECUTION_STARTED, E.EXECUTION_SUCCEEDED):
            self.fail = gate
            report = self.controller.submit(proposal("update_work_order_status"))
            self.assertIs(report.outcome, Outcome.AUDIT_FAILED)
            self.assertTrue(report.terminal)
            if gate is not E.EXECUTION_SUCCEEDED:
                self.no_effects()
            else:
                self.assertIs(report.execution_outcome, ExecutionOutcome.SUCCEEDED)
                self.handlers["update_work_order_status"].assert_called_once()
                self.assertEqual(self.fixture._statuses["WO-1001"], "closed")
        self.assertNotIn("SECRET_AUDIT", self.path.read_text())

    def test_real_writer_sync_failure_blocks_start_and_poisons_future_work(self):
        import os
        original = os.fsync
        def sync(fd):
            if self.events[-1].event_type is E.EXECUTION_STARTED:
                raise OSError("SECRET_SYNC")
            original(fd)
        with patch("controlled_agent.audit.os.fsync", side_effect=sync):
            report = self.controller.submit(proposal())
        self.assertIs(report.outcome, Outcome.AUDIT_FAILED)
        self.assertIs(report.execution_outcome, ExecutionOutcome.NO_DISPATCH)
        self.assertIs(self.controller.submit(proposal()).outcome, Outcome.AUDIT_FAILED)
        self.no_effects()

    def test_handler_partial_failure_and_dual_audit_failure_are_sanitized(self):
        def handler(action):
            self.fixture._statuses[action.target] = "closed"
            raise ValueError("SECRET_HANDLER")
        counted = Mock(side_effect=handler)
        executor = Executor(authorization=self.service, handlers={"get_work_order": counted})
        controller = Controller(authorization=self.service, executor=executor, caller=self.caller)
        self.fail = E.EXECUTION_FAILED
        report = controller.submit(proposal())
        self.assertIs(report.outcome, Outcome.AUDIT_FAILED)
        self.assertIs(report.execution_outcome, ExecutionOutcome.UNCERTAIN)
        self.assertEqual(self.fixture._statuses["WO-1001"], "closed")
        self.assertNotIn("SECRET", repr(report))
        self.assertNotIn("SECRET", self.path.read_text())
        counted.assert_called_once()

    def test_missing_handler_is_blocked_without_calls(self):
        empty = Executor(authorization=self.service, handlers={})
        controller = Controller(authorization=self.service, executor=empty, caller=self.caller)
        report = controller.submit(proposal())
        self.assertIs(report.outcome, Outcome.BLOCKED)
        self.assertIs(report.execution_outcome, ExecutionOutcome.NO_DISPATCH)
        self.assertTrue(report.terminal)
        self.no_effects()

    def test_incomplete_rejection_processing_is_terminal_without_false_effects_or_audit_failure(self):
        self.handlers["get_work_order"].side_effect = ToolRejected(RejectionReason.INVALID_ACTION)
        def fail_completion(record, **changes):
            if changes.get("completion_audited") is True:
                raise RuntimeError("SECRET_COMPLETION")
            return replace(record, **changes)
        # The audit succeeds, but recording its completion fails. No recovery
        # may be inferred from the handler's earlier no-mutation rejection.
        with patch("controlled_agent.executor.replace", side_effect=fail_completion):
            report = self.controller.submit(proposal())
        self.assertTrue(report.terminal)
        self.assertIs(report.outcome, Outcome.REJECTED)
        self.assertIs(report.execution_outcome, ExecutionOutcome.REJECTED)
        self.assertFalse(report.audit_failed)
        self.assertEqual(self.events[-1].event_type, E.EXECUTION_FAILED)
        self.handlers["get_work_order"].assert_called_once()
        self.handlers["update_work_order_status"].assert_not_called()
        self.assertEqual(self.fixture._statuses, {"WO-1001": "open", "WO-9001": "open"})
        self.assertTrue(next(iter(self.service._records.values())).consumed)
        self.assertNotIn("SECRET_COMPLETION", repr(report))

    def test_reentrant_proposal_from_audit_aborts_outer_even_when_caught(self):
        original = self.service._audit_write
        def writer(event):
            original(event)
            nested = self.controller.submit(proposal())
            self.assertIs(nested.outcome, Outcome.BLOCKED)
        self.service._audit_write = writer
        report = self.controller.submit(proposal())
        self.assertIs(report.outcome, Outcome.BLOCKED)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(len(self.service._seen), 1)
        self.assertEqual(self.service._records, {})
        self.no_effects()

    def test_reentrant_proposal_during_parser_poisoning_is_checked(self):
        def parsing(data):
            with self.assertRaises(ReentrantCallError):
                self.admit(proposal())
            return parse_proposal(data)
        with patch("controlled_agent.authorization.parse_proposal", side_effect=parsing):
            self.assertIs(self.controller.submit(proposal()).outcome, Outcome.BLOCKED)
        self.assertEqual(self.service._records, {})
        self.no_effects()

    def test_partial_publication_failure_rolls_back_new_api(self):
        publish = self.service._publish
        def partial(ref, record):
            publish(ref, record)
            raise KeyboardInterrupt
        with patch.object(self.service, "_publish", side_effect=partial):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.controller.submit(proposal())
        self.assertEqual(self.service._records, {})
        self.assertIn(caught.exception.controller_report.action_id, self.service._seen)
        self.no_effects()

    def test_interrupted_human_review_and_failed_cancellation_audit_remain_observable(self):
        self.read.side_effect = KeyboardInterrupt
        self.fail = E.APPROVAL_RESULT
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.controller.submit(proposal("update_work_order_status"))
        self.assertTrue(caught.exception.controller_report.audit_failed)
        self.assertIs(caught.exception.controller_report.outcome, Outcome.AUDIT_FAILED)
        self.no_effects()

    def test_guard_exit_failure_after_publication_cleans_both_admission_apis(self):
        from contextlib import contextmanager
        original = self.service._operation
        @contextmanager
        def interrupted_exit():
            with original():
                yield
                raise KeyboardInterrupt
        for raw in (False, True):
            action_id = uuid4()
            with patch.object(self.service, "_operation", interrupted_exit):
                with self.assertRaises(KeyboardInterrupt):
                    if raw:
                        self.admit(proposal(), action_id)
                    else:
                        self.service.issue(self.request(action_id))
            self.assertFalse(self.service._records)
            with self.assertRaises(DuplicateActionError):
                self.admit(proposal(), action_id)
        self.no_effects()

    def test_handler_interruption_preserves_type_and_uncertain_effects(self):
        interruption = KeyboardInterrupt()
        def handler(action):
            self.fixture._statuses[action.target] = "closed"
            raise interruption
        counted = Mock(side_effect=handler)
        executor = Executor(authorization=self.service, handlers={"get_work_order": counted})
        controller = Controller(authorization=self.service, executor=executor, caller=self.caller)
        with self.assertRaises(KeyboardInterrupt) as caught:
            controller.submit(proposal())
        self.assertIs(caught.exception, interruption)
        self.assertIs(interruption.controller_report.execution_outcome, ExecutionOutcome.UNCERTAIN)
        self.assertEqual(self.fixture._statuses["WO-1001"], "closed")
        counted.assert_called_once()

    def test_components_must_share_service(self):
        other = AuthorizationService(tools=TOOLS, permissions=PERMISSIONS, resources=WORK_ORDERS,
                                     audit_write=self.audit)
        with self.assertRaises(TypeError):
            Controller(authorization=other, executor=self.executor, caller=self.caller)


if __name__ == "__main__":
    unittest.main()
