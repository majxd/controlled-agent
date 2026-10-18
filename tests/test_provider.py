"""Provider failures stop before submission; proposals retain existing controls."""

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
import unittest
from unittest.mock import Mock, patch

from controlled_agent.approval import HumanApprovalAdapter
from controlled_agent.audit import JsonlAuditWriter
from controlled_agent.authorization import AuthorizationService
from controlled_agent.contracts import CallerContext, Decision
from controlled_agent.controller import Controller, Outcome
from controlled_agent.demo import TOOLS, PERMISSIONS, WORK_ORDERS, format_work_order_approval
from controlled_agent.demo_tools import FakeWorkOrderTools
from controlled_agent.executor import Executor
from controlled_agent.mock_provider import MockProvider
from controlled_agent.provider import (
    ProviderRequest, ProposalAdapter, ProviderError, ProviderFailure,
)


def proposal(tool="get_work_order", target="WO-1001"):
    args = {"work_order_id": target}
    if tool == "update_work_order_status":
        args["new_status"] = "closed"
    return {"tool_name": tool, "arguments": args}


class SequenceProvider:
    """Test-only sequence: each explicit call consumes exactly one response."""

    def __init__(self, *responses):
        self.remaining = list(responses)
        self.calls = 0

    def propose(self, request):
        self.calls += 1
        response = self.remaining.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


