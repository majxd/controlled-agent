"""Host lifetime and handoff tests, not a duplicate provider/SDK decoder suite."""

import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from controlled_agent.approval import HumanApprovalAdapter
from controlled_agent.audit import JsonlAuditWriter
from controlled_agent.authorization import AuthorizationService
from controlled_agent.contracts import AuditEventType as E, CallerContext
from controlled_agent.controller import Controller, ControllerReport, Outcome
from controlled_agent.demo import TOOLS, PERMISSIONS, WORK_ORDERS, format_work_order_approval
from controlled_agent.demo_tools import FakeWorkOrderTools
from controlled_agent.executor import Executor, ExecutionOutcome
from controlled_agent.provider import ProposalAdapter, ProviderError, ProviderFailure, ProviderRequest
from controlled_agent.provider_host import ProviderHost, ProviderHostReentry, ProviderHostStopped


SDK_AVAILABLE = all(importlib.util.find_spec(name) for name in ("groq", "httpx"))
DENIED = ["action_submitted", "governance_decision"]
READ = DENIED + ["authorization_issued", "execution_started", "execution_succeeded"]
UPDATED = DENIED + ["approval_requested", "approval_result"] + READ[2:]


def proposal(tool="get_work_order", target="WO-1001"):
    arguments = {"work_order_id": target}
    if tool == "update_work_order_status":
        arguments["new_status"] = "closed"
    return {"tool_name": tool, "arguments": arguments}


class SequenceProvider:
    """Test-only queue; callbacks can attempt same-host reentry."""

    def __init__(self, *responses):
        self.remaining = list(responses)
        self.calls = 0
        self.close = Mock()

    def propose(self, request):
        self.calls += 1
        response = self.remaining.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response(request) if callable(response) else response


class ProviderHostTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(TemporaryDirectory())
        self.path = Path(temp) / "audit.jsonl"
        self.writer = self.enterContext(JsonlAuditWriter(self.path))
        self.events = []
        self.audit_hook = lambda event: None

        def audit(event):
            self.writer(event)
            self.events.append(event)
            self.audit_hook(event)

        self.display = Mock(return_value=None)
        self.read = Mock(return_value="approve")
        self.service = AuthorizationService(
            tools=TOOLS, permissions=PERMISSIONS, resources=WORK_ORDERS, audit_write=audit,
            approval_formatter=format_work_order_approval,
            human_approval=HumanApprovalAdapter(display=self.display, read_response=self.read),
        )
        self.fixture = FakeWorkOrderTools(authorization=self.service)
        self.handlers = {name: Mock(wraps=handler) for name, handler in self.fixture.handlers.items()}
        self.executor = Executor(authorization=self.service, handlers=self.handlers)
        self.controller = Controller(authorization=self.service, executor=self.executor,
                                     caller=CallerContext(caller_id="demo_operator"))
        self.reports = []
        self.raised = []
        original_submit = self.controller.submit

        def submit(data):
            try:
                report = original_submit(data)
            except BaseException as error:
                self.raised.append((error, getattr(error, "controller_report", None)))
                raise
            self.reports.append(report)
            return report

        self.submit = self.enterContext(patch.object(self.controller, "submit", side_effect=submit))
        self.new_id = self.enterContext(patch("controlled_agent.controller.uuid4", wraps=uuid4))
        self.request = ProviderRequest(prompt="Read synthetic work order WO-1001.", tools=tuple(
            rule.definition for rule in TOOLS.values()))

    def host_for(self, *responses):
        self.provider = SequenceProvider(*responses)
        self.adapter = ProposalAdapter(self.provider)
        self.propose = self.enterContext(patch.object(self.adapter, "propose", wraps=self.adapter.propose))
        self.host = ProviderHost(controller=self.controller, adapter=self.adapter)
        return self.host

    def snapshot(self):
        return (self.path.read_bytes(), self.service._seen.copy(), dict(self.service._records),
                {ref: entry.consumed for ref, entry in self.service._records.items()},
                self.fixture._statuses.copy(), self.new_id.call_count,
                tuple(handler.call_count for handler in self.handlers.values()),
                self.read.call_count, self.display.call_count)

    def assert_stopped_without_more_work(self):
        before = self.snapshot()
        calls = (self.provider.calls, self.propose.call_count, self.submit.call_count)
        queue = self.provider.remaining.copy()
        self.assertTrue(self.host.stopped)
        for _ in range(2):
            with self.assertRaises(ProviderHostStopped) as caught:
                self.host.run_once(self.request)
            self.assertEqual(str(caught.exception), "provider_host_stopped")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.provider.calls, self.propose.call_count, self.submit.call_count), calls)
        self.assertEqual(self.provider.remaining, queue)
        self.provider.close.assert_not_called()

    def assert_no_submission(self):
        self.submit.assert_not_called()
        self.new_id.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertEqual(self.events, [])
        self.assertEqual(self.service._seen, set())
        self.assertEqual(self.service._records, {})
        self.assertEqual(self.fixture._statuses, {"WO-1001": "open", "WO-9001": "open"})
        for handler in self.handlers.values():
            handler.assert_not_called()
        self.display.assert_not_called()
        self.read.assert_not_called()

    def assert_lifecycle(self, report, expected):
        records = [json.loads(line) for line in self.path.read_text().splitlines()]
        action = [record for record in records if record["action_id"] == str(report.action_id)]
        self.assertEqual([record["event_type"] for record in action], expected)
        self.assertTrue(all(record["caller"] == "demo_operator" for record in action))
        if report.decision is not None:
            self.assertEqual(action[1]["details"]["decision"], report.decision.value)
        self.assertEqual({record["action_id"] for record in records},
                         {str(action_id) for action_id in self.service._seen})
        self.assertEqual([(record["action_id"], record["event_type"]) for record in records],
                         [(str(event.action_id), event.event_type.value) for event in self.events])

    def test_read_handoff_once_returns_original_report_and_uses_host_identity(self):
        data = proposal()
        host = self.host_for(data, proposal())
        report = host.run_once(self.request)
        self.assertIs(report, self.reports[0])
        self.assertEqual(report.outcome, Outcome.SUCCEEDED)
        self.assertFalse(host.stopped)
        self.propose.assert_called_once_with(self.request)
        self.submit.assert_called_once_with(data)
        self.assertIsNot(self.submit.call_args.args[0], data)
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.provider.remaining, [proposal()])
        self.new_id.assert_called_once()
        self.handlers["get_work_order"].assert_called_once()
        self.handlers["update_work_order_status"].assert_not_called()
        self.assert_lifecycle(report, READ)
        self.assertEqual(self.fixture._statuses["WO-1001"], "open")
        self.provider.close.assert_not_called()

    def test_no_action_is_not_stopped_and_next_explicit_call_can_submit(self):
        host = self.host_for(None, proposal())
        before = self.snapshot()
        self.assertIsNone(host.run_once(self.request))
        self.assertEqual(self.snapshot(), before)
        self.assert_no_submission()
        self.assertFalse(host.stopped)
        self.assertEqual(self.provider.remaining, [proposal()])
        report = host.run_once(self.request)
        self.assertEqual(self.provider.calls, 2)
        self.submit.assert_called_once()
        self.assert_lifecycle(report, READ)

    def test_every_provider_failure_stops_without_submission_or_cleanup(self):
        for reason in ProviderFailure:
            with self.subTest(reason=reason):
                host = self.host_for(ProviderError(reason), proposal())
                with self.assertRaises(ProviderError) as caught:
                    host.run_once(self.request)
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(str(caught.exception), reason.value)
                self.assertEqual(self.provider.calls, 1)
                self.assertEqual(self.provider.remaining, [proposal()])
                self.assert_no_submission()
                self.assert_stopped_without_more_work()

    def test_invalid_provider_output_stops_in_adapter(self):
        host = self.host_for(proposal() | {"approved": True}, proposal())
        with self.assertRaises(ProviderError) as caught:
            host.run_once(self.request)
        self.assertEqual(caught.exception.reason, ProviderFailure.INVALID_OUTPUT)
        self.assert_no_submission()
        self.assert_stopped_without_more_work()

    def test_invalid_host_request_stops_without_calling_provider(self):
        host = self.host_for(proposal())
        with self.assertRaises(TypeError):
            host.run_once({})
        self.assertEqual(self.provider.calls, 0)
        self.assert_no_submission()
        self.assert_stopped_without_more_work()

    def test_unexpected_adapter_error_propagates_original_and_stops(self):
        host = self.host_for(proposal())
        error = RuntimeError("PRIVATE_TEST_DIAGNOSTIC")
        self.propose.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            host.run_once(self.request)
        self.assertIs(caught.exception, error)
        self.assert_no_submission()
        self.assert_stopped_without_more_work()

    def test_provider_interruptions_preserve_identity_and_stop(self):
        for error in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(kind=type(error)):
                host = self.host_for(error, proposal())
                with self.assertRaises(type(error)) as caught:
                    host.run_once(self.request)
                self.assertIs(caught.exception, error)
                self.assert_no_submission()
                self.assert_stopped_without_more_work()

    def test_terminal_flag_not_outcome_controls_permanent_stop(self):
        # Core tests cover why these reports are terminal. Here only verify the
        # host respects the flag and does not infer it from the outcome label.
        for outcome in Outcome:
            with self.subTest(outcome=outcome):
                report = ControllerReport(action_id=uuid4(), outcome=outcome, terminal=True)
                host = self.host_for(proposal(), proposal())
                self.submit.side_effect = None
                self.submit.return_value = report
                self.assertIs(host.run_once(self.request), report)
                self.assert_stopped_without_more_work()

    def test_update_read_and_stale_rejection_keep_one_session(self):
        update = proposal("update_work_order_status")
        host = self.host_for(update, proposal(), update)
        first = host.run_once(self.request)
        self.assert_lifecycle(first, UPDATED)
        review = self.display.call_args.args[0]
        self.assertEqual(review.action.action_id, first.action_id)
        self.assertEqual(review.action.arguments, update["arguments"])
        self.assertEqual(review.action.caller.caller_id, "demo_operator")
        read = host.run_once(self.request)
        self.assertEqual(read.result["status"], "closed")
        self.assert_lifecycle(read, READ)
        stale = host.run_once(self.request)
        self.assertEqual(stale.outcome, Outcome.REJECTED)
        self.assertTrue(stale.stale_baseline)
        self.assertFalse(stale.terminal)
        self.assertFalse(host.stopped)
        self.assert_lifecycle(stale, UPDATED[:-1] + ["execution_failed"])
        self.assertEqual(self.handlers["update_work_order_status"].call_count, 2)
        self.handlers["get_work_order"].assert_called_once()
        self.assertEqual(self.read.call_count, 2)
        self.assertEqual(self.provider.calls, 3)
        self.assertEqual(self.submit.call_count, 3)
        self.assertEqual(self.fixture._statuses, {"WO-1001": "closed", "WO-9001": "open"})
        self.assertTrue(all(record.consumed for record in self.service._records.values()))
        self.assertIs(host._controller, self.controller)
        self.assertIs(self.controller._authorization, self.service)
        self.assertIs(self.controller._executor, self.executor)

    def test_denial_and_decline_allow_only_new_explicit_calls(self):
        host = self.host_for(proposal("update_work_order_status", "WO-9001"),
                             proposal("update_work_order_status"), proposal())
        denied = host.run_once(self.request)
        self.assertEqual(denied.outcome, Outcome.DENIED)
        self.assert_lifecycle(denied, DENIED)
        self.read.assert_not_called()
        self.assertFalse(host.stopped)
        self.read.return_value = "decline"
        declined = host.run_once(self.request)
        self.assertEqual(declined.outcome, Outcome.APPROVAL_NOT_GRANTED)
        self.assert_lifecycle(declined, UPDATED[:4])
        self.assertFalse(host.stopped)
        self.assertEqual(self.provider.calls, 2)
        self.assertEqual(self.provider.remaining, [proposal()])
        for handler in self.handlers.values():
            handler.assert_not_called()
        self.assertEqual(self.fixture._statuses, {"WO-1001": "open", "WO-9001": "open"})

    def test_provider_failure_after_mutation_preserves_state_and_unused_success(self):
        host = self.host_for(proposal("update_work_order_status"),
                             ProviderError(ProviderFailure.TIMEOUT), proposal())
        first = host.run_once(self.request)
        self.assert_lifecycle(first, UPDATED)
        before = self.snapshot()
        with self.assertRaises(ProviderError):
            host.run_once(self.request)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.provider.remaining, [proposal()])
        self.assertEqual(self.provider.calls, 2)
        self.assertTrue(all(record.consumed for record in self.service._records.values()))
        self.assertEqual(self.fixture._statuses["WO-1001"], "closed")
        self.assert_stopped_without_more_work()
        self.assertIs(host._controller, self.controller)
        self.assertIs(host._adapter, self.adapter)

    def test_audit_failure_before_execution_stops_without_dispatch(self):
        host = self.host_for(proposal(), proposal())
        def fail(event):
            if event.event_type is E.EXECUTION_STARTED:
                raise OSError("PRIVATE_AUDIT_FAILURE")
        self.audit_hook = fail
        report = host.run_once(self.request)
        self.assertIs(report, self.reports[0])
        self.assertTrue(report.terminal)
        self.assertEqual(report.outcome, Outcome.AUDIT_FAILED)
        self.assertEqual(report.execution_outcome, ExecutionOutcome.NO_DISPATCH)
        self.assert_lifecycle(report, READ[:-1])
        for handler in self.handlers.values():
            handler.assert_not_called()
        self.assertEqual(self.fixture._statuses["WO-1001"], "open")
        self.assert_stopped_without_more_work()

    def test_audit_failure_after_mutation_keeps_completed_effect_and_report(self):
        host = self.host_for(proposal("update_work_order_status"), proposal())
        def fail(event):
            if event.event_type is E.EXECUTION_SUCCEEDED:
                raise OSError("PRIVATE_AUDIT_FAILURE")
        self.audit_hook = fail
        report = host.run_once(self.request)
        self.assertIs(report, self.reports[0])
        self.assertEqual(report.outcome, Outcome.AUDIT_FAILED)
        self.assertEqual(report.execution_outcome, ExecutionOutcome.SUCCEEDED)
        self.assert_lifecycle(report, UPDATED)
        self.handlers["update_work_order_status"].assert_called_once()
        self.assertEqual(self.fixture._statuses["WO-1001"], "closed")
        self.assert_stopped_without_more_work()

    def swallowed_reentry(self):
        with self.assertRaises(ProviderHostReentry) as caught:
            self.host.run_once(self.request)
        self.assertEqual(str(caught.exception), "provider_host_reentry")
        self.assertTrue(self.host.stopped)

    def test_swallowed_provider_reentry_prevents_outer_submission_even_for_no_action(self):
        for response in (proposal(), None):
            with self.subTest(no_action=response is None):
                def produce(request):
                    self.swallowed_reentry()
                    self.swallowed_reentry()  # Still running, already stopped.
                    return response
                host = self.host_for(produce, proposal())
                with self.assertRaises(ProviderHostReentry) as caught:
                    host.run_once(self.request)
                self.assertEqual(str(caught.exception), "provider_host_reentry")
                self.propose.assert_called_once()
                self.assertEqual(self.provider.calls, 1)
                self.assert_no_submission()
                self.assert_stopped_without_more_work()

    def test_uncaught_provider_reentry_uses_existing_adapter_error_contract(self):
        host = self.host_for(lambda request: self.host.run_once(request), proposal())
        with self.assertRaises(ProviderError) as caught:
            host.run_once(self.request)
        self.assertEqual(caught.exception.reason, ProviderFailure.UNAVAILABLE)
        self.propose.assert_called_once()
        self.assertEqual(self.provider.calls, 1)
        self.assert_no_submission()
        self.assert_stopped_without_more_work()

    def test_swallowed_review_reentry_preserves_real_success_without_claiming_cancellation(self):
        host = self.host_for(proposal("update_work_order_status"), proposal())
        self.display.side_effect = lambda review: self.swallowed_reentry()
        report = host.run_once(self.request)
        self.assertIs(report, self.reports[0])
        self.assertEqual(report.outcome, Outcome.SUCCEEDED)
        self.assertFalse(report.terminal)  # Report remains the core's actual report.
        self.assertTrue(host.stopped)  # Host misuse independently stops future work.
        self.assert_lifecycle(report, UPDATED)
        self.assertEqual(self.fixture._statuses["WO-1001"], "closed")
        self.handlers["update_work_order_status"].assert_called_once()
        self.propose.assert_called_once()
        self.submit.assert_called_once()
        self.assertEqual(self.provider.calls, 1)
        self.assert_stopped_without_more_work()

    def test_swallowed_audit_reentry_preserves_real_report_and_consumed_authority(self):
        host = self.host_for(proposal(), proposal())
        def reenter(event):
            if event.event_type is E.ACTION_SUBMITTED:
                self.swallowed_reentry()
        self.audit_hook = reenter
        report = host.run_once(self.request)
        self.assertIs(report, self.reports[0])
        self.assertEqual(report.outcome, Outcome.SUCCEEDED)
        self.assert_lifecycle(report, READ)
        self.handlers["get_work_order"].assert_called_once()
        self.propose.assert_called_once()
        self.submit.assert_called_once()
        self.assertTrue(all(record.consumed for record in self.service._records.values()))
        self.assert_stopped_without_more_work()

    def test_uncaught_review_reentry_preserves_controller_failure_report(self):
        host = self.host_for(proposal("update_work_order_status"), proposal())
        self.display.side_effect = lambda review: host.run_once(self.request)
        report = host.run_once(self.request)
        self.assertIs(report, self.reports[0])
        self.assertEqual(report.outcome, Outcome.REVIEW_FAILED)
        self.assertTrue(report.terminal)
        self.assertIsNone(report.decision)  # Issuance raised before returning to controller.
        self.assert_lifecycle(report, UPDATED[:4])
        self.handlers["update_work_order_status"].assert_not_called()
        self.assertEqual(self.fixture._statuses["WO-1001"], "open")
        self.assert_stopped_without_more_work()

    def test_reentry_then_handler_interrupt_preserves_original_and_attached_report(self):
        for error in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(kind=type(error)):
                host = self.host_for(proposal("update_work_order_status"), proposal())
                def handler(action):
                    self.swallowed_reentry()
                    self.fixture._statuses[action.target] = "closed"
                    raise error
                self.handlers["update_work_order_status"].side_effect = handler
                with self.assertRaises(type(error)) as caught:
                    host.run_once(self.request)
                self.assertIs(caught.exception, error)
                report = error.controller_report
                self.assertIs(error, self.raised[-1][0])
                self.assertIs(report, self.raised[-1][1])
                self.assertEqual(report.execution_outcome, ExecutionOutcome.UNCERTAIN)
                self.assertTrue(report.terminal)
                self.assert_lifecycle(report, UPDATED[:-1] + ["execution_failed"])
                self.assertEqual(self.fixture._statuses["WO-1001"], "closed")
                self.assert_stopped_without_more_work()

    def test_unexpected_controller_error_after_reentry_is_not_replaced(self):
        host = self.host_for(proposal(), proposal())
        error = RuntimeError("PRIVATE_TEST_DIAGNOSTIC")
        report = ControllerReport(action_id=uuid4(), outcome=Outcome.UNCERTAIN, terminal=True)
        error.controller_report = report
        def fail(data):
            self.swallowed_reentry()
            raise error
        self.submit.side_effect = fail
        with self.assertRaises(RuntimeError) as caught:
            host.run_once(self.request)
        self.assertIs(caught.exception, error)
        self.assertIs(caught.exception.controller_report, report)
        self.assert_stopped_without_more_work()

    def test_constructor_borrows_exact_components_and_stopped_is_read_only(self):
        host = self.host_for(proposal())
        self.assertIs(host._controller, self.controller)
        self.assertIs(host._adapter, self.adapter)
        with self.assertRaises(AttributeError):
            host.stopped = False
        for controller, adapter in ((None, self.adapter), (self.controller, self.provider)):
            with self.assertRaises(TypeError):
                ProviderHost(controller=controller, adapter=adapter)
        self.assert_no_submission()
        self.provider.close.assert_not_called()

    @unittest.skipUnless(SDK_AVAILABLE, "optional groq/httpx extra not installed")
    def test_sdk_mock_transport_handoff_and_caller_owned_cleanup(self):
        # No import-time SDK side effects or ambient credentials; block real I/O
        # before SDK import. Only this one test adds SDK coverage to the host layer.
        self.enterContext(patch("os.environ", {}))
        blocked = [self.enterContext(patch(target, side_effect=AssertionError("Network forbidden")))
                   for target in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
                                  "socket.create_connection", "socket.socket.connect",
                                  "socket.socket.connect_ex", "socket.socket.sendto")]
        import httpx
        from controlled_agent.groq_provider import GroqProvider
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps({
                    "type": "proposal", "proposal": proposal()})}}]})
        transport = httpx.MockTransport(respond)
        transport.close = Mock(wraps=transport.close)
        with GroqProvider(api_key="dummy-offline-host-key", transport=transport) as provider:
            host = ProviderHost(controller=self.controller, adapter=ProposalAdapter(provider))
            report = host.run_once(self.request)
            self.assertIs(report, self.reports[0])
            self.assert_lifecycle(report, READ)
            self.handlers["get_work_order"].assert_called_once()
            self.submit.assert_called_once()
            self.assertEqual(len(requests), 1)
            self.assertNotIn("dummy-offline-host-key", requests[0].content.decode())
            self.assertNotIn("dummy-offline-host-key", self.path.read_text())
            transport.close.assert_not_called()
        transport.close.assert_called_once()  # Caller, not ProviderHost, closes it.
        for mock in blocked:
            mock.assert_not_called()
