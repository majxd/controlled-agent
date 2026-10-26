"""Offline fixture collection only, including optional SDK/MockTransport checks."""

from collections import deque
from dataclasses import FrozenInstanceError, fields
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from controlled_agent import proposal_collection as collection
from controlled_agent.proposal_evaluation import (
    CASES, FailureObservation, ResponseObservation, assess, score,
)
from controlled_agent.mock_provider import MockProvider
from controlled_agent.provider import ProposalAdapter, ProviderError, ProviderFailure
from test_proposal_evaluation import FIXTURES


SDK_AVAILABLE = all(importlib.util.find_spec(name) for name in ("groq", "httpx"))


class SequenceProvider:
    """Test-only queue with caller-owned cleanup; not a production provider API."""

    def __init__(self, values):
        self.remaining = deque(values)
        self.requests = []
        self.close = Mock()

    def propose(self, request):
        self.requests.append(request)
        value = self.remaining.popleft()
        if isinstance(value, BaseException):
            raise value
        return value


class CollectionTests(unittest.TestCase):
    def setUp(self):
        forbidden = (
            "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
            "socket.getnameinfo", "socket.create_connection", "socket.socket.connect",
            "socket.socket.connect_ex", "socket.socket.sendto", "socket.socket.sendmsg",
            "getpass.getpass", "builtins.input",
            "controlled_agent.controller.Controller.submit",
            "controlled_agent.controller.uuid4",
            "controlled_agent.provider_host.ProviderHost.run_once",
            "controlled_agent.authorization.AuthorizationService.__init__",
            "controlled_agent.authorization.AuthorizationService.issue",
            "controlled_agent.authorization.AuthorizationService.issue_proposal",
            "controlled_agent.authorization.AuthorizationService.consume",
            "controlled_agent.approval.HumanApprovalAdapter.__init__",
            "controlled_agent.executor.Executor.execute",
            "controlled_agent.demo_tools.FakeWorkOrderTools.__init__",
            "controlled_agent.demo_tools.FakeWorkOrderTools._get_work_order",
            "controlled_agent.demo_tools.FakeWorkOrderTools._update_work_order_status",
            "controlled_agent.audit.JsonlAuditWriter.__init__",
            "controlled_agent.audit.JsonlAuditWriter.__call__",
            "controlled_agent.governance.evaluate_action", "controlled_agent.demo.evaluate_demo",
        )
        self.guards = [self.enterContext(patch(name, side_effect=AssertionError("Forbidden side effect")))
                       for name in forbidden]

    def tearDown(self):
        for guard in self.guards:
            guard.assert_not_called()

    def test_fixed_order_public_requests_and_equivalent_scoring(self):
        provider = SequenceProvider(FIXTURES.values())
        adapter = ProposalAdapter(provider)
        with patch.object(adapter, "propose", wraps=adapter.propose) as propose:
            result = collection.collect(adapter)
        self.assertEqual(propose.call_count, 8)
        self.assertEqual(provider.requests, [c.request for c in CASES])
        for actual, case in zip(provider.requests, CASES):
            self.assertIs(actual, case.request)
            self.assertEqual({f.name for f in fields(actual)}, {"prompt", "tools"})
        self.assertEqual(result.batch, assess(
            (key, ResponseObservation(response=value)) for key, value in FIXTURES.items()))
        self.assertEqual(result.batch.intent_matches, 8)
        self.assertTrue(result.batch.results[4].correct)  # Protected target: intent only.
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.adapter_invocations, 8)
        self.assertIsNone(result.stopped_case_id)
        self.assertFalse(provider.remaining)
        provider.close.assert_not_called()

    def test_quality_mismatch_and_no_action_do_not_stop(self):
        for provider in (MockProvider(), MockProvider(FIXTURES["R1"])):
            with patch.object(provider, "propose", wraps=provider.propose) as propose:
                result = collection.collect(ProposalAdapter(provider))
            self.assertEqual(propose.call_count, 8)
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.batch.correctness_assessed, 8)
            self.assertLess(result.batch.intent_matches, 8)
            self.assertEqual(sum(result.batch.failures.values()), 0)

    def test_every_fixed_failure_stops_at_first_middle_or_last_case(self):
        values = list(FIXTURES.values())
        for code in ProviderFailure:
            for index in (0, 3, 7):
                with self.subTest(code=code, index=index):
                    error = ProviderError(code)
                    error.args = ("PRIVATE_DIAGNOSTIC",)
                    provider = SequenceProvider(values[:index] + [error, "UNUSED"])
                    result = collection.collect(ProposalAdapter(provider))
                    self.assertEqual(result.adapter_invocations, index + 1)
                    self.assertEqual(len(provider.requests), index + 1)
                    self.assertEqual(list(provider.remaining), ["UNUSED"])
                    self.assertEqual(result.status, "provider_failure")
                    self.assertEqual(result.stopped_case_id, CASES[index].case_id)
                    self.assertEqual(result.batch.total_observations, index + 1)
                    self.assertEqual(result.batch.correctness_assessed, index)
                    self.assertIs(result.batch.results[-1].failure, code)
                    self.assertIsNone(result.batch.results[-1].correct)
                    self.assertEqual(result.batch.unobserved_case_ids,
                                     tuple(c.case_id for c in CASES[index + 1:]))
                    self.assertNotIn("PRIVATE", repr(result))
                    provider.close.assert_not_called()

    def test_adapter_malformed_output_and_mapped_exceptions_remain_failure_only(self):
        for value, code in (("no_action", ProviderFailure.INVALID_OUTPUT),
                            ({}, ProviderFailure.INVALID_OUTPUT),
                            (TimeoutError("PRIVATE"), ProviderFailure.TIMEOUT),
                            (RuntimeError("PRIVATE"), ProviderFailure.UNAVAILABLE)):
            provider = SequenceProvider([value, None])
            result = collection.collect(ProposalAdapter(provider))
            self.assertEqual(result.status, "provider_failure")
            self.assertEqual(result.adapter_invocations, 1)
            self.assertIs(result.batch.results[0].failure, code)
            self.assertIsNone(result.batch.results[0].structurally_valid)
            self.assertEqual(result.batch.correctness_assessed, 0)
            self.assertEqual(list(provider.remaining), [None])

    def test_interruptions_preserve_identity_partial_results_and_queue(self):
        for error in (KeyboardInterrupt("PRIVATE"), SystemExit(7)):
            provider = SequenceProvider([FIXTURES["R1"], error, "UNUSED"])
            with self.assertRaises(type(error)) as caught:
                collection.collect(ProposalAdapter(provider))
            self.assertIs(caught.exception, error)
            result = error.collection_result
            self.assertEqual(result.status, "interrupted")
            self.assertEqual(result.adapter_invocations, 2)
            self.assertEqual(result.stopped_case_id, "R2")
            self.assertEqual(result.batch.total_observations, 1)
            self.assertEqual(result.batch.unobserved_case_ids, tuple(c.case_id for c in CASES[1:]))
            self.assertEqual(list(provider.remaining), ["UNUSED"])
            self.assertNotIn("PRIVATE", repr(result))
            provider.close.assert_not_called()

    def test_unexpected_adapter_error_propagates_without_fabricating_failure(self):
        error = RuntimeError("PRIVATE")
        adapter = ProposalAdapter(MockProvider())
        with patch.object(adapter, "propose", side_effect=[None, error, None]) as propose:
            with self.assertRaises(RuntimeError) as caught:
                collection.collect(adapter)
        self.assertIs(caught.exception, error)
        self.assertEqual(propose.call_count, 2)
        self.assertEqual(error.collection_result.status, "unexpected_failure")
        self.assertEqual(error.collection_result.batch.total_observations, 1)
        self.assertEqual(sum(error.collection_result.batch.failures.values()), 0)

    def test_scoring_provider_error_is_unexpected_even_after_provider_failure(self):
        for value in (None, ProviderError(ProviderFailure.TIMEOUT)):
            error = ProviderError(ProviderFailure.REFUSAL)
            provider = SequenceProvider([None, value, "UNUSED"])
            first = score("R1", ResponseObservation(response=None))
            with patch.object(collection, "score", side_effect=[first, error]):
                with self.assertRaises(ProviderError) as caught:
                    collection.collect(ProposalAdapter(provider))
            self.assertIs(caught.exception, error)
            result = error.collection_result
            self.assertEqual(result.status, "unexpected_failure")
            self.assertEqual(result.batch.results, (first,))
            self.assertEqual(sum(result.batch.failures.values()), 0)
            self.assertEqual(result.adapter_invocations, 2)
            self.assertEqual(list(provider.remaining), ["UNUSED"])

    def test_summary_provider_error_is_unexpected_and_retains_scored_records(self):
        original = collection._summarize
        for provider in (MockProvider(), MockProvider(failure=ProviderFailure.TIMEOUT)):
            error = ProviderError(ProviderFailure.REFUSAL)
            calls = 0
            def summarize(results):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise error
                return original(results)
            with patch.object(collection, "_summarize", side_effect=summarize):
                with self.assertRaises(ProviderError) as caught:
                    collection.collect(ProposalAdapter(provider))
            self.assertIs(caught.exception, error)
            result = error.collection_result
            self.assertEqual(result.status, "unexpected_failure")
            self.assertEqual(result.batch.total_observations, result.adapter_invocations)
            self.assertEqual(result.batch.failures[ProviderFailure.REFUSAL], 0)

    def test_partial_snapshot_failure_cannot_replace_original_exception(self):
        for secondary in (RuntimeError("SECONDARY"), KeyboardInterrupt()):
            error = SystemExit(9)
            adapter = ProposalAdapter(MockProvider())
            with patch.object(adapter, "propose", side_effect=error) as propose:
                with patch.object(collection, "_summarize", side_effect=secondary):
                    with self.assertRaises(SystemExit) as caught:
                        collection.collect(adapter)
            self.assertIs(caught.exception, error)
            self.assertFalse(hasattr(error, "collection_result"))
            propose.assert_called_once()

    def test_exception_rejecting_attachment_keeps_original_identity(self):
        class Unattachable(KeyboardInterrupt):
            def __setattr__(self, name, value):
                raise RuntimeError("Assignment rejected")
        error = Unattachable()
        provider = SequenceProvider([None, error, "UNUSED"])
        with self.assertRaises(Unattachable) as caught:
            collection.collect(ProposalAdapter(provider))
        self.assertIs(caught.exception, error)
        self.assertFalse(hasattr(error, "collection_result"))
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(list(provider.remaining), ["UNUSED"])

    def test_results_are_immutable_fixed_provenance_and_do_not_retain_raw_data(self):
        raw = {"tool_name": "get_work_order", "arguments": {"work_order_id": "PRIVATE_SENTINEL"}}
        result = collection.collect(ProposalAdapter(MockProvider(raw)))
        raw["arguments"].clear()
        self.assertNotIn("PRIVATE", repr(result))
        self.assertEqual(result.provenance, "adapter_collection_source_unverified")
        self.assertEqual(result.batch.evidence, "scored_observations_source_unverified")
        self.assertEqual({f.name for f in fields(result)}, {
            "provenance", "batch", "status", "adapter_invocations", "stopped_case_id"})
        with self.assertRaises(FrozenInstanceError):
            result.status = "completed"
        with self.assertRaises(FrozenInstanceError):
            result.batch.results[0].correct = True
        with self.assertRaises(TypeError):
            result.batch.failures[ProviderFailure.TIMEOUT] = 0
        with self.assertRaises(TypeError):
            collection.collect(ProposalAdapter(MockProvider()), live=True)
        with self.assertRaises(TypeError):
            collection.CollectionResult(batch=result.batch, status="completed", adapter_invocations=8,
                                        stopped_case_id=None, provenance="live")

    def test_caller_retains_returned_or_attached_result_before_cleanup_failure(self):
        for interrupt in (False, True):
            provider = SequenceProvider([None, KeyboardInterrupt()] if interrupt else [None] * 8)
            result = None
            cleanup_error = RuntimeError("PRIVATE_CLEANUP")
            provider.close.side_effect = cleanup_error
            try:
                try:
                    result = collection.collect(ProposalAdapter(provider))
                except KeyboardInterrupt as error:
                    result = error.collection_result
                    raise
                finally:
                    # The caller retains evidence before attempting its own cleanup.
                    self.assertIsNotNone(result)
                    provider.close.assert_not_called()
                    try:
                        provider.close()
                    except RuntimeError as error:
                        self.assertIs(error, cleanup_error)
            except KeyboardInterrupt:
                self.assertTrue(interrupt)
            provider.close.assert_called_once()
            self.assertEqual(result.batch.total_observations, 1 if interrupt else 8)
            self.assertNotIn("PRIVATE", repr(result))

    def test_dependency_free_collection_without_execution_imports_or_io(self):
        program = '''
import sys
forbidden = {"groq", "httpx", "controlled_agent.controller", "controlled_agent.provider_host",
             "controlled_agent.executor", "controlled_agent.audit", "controlled_agent.demo_tools"}
def guard(event, args):
    if event == "import" and args[0] in forbidden:
        raise AssertionError("Forbidden import")
    if event.startswith("socket.") or event == "builtins.input":
        raise AssertionError("Forbidden I/O")
    if event == "open" and args[2] & 3:
        raise AssertionError("Forbidden write")
sys.addaudithook(guard)
from controlled_agent.proposal_collection import collect
from controlled_agent.provider import ProposalAdapter
from controlled_agent.mock_provider import MockProvider
result = collect(ProposalAdapter(MockProvider()))
assert result.status == "completed" and result.adapter_invocations == 8
assert not forbidden.intersection(sys.modules)
'''
        result = subprocess.run([sys.executable, "-B", "-S", "-c", program],
                                cwd=Path(__file__).resolve().parents[1], env={},
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    @unittest.skipUnless(SDK_AVAILABLE, "Optional Groq/HTTPX not installed")
    def test_actual_sdk_mock_transport_full_fixture_collection_and_cleanup(self):
        self.sdk_collection(fail_at=None)

    @unittest.skipUnless(SDK_AVAILABLE, "Optional Groq/HTTPX not installed")
    def test_actual_sdk_mock_transport_failure_has_no_retry_or_later_request(self):
        self.sdk_collection(fail_at=2)

    def sdk_collection(self, *, fail_at):
        import httpx
        from controlled_agent.groq_provider import GroqProvider
        requests, responses = [], []
        values = deque(FIXTURES.values())
        def handle(request):
            index = len(requests)
            requests.append(request)
            body = json.loads(request.content)
            self.assertEqual(body["messages"][1], {"role": "user", "content": CASES[index].request.prompt})
            descriptions = json.loads(body["messages"][0]["content"].split("Public tool descriptions follow: ")[1])
            self.assertEqual([t["name"] for t in descriptions], [t.name for t in CASES[index].request.tools])
            self.assertTrue(all(set(t) == {"name", "description", "argument_schema"} for t in descriptions))
            self.assertNotIn("dummy-offline-key", request.content.decode())
            if index == fail_at:
                response = httpx.Response(429, json={"error": {"message": "PRIVATE"}})
            else:
                value = values.popleft()
                wire = {"type": "no_action"} if value is None else {"type": "proposal", "proposal": value}
                response = httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(wire)}}]})
            responses.append(response)
            return response
        transport = httpx.MockTransport(handle)
        transport.close = Mock(wraps=transport.close)
        provider = GroqProvider(api_key="dummy-offline-key", transport=transport)
        try:
            result = collection.collect(ProposalAdapter(provider))
            transport.close.assert_not_called()
        finally:
            provider.close()
        transport.close.assert_called_once()
        self.assertTrue(all(r.is_closed for r in responses))
        self.assertEqual(result.provenance, "adapter_collection_source_unverified")
        self.assertEqual(len(requests), 8 if fail_at is None else fail_at + 1)
        self.assertEqual(result.adapter_invocations, len(requests))
        if fail_at is None:
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.batch.intent_matches, 8)
        else:
            self.assertEqual(result.status, "provider_failure")
            self.assertEqual(result.batch.failures[ProviderFailure.RATE_LIMIT], 1)
            self.assertEqual(len(values), 8 - fail_at)
            self.assertNotIn("PRIVATE", repr(result))


if __name__ == "__main__":
    unittest.main()