DENIED = ["action_submitted", "governance_decision"]
READ = DENIED + ["authorization_issued", "execution_started", "execution_succeeded"]
REVIEWED = DENIED + ["approval_requested", "approval_result"]
UPDATED = REVIEWED + ["authorization_issued", "execution_started", "execution_succeeded"]


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.request = ProviderRequest(
            prompt="Inspect the synthetic sample work order.",
            tools=tuple(rule.definition for rule in TOOLS.values()),
        )
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "audit.jsonl"
        self.writer = self.enterContext(JsonlAuditWriter(self.path))
        self.events = []

        def audit(event):
            self.writer(event)
            self.events.append(event)

        self.audit = audit
        self.new_id = self.enterContext(patch(
            "controlled_agent.controller.uuid4", wraps=uuid4,
        ))
        self.read = Mock(return_value="approve")
        self.display = Mock(return_value=None)
        self.service = AuthorizationService(
            tools=TOOLS, permissions=PERMISSIONS, resources=WORK_ORDERS,
            audit_write=self.audit,
            approval_formatter=format_work_order_approval,
            human_approval=HumanApprovalAdapter(display=self.display, read_response=self.read),
        )
        self.fixture = FakeWorkOrderTools(authorization=self.service)
        self.handlers = {key: Mock(wraps=value) for key, value in self.fixture.handlers.items()}
        self.executor = Executor(authorization=self.service, handlers=self.handlers)
        self.controller = Controller(
            authorization=self.service,
            executor=self.executor,
            caller=CallerContext(caller_id="demo_operator"),
        )
        self.submit = Mock(wraps=self.controller.submit)

    def run_provider(self, provider):
        data = ProposalAdapter(provider).propose(self.request)
        return None if data is None else self.submit(data)

    def records(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def assert_lifecycle(self, report, expected, *, service=None):
        records = self.records()
        action_records = [r for r in records if r["action_id"] == str(report.action_id)]
        self.assertEqual([r["event_type"] for r in action_records], expected)
        self.assertTrue(all(r["caller"] == "demo_operator" for r in action_records))
        self.assertEqual(
            [(r["action_id"], r["event_type"]) for r in records],
            [(str(e.action_id), e.event_type.value) for e in self.events],
        )
        self.assertIn(report.action_id, (service or self.service)._seen)
        self.assertEqual(action_records[1]["details"]["decision"], report.decision.value)

    def assert_no_effect(self):
        self.submit.assert_not_called()
        self.assertEqual(self.events, [])
        self.assertEqual(self.path.read_bytes(), b"")
        self.new_id.assert_not_called()
        self.assertEqual(self.service._seen, set())
        self.assertEqual(self.service._records, {})
        self.assertEqual(self.fixture._statuses, {"WO-1001": "open", "WO-9001": "open"})
        for handler in self.handlers.values():
            handler.assert_not_called()
        self.read.assert_not_called()
        self.display.assert_not_called()

    def test_public_request_is_immutable_metadata_only(self):
        with self.assertRaises(FrozenInstanceError):
            self.request.prompt = "changed"
        with self.assertRaises(TypeError):
            self.request.tools[0].argument_schema["type"] = "changed"
        with self.assertRaises(TypeError):
            ProviderRequest(prompt="sample", tools=(TOOLS["get_work_order"],))
        with self.assertRaises(TypeError):
            ProviderRequest(prompt="sample", tools=[], caller="model")
        with self.assertRaises(TypeError):
            ProviderRequest(prompt="sample", tools=[])
        for prompt in ("", "a" * 4097, None):
            with self.subTest(prompt_type=type(prompt)), self.assertRaises(ValueError):
                ProviderRequest(prompt=prompt, tools=self.request.tools)

    def test_adapter_copies_plain_data_without_normalizing_semantics(self):
        data = proposal("unknown")
        copied = ProposalAdapter(MockProvider(data)).propose(self.request)
        data["arguments"]["work_order_id"] = "changed"
        self.assertEqual(copied, proposal("unknown"))
        self.assertIs(type(copied), dict)
        self.assertIs(type(copied["arguments"]), dict)

    def test_read_submits_once_with_host_identity_and_executes_once(self):
        result = self.run_provider(MockProvider(proposal()))
        self.assertEqual(result.outcome, Outcome.SUCCEEDED)
        self.submit.assert_called_once_with(proposal())
        self.handlers["get_work_order"].assert_called_once()
        self.handlers["update_work_order_status"].assert_not_called()
        self.assertTrue(all(e.action_id == result.action_id for e in self.events))
        self.assertTrue(all(e.caller.caller_id == "demo_operator" for e in self.events))
        self.new_id.assert_called_once()
        self.assertEqual(self.service._seen, {result.action_id})
        self.assert_lifecycle(result, READ)
        self.assertEqual(self.fixture._statuses, {"WO-1001": "open", "WO-9001": "open"})

    def test_update_requires_exact_human_review(self):
        result = self.run_provider(MockProvider(proposal("update_work_order_status")))
        self.assertEqual(result.decision, Decision.REQUIRE_APPROVAL)
        self.assertEqual(result.outcome, Outcome.SUCCEEDED)
        self.read.assert_called_once()
        review = self.display.call_args.args[0]
        self.assertEqual(review.action.arguments, proposal("update_work_order_status")["arguments"])
        self.assertEqual(review.action.action_id, result.action_id)
        self.assertEqual(review.action.caller.caller_id, "demo_operator")
        self.assertEqual(review.action.target, "WO-1001")
        self.assertEqual(review.action.tool_name, "update_work_order_status")
        self.assert_lifecycle(result, UPDATED)
        self.handlers["update_work_order_status"].assert_called_once()
        self.assertEqual(self.fixture._statuses["WO-1001"], "closed")

    def test_human_decline_never_mutates(self):
        self.read.return_value = "decline"
        result = self.run_provider(MockProvider(proposal("update_work_order_status")))
        self.assertEqual(result.outcome, Outcome.APPROVAL_NOT_GRANTED)
        self.assert_lifecycle(result, REVIEWED)
        self.assertEqual(self.records()[-1]["details"]["outcome"], "declined")
        self.handlers["update_work_order_status"].assert_not_called()
        self.assertEqual(self.fixture._statuses["WO-1001"], "open")

    def test_protected_target_reaches_governance_and_is_denied(self):
        result = self.run_provider(MockProvider(proposal("update_work_order_status", "WO-9001")))
        self.assertEqual(result.outcome, Outcome.DENIED)
        self.submit.assert_called_once()
        self.read.assert_not_called()
        self.handlers["update_work_order_status"].assert_not_called()
        self.assertEqual(self.fixture._statuses["WO-9001"], "open")
        self.assert_lifecycle(result, DENIED)
        self.assertEqual(self.records()[-1]["details"]["reason_code"], "PROTECTED_TARGET")

    def test_unknown_tool_reaches_governance(self):
        result = self.run_provider(MockProvider(proposal("unknown")))
        self.assertEqual(result.outcome, Outcome.DENIED)
        self.assert_lifecycle(result, DENIED)
        self.assertEqual(self.records()[-1]["details"]["reason_code"], "UNKNOWN_TOOL")
        self.submit.assert_called_once()
        for handler in self.handlers.values():
            handler.assert_not_called()

    def test_unknown_target_reaches_governance(self):
        result = self.run_provider(MockProvider(proposal(target="WO-UNKNOWN")))
        self.assertEqual(result.outcome, Outcome.DENIED)
        self.submit.assert_called_once()
        self.assert_lifecycle(result, DENIED)
        self.assertEqual(self.records()[-1]["details"]["reason_code"], "INVALID_TARGET")
        for handler in self.handlers.values():
            handler.assert_not_called()

    def test_no_action_has_no_submission_or_audit(self):
        self.assertIsNone(self.run_provider(MockProvider()))
        self.assert_no_effect()

    def test_invalid_output_and_authority_claims_stop_before_submission(self):
        cases = ["no_action", {}, [], proposal() | {"approved": True},
                 proposal() | {"caller": "admin"},
                 {"tool_name": "get_work_order", "arguments": {"nested": {}}},
                 {"tool_name": "x", "arguments": {"x": "a" * 257}}]
        for data in cases:
            with self.subTest(data_type=type(data)), self.assertRaises(ProviderError) as caught:
                self.run_provider(MockProvider(data))
            self.assertEqual(caught.exception.reason, ProviderFailure.INVALID_OUTPUT)
        self.assert_no_effect()

    def test_typed_failures_stop_before_submission(self):
        for reason in ProviderFailure:
            with self.subTest(reason=reason), self.assertRaises(ProviderError) as caught:
                self.run_provider(MockProvider(failure=reason))
            self.assertEqual(caught.exception.reason, reason)
        self.assert_no_effect()

    def test_unexpected_failure_and_timeout_are_sanitized_without_retry(self):
        for error, reason in [(RuntimeError("SECRET"), ProviderFailure.UNAVAILABLE),
                              (TimeoutError("SECRET"), ProviderFailure.TIMEOUT)]:
            provider = Mock()
            provider.propose.side_effect = error
            with self.assertRaises(ProviderError) as caught:
                self.run_provider(provider)
            self.assertEqual(str(caught.exception), reason.value)
            provider.propose.assert_called_once_with(self.request)
        self.assert_no_effect()

    def test_interruptions_propagate_without_submission(self):
        for error in (KeyboardInterrupt(), SystemExit()):
            provider = Mock()
            provider.propose.side_effect = error
            with self.assertRaises(type(error)):
                self.run_provider(provider)
            provider.propose.assert_called_once()
        self.assert_no_effect()

    def test_provider_failure_preserves_session_and_queued_response(self):
        update = proposal("update_work_order_status")
        first = self.run_provider(MockProvider(update))
        self.assertEqual(first.outcome, Outcome.SUCCEEDED)
        self.assert_lifecycle(first, UPDATED)
        seen = self.service._seen.copy()
        records = dict(self.service._records)
        consumed = {ref: entry.consumed for ref, entry in records.items()}
        self.assertTrue(consumed and all(consumed.values()))
        state = self.fixture._statuses.copy()
        persisted = self.path.read_bytes()
        event_count = len(self.events)
        provider = SequenceProvider(RuntimeError("SECRET"), proposal())
        adapter = ProposalAdapter(provider)
        controller = self.controller
        service = self.service
        fixture = self.fixture
        executor = self.executor
        writer = self.writer

        with self.assertRaises(ProviderError) as caught:
            data = adapter.propose(self.request)
            if data is not None:
                self.submit(data)

        self.assertEqual(caught.exception.reason, ProviderFailure.UNAVAILABLE)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.remaining, [proposal()])
        self.assertEqual(self.submit.call_count, 1)
        self.assertEqual(self.new_id.call_count, 1)
        self.assertEqual(self.service._seen, seen)
        self.assertEqual(self.service._records, records)
        self.assertEqual({ref: e.consumed for ref, e in records.items()}, consumed)
        self.assertEqual(self.fixture._statuses, state)
        self.assertEqual(self.path.read_bytes(), persisted)
        self.assertEqual(len(self.events), event_count)
        self.assertIs(self.controller, controller)
        self.assertIs(self.controller._authorization, service)
        self.assertIs(self.fixture, fixture)
        self.assertIs(self.controller._executor, executor)
        self.assertIs(self.writer, writer)
        self.handlers["get_work_order"].assert_not_called()
        self.assertEqual(self.handlers["update_work_order_status"].call_count, 1)
        self.assertEqual(self.read.call_count, 1)

        # Only a new explicit host call consumes the previously queued read.
        data = adapter.propose(self.request)
        read_result = self.submit(data)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(provider.remaining, [])
        self.assertEqual(read_result.outcome, Outcome.SUCCEEDED)
        self.assertEqual(read_result.result["status"], "closed")
        self.assert_lifecycle(read_result, READ)
        self.handlers["get_work_order"].assert_called_once()

        stale = self.run_provider(MockProvider(update))
        self.assertTrue(stale.stale_baseline)
        self.assertEqual(stale.outcome, Outcome.REJECTED)
        self.assert_lifecycle(stale, UPDATED[:-1] + ["execution_failed"])
        self.assertEqual(self.fixture._statuses, {"WO-1001": "closed", "WO-9001": "open"})
        self.assertEqual(self.handlers["update_work_order_status"].call_count, 2)
        self.assertEqual(self.service._seen, {first.action_id, read_result.action_id, stale.action_id})
        self.assertTrue(all(e.consumed for e in self.service._records.values()))

    def test_structural_validity_does_not_grant_permission(self):
        service = AuthorizationService(
            tools=TOOLS, permissions={}, resources=WORK_ORDERS, audit_write=self.audit,
        )
        fixture = FakeWorkOrderTools(authorization=service)
        handlers = {key: Mock(wraps=value) for key, value in fixture.handlers.items()}
        controller = Controller(authorization=service, executor=Executor(
            authorization=service, handlers=handlers), caller=CallerContext(caller_id="demo_operator"))
        data = ProposalAdapter(MockProvider(proposal())).propose(self.request)
        result = controller.submit(data)
        self.assertEqual(result.outcome, Outcome.DENIED)
        self.assert_lifecycle(result, DENIED, service=service)
        self.assertEqual(fixture._statuses, {"WO-1001": "open", "WO-9001": "open"})
        for handler in handlers.values():
            handler.assert_not_called()

    def test_argument_approval_claim_is_only_data_and_is_denied(self):
        data = proposal("update_work_order_status")
        data["arguments"]["approved"] = True
        result = self.run_provider(MockProvider(data))
        self.assert_lifecycle(result, DENIED)
        self.assertEqual(result.outcome, Outcome.DENIED)
        self.submit.assert_called_once()
        self.read.assert_not_called()
        self.handlers["update_work_order_status"].assert_not_called()
        self.assertEqual(self.fixture._statuses["WO-1001"], "open")

    def test_hostile_output_hooks_are_not_called(self):
        class Hostile(dict):
            def items(self):
                raise AssertionError("must not inspect custom mappings")
        for data in (Hostile(proposal()), {"tool_name": "x", "arguments": Hostile()}):
            with self.assertRaises(ProviderError) as caught:
                self.run_provider(MockProvider(data))
            self.assertEqual(caught.exception.reason, ProviderFailure.INVALID_OUTPUT)
        self.assert_no_effect()

    def test_typed_provider_error_message_is_not_forwarded(self):
        error = ProviderError(ProviderFailure.REFUSAL)
        error.args = ("SECRET",)
        provider = Mock()
        provider.propose.side_effect = error
        with self.assertRaises(ProviderError) as caught:
            self.run_provider(provider)
        self.assertEqual(str(caught.exception), "refusal")
        self.assert_no_effect()
