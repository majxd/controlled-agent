"""Execution boundary tests: counts and effects, with test-only audit writers."""

from copy import copy
from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from controlled_agent.approval import HumanApprovalAdapter
from controlled_agent.authorization import (
    AuditWriteError, AuthorizationService, DuplicateActionError, InvalidReferenceError, ReentrantCallError,
)
from controlled_agent.contracts import (
    ActionRequest, AuditEventType, CallerContext, Decision,
)
from controlled_agent.demo import TOOLS, PERMISSIONS, WORK_ORDERS, format_work_order_approval
from controlled_agent.demo_tools import FakeWorkOrderTools
from controlled_agent.executor import (
    Executor, ExecutionError, ExecutionOutcome, ToolRejected,
)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.views = []
        self.response = Mock(return_value="approve")
        self.fail_at = None
        self.audit_exception = OSError
        self.service = self.new_service()
        self.tools = FakeWorkOrderTools(authorization=self.service)
        self.handlers = {name: Mock(wraps=handler) for name, handler in self.tools.handlers.items()}
        self.executor = Executor(authorization=self.service, handlers=self.handlers)

    def writer(self, event):
        self.events.append(event)
        if event.event_type is self.fail_at:
            raise self.audit_exception("sensitive-audit-error")

    def new_service(self, **changes):
        return AuthorizationService(**({
            "tools": TOOLS, "permissions": PERMISSIONS, "resources": WORK_ORDERS,
            "audit_write": self.writer, "approval_formatter": format_work_order_approval,
            "human_approval": HumanApprovalAdapter(display=self.views.append, read_response=self.response),
        } | changes))

    def request(self, tool="get_work_order", target="WO-1001", **changes):
        arguments = {"work_order_id": target}
        if tool == "update_work_order_status":
            arguments["new_status"] = "closed"
        return ActionRequest(**({"action_id": uuid4(), "caller": CallerContext(caller_id="demo_operator"),
                                "tool_name": tool, "arguments": arguments} | changes))

    def issue(self, tool="get_work_order", target="WO-1001"):
        request = self.request(tool, target)
        _, reference = self.service.issue(request)
        self.assertIsNotNone(reference)
        return request, reference

    def assert_no_calls(self):
        for handler in self.handlers.values():
            handler.assert_not_called()
        self.assertEqual(self.tools._statuses, {"WO-1001": "open", "WO-9001": "open"})

    def assert_burned(self, reference, request):
        with self.assertRaises(InvalidReferenceError):
            self.service.consume(reference)
        with self.assertRaises(DuplicateActionError):
            self.service.issue(request)
        self.assertFalse(self.service._busy)

    def test_allowed_reads_dispatch_once_on_both_targets_and_audit_in_order(self):
        for target in WORK_ORDERS:
            request, reference = self.issue(target=target)
            action = self.service._records[reference].authorization.action
            report = self.executor.execute(reference)
            self.assertIs(report.outcome, ExecutionOutcome.SUCCEEDED)
            self.assertTrue(report.completion_audited)
            self.assertFalse(report.audit_failed)
            self.assertEqual(report.action_id, request.action_id)
            self.assertEqual(report.result["work_order_id"], target)
            self.assertEqual(report.result["status"], "open")
            self.assertIs(self.handlers["get_work_order"].call_args.args[0], action)
            self.assertEqual([e.event_type for e in self.events[-2:]], [
                AuditEventType.EXECUTION_STARTED, AuditEventType.EXECUTION_SUCCEEDED,
            ])
            with self.assertRaises(ExecutionError):
                self.executor.execute(reference)
        self.assertEqual(self.handlers["get_work_order"].call_count, 2)
        self.handlers["update_work_order_status"].assert_not_called()
        self.assertEqual(self.tools._statuses, {"WO-1001": "open", "WO-9001": "open"})

    def test_approved_mutation_binds_review_consumption_and_handler_identity(self):
        request, reference = self.issue("update_work_order_status")
        action = self.views[0].action
        report = self.executor.execute(reference)
        self.assertIs(self.handlers["update_work_order_status"].call_args.args[0], action)
        self.assertEqual(report.result["status"], "closed")
        self.assertEqual(self.tools._statuses, {"WO-1001": "closed", "WO-9001": "open"})
        self.response.assert_called_once()
        self.assertEqual([e.event_type for e in self.events], [
            AuditEventType.ACTION_SUBMITTED, AuditEventType.GOVERNANCE_DECISION,
            AuditEventType.APPROVAL_REQUESTED, AuditEventType.APPROVAL_RESULT,
            AuditEventType.AUTHORIZATION_ISSUED, AuditEventType.EXECUTION_STARTED,
            AuditEventType.EXECUTION_SUCCEEDED,
        ])
        self.assert_burned(reference, request)
        self.handlers["update_work_order_status"].assert_called_once()
        _, read_ref = self.issue()
        self.assertEqual(self.executor.execute(read_ref).result["status"], "closed")

    def test_denial_decline_and_cancellation_cannot_dispatch(self):
        for request, response, expected in (
            (self.request("update_work_order_status", "WO-9001"), "approve", Decision.DENY),
            (self.request("update_work_order_status"), "decline", Decision.REQUIRE_APPROVAL),
            (self.request("update_work_order_status"), None, Decision.REQUIRE_APPROVAL),
            (self.request("update_work_order_status"), "", Decision.REQUIRE_APPROVAL),
            (self.request("unknown"), "approve", Decision.DENY),
            (self.request(target="unknown"), "approve", Decision.DENY),
            (self.request(caller=CallerContext(caller_id="unprivileged")), "approve", Decision.DENY),
            (self.request(arguments={"work_order_id": "WO-1001", "approved": True}), "approve", Decision.DENY),
        ):
            with self.subTest(expected=expected, response=response):
                before = self.response.call_count
                self.response.return_value = response
                result, reference = self.service.issue(request)
                self.assertIs(result.decision, expected)
                self.assertIsNone(reference)
                if expected is Decision.DENY:
                    self.assertEqual(self.response.call_count, before)
                with self.assertRaises(ExecutionError) as caught:
                    self.executor.execute(reference)
                self.assertIs(caught.exception.report.outcome, ExecutionOutcome.NO_DISPATCH)
                self.assert_no_calls()
        self.assertNotIn(AuditEventType.EXECUTION_STARTED, [e.event_type for e in self.events])

    def test_forged_copied_foreign_and_data_objects_never_dispatch(self):
        request, reference = self.issue()
        other = self.new_service()
        _, foreign = other.issue(self.request())
        record = self.service._records[reference].authorization
        result, _ = self.service.issue(self.request("update_work_order_status", "WO-9001"))
        for value in (None, object(), uuid4(), request, result, record, record.authorization_id,
                      type(reference)(), copy(reference), foreign, {"approved": True}, self.events[-1]):
            with self.subTest(kind=type(value)):
                with self.assertRaises(ExecutionError):
                    self.executor.execute(value)
                self.assertIs(self.events[-1].event_type, AuditEventType.EXECUTION_BLOCKED)
                self.assertNotEqual(self.events[-1].action_id, request.action_id)
                self.assert_no_calls()
        self.assertIs(self.executor.execute(reference).outcome, ExecutionOutcome.SUCCEEDED)
        self.assertEqual(other.consume(foreign).target, "WO-1001")

    def test_raw_inputs_do_not_invoke_hash_equality_or_string_hooks(self):
        class Hostile:
            def __hash__(self):
                raise AssertionError("hash called")
            def __eq__(self, other):
                raise AssertionError("equality called")
            def __str__(self):
                raise AssertionError("string called")
        with self.assertRaises(ExecutionError) as caught:
            self.executor.execute(Hostile())
        self.assertIsInstance(caught.exception.__cause__, InvalidReferenceError)
        self.assert_no_calls()

    def test_execute_accepts_no_replacement_fields_or_configuration(self):
        _, reference = self.issue()
        for field, value in (("action", self.request()), ("arguments", {}), ("target", "WO-9001"),
                             ("decision", Decision.ALLOW), ("handlers", self.handlers)):
            with self.subTest(field=field), self.assertRaises(TypeError):
                self.executor.execute(reference, **{field: value})
        self.assert_no_calls()
        self.executor.execute(reference)
        self.handlers["get_work_order"].assert_called_once()

    def test_replay_is_correlated_and_never_invokes_again(self):
        request, reference = self.issue("update_work_order_status")
        self.executor.execute(reference)
        with self.assertRaises(ExecutionError) as caught:
            self.executor.execute(reference)
        self.assertIs(caught.exception.report.outcome, ExecutionOutcome.NO_DISPATCH)
        self.assertEqual(self.events[-1].action_id, request.action_id)
        self.assertEqual(self.events[-1].details["reason_code"], "AUTHORIZATION_CONSUMED")
        self.handlers["update_work_order_status"].assert_called_once()
        self.assertEqual(self.tools._statuses["WO-1001"], "closed")

    def test_missing_handler_burns_reference_and_audits_block(self):
        request, reference = self.issue()
        executor = Executor(authorization=self.service, handlers={})
        with self.assertRaises(ExecutionError) as caught:
            executor.execute(reference)
        self.assertEqual(caught.exception.report.reason_code, "HANDLER_UNAVAILABLE")
        self.assertIs(caught.exception.report.outcome, ExecutionOutcome.NO_DISPATCH)
        self.assertEqual(self.events[-1].action_id, request.action_id)
        self.assertIs(self.events[-1].event_type, AuditEventType.EXECUTION_BLOCKED)
        self.assert_no_calls()
        self.assert_burned(reference, request)

    def test_missing_handler_blocked_audit_failure_is_terminal(self):
        request, reference = self.issue()
        executor = Executor(authorization=self.service, handlers={})
        self.fail_at = AuditEventType.EXECUTION_BLOCKED
        with self.assertRaises(ExecutionError) as caught:
            executor.execute(reference)
        self.assertEqual(caught.exception.report.reason_code, "HANDLER_UNAVAILABLE")
        self.assertTrue(caught.exception.report.audit_failed)
        self.assertIs(caught.exception.report.outcome, ExecutionOutcome.NO_DISPATCH)
        self.assert_no_calls()
        self.fail_at = None
        self.assert_burned(reference, request)

    def test_handler_mapping_is_copied_read_only_and_not_model_selected(self):
        original = self.handlers["get_work_order"]
        replacement = Mock(side_effect=AssertionError("replacement called"))
        self.handlers["get_work_order"] = replacement
        with self.assertRaises(TypeError):
            self.executor._handlers["get_work_order"] = replacement
        _, reference = self.issue()
        self.executor.execute(reference)
        original.assert_called_once()
        replacement.assert_not_called()

    def test_start_audit_failure_or_interruption_blocks_dispatch_and_burns(self):
        for error in (OSError, KeyboardInterrupt, SystemExit):
            with self.subTest(error=error):
                request, reference = self.issue("update_work_order_status")
                self.fail_at, self.audit_exception = AuditEventType.EXECUTION_STARTED, error
                with self.assertRaises(ExecutionError if error is OSError else error) as caught:
                    self.executor.execute(reference)
                report = caught.exception.report if error is OSError else caught.exception.execution_report
                self.assertIs(report.outcome, ExecutionOutcome.NO_DISPATCH)
                self.assertTrue(report.audit_failed)
                self.assertFalse(report.completion_audited)
                self.assert_no_calls()
                self.fail_at = None
                self.assert_burned(reference, request)

    def test_handler_is_called_only_after_start_audit_acceptance_and_consumption(self):
        request, reference = self.issue()
        original_writer = self.service._audit_write
        handler = Mock(return_value={"ok": True})
        def writer(event):
            if event.event_type is AuditEventType.EXECUTION_STARTED:
                self.assertTrue(self.service._records[reference].consumed)
                handler.assert_not_called()
            original_writer(event)
        self.service._audit_write = writer
        executor = Executor(authorization=self.service, handlers={"get_work_order": handler})
        executor.execute(reference)
        handler.assert_called_once()
        self.assert_burned(reference, request)

    def test_unsuccessful_start_writer_return_is_failure(self):
        _, reference = self.issue()
        self.service._audit_write = lambda event: True
        with self.assertRaises(ExecutionError) as caught:
            self.executor.execute(reference)
        self.assertTrue(caught.exception.report.audit_failed)
        self.assert_no_calls()
        self.assertTrue(self.service._records[reference].consumed)

    def test_completion_audit_failure_reports_successful_effect_without_retry(self):
        for error in (OSError, KeyboardInterrupt, SystemExit):
            with self.subTest(error=error):
                fixture = FakeWorkOrderTools(authorization=self.service)
                handler = Mock(wraps=fixture.handlers["update_work_order_status"])
                executor = Executor(authorization=self.service, handlers={"update_work_order_status": handler})
                request, reference = self.issue("update_work_order_status")
                self.fail_at, self.audit_exception = AuditEventType.EXECUTION_SUCCEEDED, error
                with self.assertRaises(ExecutionError if error is OSError else error) as caught:
                    executor.execute(reference)
                report = caught.exception.report if error is OSError else caught.exception.execution_report
                self.assertIs(report.outcome, ExecutionOutcome.SUCCEEDED)
                self.assertEqual(report.result["status"], "closed")
                self.assertTrue(report.audit_failed)
                self.assertFalse(report.completion_audited)
                self.assertEqual(fixture._statuses["WO-1001"], "closed")
                self.assertIsNotNone(caught.exception.audit_error)
                self.assertIsNone(caught.exception.handler_error)
                handler.assert_called_once()
                self.fail_at = None
                self.assert_burned(reference, request)

    def test_partial_handler_failure_and_interruption_preserve_uncertain_effect(self):
        for error in (ValueError, KeyboardInterrupt, SystemExit):
            with self.subTest(error=error):
                state = []
                def fail(action):
                    state.append(action.target)
                    raise error("sensitive-handler-error")
                handler = Mock(side_effect=fail)
                executor = Executor(authorization=self.service, handlers={"get_work_order": handler})
                request, reference = self.issue()
                with self.assertRaises(ExecutionError if error is ValueError else error) as caught:
                    executor.execute(reference)
                report = caught.exception.report if error is ValueError else caught.exception.execution_report
                self.assertIs(report.outcome, ExecutionOutcome.UNCERTAIN)
                self.assertTrue(report.completion_audited)
                self.assertEqual(state, ["WO-1001"])
                handler.assert_called_once()
                self.assertIs(self.events[-1].event_type, AuditEventType.EXECUTION_FAILED)
                self.assertNotIn("sensitive-handler-error", str(self.events[-1].details))
                self.assert_burned(reference, request)

    def test_handler_audit_write_error_is_not_an_executor_audit_failure(self):
        effects = []
        handler_error = AuditWriteError("private handler failure")

        def fail(action):
            effects.append(action.target)
            raise handler_error

        handler = Mock(side_effect=fail)
        executor = Executor(authorization=self.service, handlers={"get_work_order": handler})
        request, reference = self.issue()
        with self.assertRaises(ExecutionError) as caught:
            executor.execute(reference)
        report = caught.exception.report
        self.assertIs(report.outcome, ExecutionOutcome.UNCERTAIN)
        self.assertEqual(report.reason_code, "HANDLER_FAILED")
        self.assertTrue(report.completion_audited)
        self.assertFalse(report.audit_failed)
        self.assertIsNone(caught.exception.audit_error)
        self.assertIs(caught.exception.handler_error, handler_error)
        self.assertIs(caught.exception.__cause__, handler_error)
        self.assertEqual(effects, ["WO-1001"])
        self.assertEqual([event.event_type for event in self.events[-2:]], [
            AuditEventType.EXECUTION_STARTED, AuditEventType.EXECUTION_FAILED,
        ])
        self.assertNotIn("private handler failure", str(self.events[-1].details))
        self.assert_burned(reference, request)
        handler.assert_called_once()

    def test_handler_audit_write_error_and_failed_completion_audit_remain_distinct(self):
        effects = []
        handler_error = AuditWriteError("private handler failure")

        def fail(action):
            effects.append(action.target)
            raise handler_error

        handler = Mock(side_effect=fail)
        executor = Executor(authorization=self.service, handlers={"get_work_order": handler})
        request, reference = self.issue()
        self.fail_at = AuditEventType.EXECUTION_FAILED
        with self.assertRaises(ExecutionError) as caught:
            executor.execute(reference)
        report = caught.exception.report
        self.assertIs(report.outcome, ExecutionOutcome.UNCERTAIN)
        self.assertTrue(report.audit_failed)
        self.assertFalse(report.completion_audited)
        self.assertIs(caught.exception.handler_error, handler_error)
        self.assertIsInstance(caught.exception.audit_error, AuditWriteError)
        self.assertIsNot(caught.exception.audit_error, handler_error)
        self.assertIsInstance(caught.exception.audit_error.__cause__, OSError)
        self.assertEqual(effects, ["WO-1001"])
        self.assertEqual(sum(event.event_type is AuditEventType.EXECUTION_FAILED
                             for event in self.events), 1)
        self.fail_at = None
        self.assert_burned(reference, request)
        handler.assert_called_once()

    def test_failure_audit_failure_preserves_handler_and_audit_errors(self):
        for handler_error in (ValueError("private"), KeyboardInterrupt(), SystemExit()):
            with self.subTest(error=type(handler_error)):
                handler = Mock(side_effect=handler_error)
                executor = Executor(authorization=self.service, handlers={"get_work_order": handler})
                request, reference = self.issue()
                self.fail_at = AuditEventType.EXECUTION_FAILED
                interrupted = not isinstance(handler_error, Exception)
                with self.assertRaises(type(handler_error) if interrupted else ExecutionError) as caught:
                    executor.execute(reference)
                report = caught.exception.execution_report if interrupted else caught.exception.report
                self.assertIs(report.outcome, ExecutionOutcome.UNCERTAIN)
                self.assertTrue(report.audit_failed)
                self.assertFalse(report.completion_audited)
                self.assertIsNotNone(caught.exception.audit_error)
                if interrupted:
                    self.assertIs(caught.exception, handler_error)
                else:
                    self.assertIs(caught.exception.handler_error, handler_error)
                    self.assertIsInstance(caught.exception.audit_error.__cause__, OSError)
                handler.assert_called_once()
                self.fail_at = None
                self.assert_burned(reference, request)

    def test_blocked_audit_failure_never_dispatches_or_restores_reference(self):
        request, reference = self.issue()
        self.service.consume(reference)
        self.fail_at = AuditEventType.EXECUTION_BLOCKED
        for supplied in (reference, object()):
            with self.assertRaises(ExecutionError) as caught:
                self.executor.execute(supplied)
            self.assertTrue(caught.exception.report.audit_failed)
            self.assert_no_calls()
        self.fail_at = None
        self.assert_burned(reference, request)

    def test_blocked_audit_interruptions_report_no_dispatch_and_failed_audit(self):
        for error in (KeyboardInterrupt, SystemExit):
            request, reference = self.issue()
            self.service.consume(reference)
            self.fail_at, self.audit_exception = AuditEventType.EXECUTION_BLOCKED, error
            for supplied in (reference, object()):
                with self.assertRaises(error) as caught:
                    self.executor.execute(supplied)
                self.assertIs(caught.exception.execution_report.outcome, ExecutionOutcome.NO_DISPATCH)
                self.assertTrue(caught.exception.execution_report.audit_failed)
                self.assert_no_calls()
            self.fail_at = None
            self.assert_burned(reference, request)

    def test_stale_review_baseline_rejects_second_mutation_without_modification(self):
        first, first_ref = self.issue("update_work_order_status")
        second = self.request("update_work_order_status", arguments={"work_order_id": "WO-1001",
                                                                   "new_status": "in_progress"})
        _, second_ref = self.service.issue(second)
        self.executor.execute(first_ref)
        with self.assertRaises(ExecutionError) as caught:
            self.executor.execute(second_ref)
        report = caught.exception.report
        self.assertIs(report.outcome, ExecutionOutcome.REJECTED)
        self.assertEqual(report.reason_code, "STALE_BASELINE")
        self.assertTrue(report.completion_audited)
        self.assertEqual(self.events[-1].details["reason_code"], "STALE_BASELINE")
        self.assertEqual(self.tools._statuses["WO-1001"], "closed")
        self.assertEqual(self.handlers["update_work_order_status"].call_count, 2)
        self.assertEqual([view.change.current_state["status"] for view in self.views], ["open", "open"])
        self.assert_burned(first_ref, first)
        self.assert_burned(second_ref, second)

    def test_new_review_after_mutation_still_cannot_use_stale_baseline(self):
        _, reference = self.issue("update_work_order_status")
        self.executor.execute(reference)
        request, reference = self.issue("update_work_order_status")
        with self.assertRaises(ExecutionError) as caught:
            self.executor.execute(reference)
        self.assertEqual(caught.exception.report.reason_code, "STALE_BASELINE")
        self.assertEqual(self.tools._statuses["WO-1001"], "closed")
        self.assert_burned(reference, request)

    def test_reentrant_start_audit_blocks_all_nested_operations_even_if_caught(self):
        for operation in ("issue", "consume", "execute", "other_executor"):
            with self.subTest(operation=operation):
                request, reference = self.issue()
                spare_request, spare = self.issue()
                other_executor = Executor(authorization=self.service, handlers=self.handlers)
                def writer(event):
                    self.events.append(event)
                    if event.event_type is AuditEventType.EXECUTION_STARTED:
                        with self.assertRaises((ReentrantCallError, ExecutionError)):
                            if operation == "issue":
                                self.service.issue(self.request())
                            elif operation == "consume":
                                self.service.consume(spare)
                            else:
                                (other_executor if operation == "other_executor" else self.executor).execute(spare)
                self.service._audit_write = writer
                with self.assertRaises(ExecutionError) as caught:
                    self.executor.execute(reference)
                self.assertIs(caught.exception.report.outcome, ExecutionOutcome.NO_DISPATCH)
                self.assert_no_calls()
                self.service._audit_write = self.writer
                self.assert_burned(reference, request)
                self.assertEqual(self.service.consume(spare).action_id, spare_request.action_id)

    def test_reentrant_handler_reports_actual_returned_effect_without_more_callbacks(self):
        for operation in ("issue", "consume", "execute"):
            with self.subTest(operation=operation):
                state = []
                request, reference = self.issue()
                _, spare = self.issue()
                def handler(action):
                    state.append("effect")
                    with self.assertRaises((ReentrantCallError, ExecutionError)):
                        if operation == "issue":
                            self.service.issue(self.request())
                        elif operation == "consume":
                            self.service.consume(spare)
                        else:
                            self.executor.execute(spare)
                    return {"effect": True}
                counted = Mock(side_effect=handler)
                executor = Executor(authorization=self.service, handlers={"get_work_order": counted})
                before = len(self.events)
                with self.assertRaises(ExecutionError) as caught:
                    executor.execute(reference)
                report = caught.exception.report
                self.assertIs(report.outcome, ExecutionOutcome.SUCCEEDED)
                self.assertEqual(report.reason_code, "REENTRANT_EXECUTION")
                self.assertFalse(report.completion_audited)
                self.assertEqual(len(self.events), before + 1)  # start only; no recursive audit
                self.assertEqual(state, ["effect"])
                counted.assert_called_once()
                self.assert_no_calls()
                self.assert_burned(reference, request)
                self.service.consume(spare)

    def test_reentrancy_in_completion_audit_preserves_effect_and_blocks_nested_dispatch(self):
        request, reference = self.issue("update_work_order_status")
        _, spare = self.issue()
        def writer(event):
            self.events.append(event)
            if event.event_type is AuditEventType.EXECUTION_SUCCEEDED:
                with self.assertRaises(ExecutionError):
                    self.executor.execute(spare)
        self.service._audit_write = writer
        with self.assertRaises(ExecutionError) as caught:
            self.executor.execute(reference)
        self.assertIs(caught.exception.report.outcome, ExecutionOutcome.SUCCEEDED)
        self.assertTrue(caught.exception.report.audit_failed)
        self.assertEqual(self.tools._statuses["WO-1001"], "closed")
        self.handlers["update_work_order_status"].assert_called_once()
        self.handlers["get_work_order"].assert_not_called()
        self.service._audit_write = self.writer
        self.assert_burned(reference, request)
        self.service.consume(spare)

    def test_reentrant_execute_from_issuance_callback_cannot_dispatch_existing_reference(self):
        _, reference = self.issue()
        def writer(event):
            self.events.append(event)
            with self.assertRaises(ExecutionError):
                self.executor.execute(reference)
        self.service._audit_write = writer
        with self.assertRaises(ReentrantCallError):
            self.service.issue(self.request())
        self.assert_no_calls()
        self.service._audit_write = self.writer
        self.executor.execute(reference)
        self.handlers["get_work_order"].assert_called_once()

    def test_issued_action_cannot_change_and_is_not_reevaluated_at_dispatch(self):
        request, reference = self.issue()
        stored = self.service._records[reference].authorization.action
        with patch("controlled_agent.authorization._evaluate_action", side_effect=AssertionError("reevaluation")):
            report = self.executor.execute(reference)
        self.assertIs(self.handlers["get_work_order"].call_args.args[0], stored)
        with self.assertRaises(TypeError):
            report.result["status"] = "other"
        with self.assertRaises(FrozenInstanceError):
            report.outcome = ExecutionOutcome.NO_DISPATCH
        with self.assertRaises(ExecutionError):
            self.executor.execute(report)
        self.handlers["get_work_order"].assert_called_once()

    def test_sensitive_arguments_results_and_errors_are_not_audited(self):
        rules = replace(TOOLS["get_work_order"], validate_arguments=lambda _: True)
        service = self.new_service(tools={"get_work_order": rules})
        handler = Mock(return_value={"secret_result": "PRIVATE_RESULT"})
        executor = Executor(authorization=service, handlers={"get_work_order": handler})
        request = self.request(arguments={"work_order_id": "WO-1001", "secret": "PRIVATE_ARGUMENT"})
        _, reference = service.issue(request)
        report = executor.execute(reference)
        self.assertEqual(report.result["secret_result"], "PRIVATE_RESULT")
        self.assertEqual(handler.call_args.args[0].arguments["secret"], "PRIVATE_ARGUMENT")
        for event in self.events[-2:]:
            self.assertEqual(set(event.details), {"tool_name", "target", "outcome", "reason_code"})
            self.assertEqual(event.action_id, request.action_id)
        serialized = str(self.events)
        for text in ("PRIVATE_ARGUMENT", "PRIVATE_RESULT", repr(reference)):
            self.assertNotIn(text, serialized)
        handler.assert_called_once()

    def test_invalid_result_does_not_erase_known_handler_completion(self):
        for result in (None, object(), {"bad": object()}):
            with self.subTest(result_type=type(result)):
                handler = Mock(return_value=result)
                executor = Executor(authorization=self.service, handlers={"get_work_order": handler})
                request, reference = self.issue()
                with self.assertRaises(ExecutionError) as caught:
                    executor.execute(reference)
                self.assertIs(caught.exception.report.outcome, ExecutionOutcome.SUCCEEDED)
                self.assertEqual(caught.exception.report.reason_code, "RESULT_INVALID")
                self.assertTrue(caught.exception.report.completion_audited)
                handler.assert_called_once()
                self.assert_burned(reference, request)

    def test_interrupt_during_consumption_never_reaches_handler_or_restores_reference(self):
        request, reference = self.issue()
        with patch("controlled_agent.authorization.replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.executor.execute(reference)
        self.assertIs(caught.exception.execution_report.outcome, ExecutionOutcome.NO_DISPATCH)
        self.assert_no_calls()
        self.assert_burned(reference, request)


    def test_failed_issuance_audit_gates_leave_nothing_to_dispatch(self):
        for gate in (AuditEventType.ACTION_SUBMITTED, AuditEventType.GOVERNANCE_DECISION,
                     AuditEventType.APPROVAL_REQUESTED, AuditEventType.APPROVAL_RESULT,
                     AuditEventType.AUTHORIZATION_ISSUED):
            with self.subTest(gate=gate):
                self.fail_at = gate
                request = self.request("update_work_order_status")
                with self.assertRaises(AuditWriteError):
                    self.service.issue(request)
                self.assertEqual(self.service._records, {})
                self.fail_at = None
                with self.assertRaises(ExecutionError):
                    self.executor.execute(None)
                with self.assertRaises(DuplicateActionError):
                    self.service.issue(request)
                self.assert_no_calls()

    def test_failure_audit_reentrancy_cannot_dispatch_spare_reference(self):
        request, reference = self.issue()
        _, spare = self.issue()
        handler = Mock(side_effect=ValueError("private-handler-failure"))
        executor = Executor(authorization=self.service, handlers={"get_work_order": handler})
        def writer(event):
            self.events.append(event)
            if event.event_type is AuditEventType.EXECUTION_FAILED:
                with self.assertRaises(ExecutionError):
                    self.executor.execute(spare)
        self.service._audit_write = writer
        with self.assertRaises(ExecutionError) as caught:
            executor.execute(reference)
        self.assertIs(caught.exception.report.outcome, ExecutionOutcome.UNCERTAIN)
        self.assertTrue(caught.exception.report.audit_failed)
        self.assertIsInstance(caught.exception.handler_error, ValueError)
        handler.assert_called_once()
        self.assert_no_calls()
        self.service._audit_write = self.writer
        self.assert_burned(reference, request)
        self.service.consume(spare)

    def test_blocked_audit_reentrancy_does_not_recurse_or_consume_spare(self):
        _, spare = self.issue()
        def writer(event):
            self.events.append(event)
            with self.assertRaises(ExecutionError):
                self.executor.execute(spare)
        self.service._audit_write = writer
        before = len(self.events)
        with self.assertRaises(ExecutionError):
            self.executor.execute(object())
        self.assertEqual(len(self.events), before + 1)
        self.assert_no_calls()
        self.service._audit_write = self.writer
        self.service.consume(spare)

    def test_reentrant_interrupted_handler_preserves_interruption_and_effect(self):
        request, reference = self.issue()
        state = []
        interruption = KeyboardInterrupt()
        def handler(action):
            state.append("effect")
            with self.assertRaises(ReentrantCallError):
                self.service.issue(self.request())
            raise interruption
        counted = Mock(side_effect=handler)
        executor = Executor(authorization=self.service, handlers={"get_work_order": counted})
        before = len(self.events)
        with self.assertRaises(KeyboardInterrupt) as caught:
            executor.execute(reference)
        self.assertIs(caught.exception, interruption)
        self.assertIs(caught.exception.execution_report.outcome, ExecutionOutcome.UNCERTAIN)
        self.assertEqual(caught.exception.execution_report.reason_code, "REENTRANT_EXECUTION")
        self.assertEqual(state, ["effect"])
        self.assertEqual(len(self.events), before + 1)
        counted.assert_called_once()
        self.assert_burned(reference, request)

    def test_stale_rejection_audit_failure_retains_known_no_mutation_outcome(self):
        _, reference = self.issue("update_work_order_status")
        self.executor.execute(reference)
        request, reference = self.issue("update_work_order_status")
        self.fail_at = AuditEventType.EXECUTION_FAILED
        with self.assertRaises(ExecutionError) as caught:
            self.executor.execute(reference)
        self.assertIs(caught.exception.report.outcome, ExecutionOutcome.REJECTED)
        self.assertEqual(caught.exception.report.reason_code, "STALE_BASELINE")
        self.assertTrue(caught.exception.report.audit_failed)
        self.assertEqual(self.tools._statuses["WO-1001"], "closed")
        self.assertEqual(self.handlers["update_work_order_status"].call_count, 2)
        self.fail_at = None
        self.assert_burned(reference, request)

    def test_execution_audit_omits_out_of_policy_configured_identifiers(self):
        for identifier in ("x" * 129, "line\nbreak", "nonascii-é"):
            with self.subTest(identifier=identifier):
                rules = TOOLS["get_work_order"]
                rules = replace(rules, definition=replace(rules.definition, name=identifier),
                                resolve_target=lambda _: identifier, validate_arguments=lambda _: True)
                service = self.new_service(tools={identifier: rules}, resources={identifier: {}},
                                           permissions={"demo_operator": {identifier}})
                handler = Mock(return_value={})
                executor = Executor(authorization=service, handlers={identifier: handler})
                _, reference = service.issue(self.request(tool=identifier, arguments={}))
                executor.execute(reference)
                for event in self.events[-2:]:
                    self.assertIsNone(event.details["tool_name"])
                    self.assertIsNone(event.details["target"])
                    self.assertNotIn(identifier, str(event.details))
                handler.assert_called_once()

    def test_result_snapshot_does_not_retain_mutable_handler_aliases(self):
        result = {"items": [{"value": "original"}]}
        handler = Mock(return_value=result)
        executor = Executor(authorization=self.service, handlers={"get_work_order": handler})
        _, reference = self.issue()
        report = executor.execute(reference)
        result["items"][0]["value"] = "changed"
        self.assertEqual(report.result["items"][0]["value"], "original")
        handler.assert_called_once()

    def test_fake_tools_use_the_services_captured_review_baseline(self):
        resources = {key: dict(value) for key, value in WORK_ORDERS.items()}
        resources["WO-1001"]["status"] = "in_progress"
        service = self.new_service(resources=resources)
        resources["WO-1001"]["status"] = "open"
        fixture = FakeWorkOrderTools(authorization=service)
        handler = Mock(wraps=fixture.handlers["update_work_order_status"])
        executor = Executor(authorization=service, handlers={"update_work_order_status": handler})
        _, reference = service.issue(self.request("update_work_order_status"))
        self.assertEqual(self.views[-1].change.current_state["status"], "in_progress")
        with self.assertRaises(ExecutionError) as caught:
            executor.execute(reference)
        self.assertEqual(caught.exception.report.reason_code, "STALE_BASELINE")
        self.assertEqual(fixture._statuses["WO-1001"], "open")
        handler.assert_called_once()

    def test_fake_protection_bounds_hold_even_under_permissive_trusted_test_policy(self):
        rules = TOOLS["update_work_order_status"]
        service = self.new_service(tools={rules.definition.name: replace(
            rules, evaluate_policy=TOOLS["get_work_order"].evaluate_policy,
        )})
        fixture = FakeWorkOrderTools(authorization=service)
        handler = Mock(wraps=fixture.handlers["update_work_order_status"])
        executor = Executor(authorization=service, handlers={"update_work_order_status": handler})
        _, reference = service.issue(self.request("update_work_order_status", "WO-9001"))
        with self.assertRaises(ExecutionError) as caught:
            executor.execute(reference)
        self.assertIs(caught.exception.report.outcome, ExecutionOutcome.REJECTED)
        self.assertEqual(caught.exception.report.reason_code, "PROTECTED_TARGET")
        self.assertEqual(fixture._statuses["WO-9001"], "open")
        handler.assert_called_once()


class FakeToolTests(unittest.TestCase):
    def setUp(self):
        self.service = AuthorizationService(tools=TOOLS, permissions=PERMISSIONS, resources=WORK_ORDERS,
                                            audit_write=lambda event: None)
        self.tools = FakeWorkOrderTools(authorization=self.service)

    def action(self, tool="update_work_order_status", target="WO-1001", **changes):
        return ActionRequest(**({"action_id": uuid4(), "caller": CallerContext(caller_id="demo_operator"),
                                "tool_name": tool, "target": target,
                                "arguments": {"work_order_id": target, "new_status": "closed"}} | changes))

    def test_direct_handler_bounds_reject_bad_identity_fields_status_and_protection(self):
        handler = self.tools.handlers["update_work_order_status"]
        for action in (None, {}, self.action(target="WO-9001"), self.action(target="unknown"),
                       self.action(tool="get_work_order"), self.action(target=None),
                       self.action(arguments={"work_order_id": "WO-9001", "new_status": "closed"}),
                       self.action(arguments={"work_order_id": "WO-1001", "new_status": "deleted"}),
                       self.action(arguments={"work_order_id": "WO-1001", "new_status": True}),
                       self.action(arguments={"work_order_id": "WO-1001"}),
                       self.action(arguments={"work_order_id": "WO-1001", "new_status": "closed", "approved": True})):
            with self.subTest(action=action):
                with self.assertRaises(ToolRejected):
                    handler(action)
                self.assertEqual(self.tools._statuses, {"WO-1001": "open", "WO-9001": "open"})

    def test_read_requires_exact_fields_and_returns_detached_snapshot(self):
        handler = self.tools.handlers["get_work_order"]
        with self.assertRaises(ToolRejected):
            handler(self.action(tool="get_work_order"))
        read = self.action(tool="get_work_order", arguments={"work_order_id": "WO-1001"})
        snapshot = handler(read)
        self.tools.handlers["update_work_order_status"](self.action())
        self.assertEqual(snapshot["status"], "open")
        self.assertEqual(handler(read)["status"], "closed")
        with self.assertRaises(TypeError):
            snapshot["status"] = "closed"

    def test_all_supported_statuses_and_same_status_change_only_status(self):
        for status in ("open", "in_progress", "closed"):
            fixture = FakeWorkOrderTools(authorization=self.service)
            result = fixture.handlers["update_work_order_status"](self.action(
                arguments={"work_order_id": "WO-1001", "new_status": status}))
            self.assertEqual(result["status"], status)
            self.assertEqual({key: result[key] for key in ("editable", "protected", "critical")},
                             {"editable": True, "protected": False, "critical": False})
            self.assertEqual(fixture._statuses["WO-9001"], "open")
        self.assertEqual(WORK_ORDERS["WO-1001"]["status"], "open")

    def test_independent_fixture_state_has_no_io_or_implicit_input(self):
        other = FakeWorkOrderTools(authorization=self.service)
        with patch("builtins.open", side_effect=AssertionError("no file IO")), \
             patch("builtins.input", side_effect=AssertionError("no console")):
            self.tools.handlers["update_work_order_status"](self.action())
        self.assertEqual(other._statuses["WO-1001"], "open")
        self.assertEqual(self.tools._statuses["WO-1001"], "closed")


if __name__ == "__main__":
    unittest.main()
