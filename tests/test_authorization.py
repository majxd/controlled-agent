"""Trusted issuance/consumption boundaries only; no executor or real audit sink."""

from copy import copy
from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from controlled_agent import (
    ActionRequest, AuditEventType, Authorization, AuthorizationState, CallerContext,
    Decision, GovernanceResult, Risk,
)
from controlled_agent.authorization import (
    AuditWriteError, AuthorizationService, DuplicateActionError, InvalidReferenceError,
    ReentrantCallError,
)
from controlled_agent.demo import PERMISSIONS, TOOLS, WORK_ORDERS


class RecordingAudit:
    """Test-only synchronous writer; never a production audit sink."""

    def __init__(self, fail_at=None, error=OSError):
        self.events = []
        self.fail_at = fail_at
        self.error = error

    def __call__(self, event):
        self.events.append(event)
        if event.event_type is self.fail_at:
            raise self.error("injected audit failure")


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.audit = RecordingAudit()

    def service(self, **changes):
        return AuthorizationService(**({
            "tools": TOOLS, "permissions": PERMISSIONS, "resources": WORK_ORDERS,
            "audit_write": self.audit,
        } | changes))

    def request(self, tool="get_work_order", target="WO-1001", **changes):
        arguments = {"work_order_id": target}
        if tool == "update_work_order_status":
            arguments["new_status"] = "closed"
        return ActionRequest(**({
            "action_id": uuid4(), "tool_name": tool, "arguments": arguments,
            "caller": CallerContext(caller_id="demo_operator"), "target": None,
        } | changes))

    def test_allow_issues_only_after_ordered_correlated_audit_gates(self):
        service = self.service()
        request = self.request()
        result, reference = service.issue(request)
        self.assertIs(result.decision, Decision.ALLOW)
        self.assertIsNotNone(reference)
        self.assertEqual([event.event_type for event in self.audit.events], [
            AuditEventType.ACTION_SUBMITTED, AuditEventType.GOVERNANCE_DECISION,
            AuditEventType.AUTHORIZATION_ISSUED,
        ])
        for event in self.audit.events:
            self.assertEqual(event.action_id, request.action_id)
            self.assertEqual(event.caller, request.caller)
        issued = self.audit.events[-1]
        self.assertEqual(issued.details["target"], "WO-1001")
        self.assertNotIn("arguments", issued.details)
        action = service.consume(reference)
        self.assertEqual(action.target, "WO-1001")
        self.assertEqual(len(self.audit.events), 3)  # Consumption is not dispatch.

    def test_deny_and_require_approval_never_issue_and_cannot_be_resubmitted(self):
        cases = (
            (self.request("update_work_order_status"), Decision.REQUIRE_APPROVAL),
            (self.request("update_work_order_status", "WO-9001"), Decision.DENY),
            (self.request("unknown"), Decision.DENY),
            (self.request(target="unknown"), Decision.DENY),
            (self.request(arguments={"work_order_id": "WO-1001", "approved": True}), Decision.DENY),
            (self.request(caller=CallerContext(caller_id="unprivileged")), Decision.DENY),
        )
        for request, expected in cases:
            with self.subTest(decision=expected, arguments=request.arguments):
                audit = RecordingAudit()
                service = self.service(audit_write=audit)
                result, reference = service.issue(request)
                self.assertIs(result.decision, expected)
                self.assertIsNone(reference)
                self.assertNotIn(AuditEventType.AUTHORIZATION_ISSUED,
                                 [event.event_type for event in audit.events])
                with self.assertRaises(DuplicateActionError):
                    service.issue(replace(self.request(), action_id=request.action_id))
                self.assertEqual(service._records, {})

    def test_issuance_rejects_supplied_results_records_flags_and_configuration(self):
        service = self.service()
        request = self.request()
        fabricated = GovernanceResult(action_id=request.action_id, decision=Decision.ALLOW,
                                       risk=Risk.LOW, reason_code="ALLOW", explanation="Untrusted.")
        record = Authorization(authorization_id=uuid4(), action=request, state=AuthorizationState.USABLE)
        for supplied in (fabricated, record, {"approved": True}):
            with self.subTest(supplied=type(supplied).__name__):
                with self.assertRaises(TypeError):
                    service.issue(supplied)
        for field, value in (("result", fabricated), ("authorization", record), ("approved", True),
                             ("permissions", PERMISSIONS), ("tools", TOOLS), ("resources", WORK_ORDERS),
                             ("evaluate", lambda _: fabricated)):
            with self.subTest(field=field):
                with self.assertRaises(TypeError):
                    service.issue(request, **{field: value})
        self.assertEqual(self.audit.events, [])
        self.assertEqual(service._records, {})

    def test_non_issued_audits_identify_resolution_without_claiming_full_validation(self):
        cases = (
            (self.request("update_work_order_status"), "WO-1001", True, Decision.REQUIRE_APPROVAL),
            (self.request("update_work_order_status", "WO-9001"), "WO-9001", False, Decision.DENY),
            (self.request("update_work_order_status", arguments={
                "work_order_id": "WO-9001", "new_status": "secret-invalid-status",
                "approved": True, "secret": "sensitive-payload",
            }), "WO-9001", False, Decision.DENY),
            (self.request(caller=CallerContext(caller_id="unprivileged")), "WO-1001", False, Decision.DENY),
            (self.request(arguments={"work_order_id": "WO-1001", "secret": "sensitive-payload"}),
             "WO-1001", False, Decision.DENY),
            (replace(self.request("update_work_order_status", "WO-9001"), target="WO-1001"),
             "WO-9001", False, Decision.DENY),
        )
        for request, target, validated, decision in cases:
            with self.subTest(request=request, validated=validated):
                audit = RecordingAudit()
                service = self.service(audit_write=audit)
                result, reference = service.issue(request)
                self.assertIs(result.decision, decision)
                self.assertIsNone(reference)
                self.assertEqual(service._records, {})
                submitted, evaluated = audit.events
                self.assertEqual(dict(submitted.details), {"submitted_tool_name": request.tool_name})
                self.assertEqual(dict(evaluated.details), {
                    "tool_name": request.tool_name, "resolved_target": target,
                    "action_validated": validated, "decision": result.decision.value,
                    "risk": result.risk.value, "reason_code": result.reason_code,
                })
                for event in (submitted, evaluated):
                    self.assertEqual(event.action_id, request.action_id)
                    self.assertEqual(event.caller, request.caller)
                    self.assertNotIn("arguments", event.details)
                    self.assertNotIn("sensitive-payload", str(event.details))
                    self.assertNotIn("secret-invalid-status", str(event.details))
                    # Even validated decision data is not registry authority.
                    with self.assertRaises(InvalidReferenceError):
                        service.consume(event)

    def test_unresolved_audit_context_does_not_retain_unknown_or_spoofed_claims(self):
        for request, tool_name in (
            (self.request(tool="unknown-secret-tool-" + "x" * 5000), None),
            (replace(self.request(target="unknown-secret-target"), target="WO-9001"), "get_work_order"),
            (replace(self.request(arguments={"target": "WO-9001", "secret": "private"}),
                     target="WO-9001"), "get_work_order"),
        ):
            with self.subTest(tool_name=tool_name):
                audit = RecordingAudit()
                service = self.service(audit_write=audit)
                result, reference = service.issue(request)
                self.assertIs(result.decision, Decision.DENY)
                self.assertIsNone(reference)
                self.assertEqual(audit.events[0].details["submitted_tool_name"], tool_name)
                details = audit.events[-1].details
                self.assertEqual(details["tool_name"], tool_name)
                self.assertIsNone(details["resolved_target"])
                self.assertFalse(details["action_validated"])
                for forbidden in ("unknown-secret", "WO-9001", "private"):
                    self.assertNotIn(forbidden, str([dict(e.details) for e in audit.events]))

    def test_missing_resource_metadata_cannot_become_resolved_audit_context(self):
        audit = RecordingAudit()
        service = self.service(resources={}, audit_write=audit)
        result, reference = service.issue(self.request("update_work_order_status", "WO-9001"))
        self.assertIs(result.decision, Decision.DENY)
        self.assertIsNone(reference)
        self.assertIsNone(audit.events[-1].details["resolved_target"])
        self.assertFalse(audit.events[-1].details["action_validated"])

    def test_validated_sensitive_arguments_are_not_audited_but_remain_bound(self):
        rules = TOOLS["get_work_order"]
        service = self.service(tools={"get_work_order": replace(rules, validate_arguments=lambda _: True)})
        request = self.request(arguments={"work_order_id": "WO-1001", "secret": {"token": "private-value"}})
        _, reference = service.issue(request)
        for event in self.audit.events:
            self.assertNotIn("arguments", event.details)
            self.assertNotIn("private-value", str(event.details))
            self.assertNotIn("secret", str(event.details))
        action = service.consume(reference)
        self.assertEqual(action.arguments, request.arguments)
        self.assertEqual(action.arguments["secret"]["token"], "private-value")
        with self.assertRaises(InvalidReferenceError):
            service.consume(reference)

    def test_audit_identifier_retention_is_bounded_without_truncating_into_false_identity(self):
        for name in ("x" * 128, "x" * 129, "line\nbreak", "nonascii-\u00e9"):
            with self.subTest(name=name):
                audit = RecordingAudit()
                rules = TOOLS["get_work_order"]
                rules = replace(rules, definition=replace(rules.definition, name=name),
                                resolve_target=lambda _: name, validate_arguments=lambda _: True)
                service = self.service(tools={name: rules}, permissions={"demo_operator": {name}},
                                       resources={name: {}}, audit_write=audit)
                request = self.request(tool=name, arguments={})
                _, reference = service.issue(request)
                retained = name if name == "x" * 128 else None
                self.assertEqual(audit.events[0].details["submitted_tool_name"], retained)
                self.assertEqual(audit.events[1].details["tool_name"], retained)
                self.assertEqual(audit.events[1].details["resolved_target"], retained)
                self.assertTrue(audit.events[1].details["action_validated"])
                self.assertEqual(audit.events[2].details["tool_name"], retained)
                self.assertEqual(audit.events[2].details["target"], retained)
                self.assertEqual(service.consume(reference).target, name)

    def test_decision_audit_failure_on_non_issued_paths_is_terminal(self):
        for target in ("WO-1001", "WO-9001"):
            for error in (OSError, KeyboardInterrupt, SystemExit):
                with self.subTest(target=target, error=error):
                    audit = RecordingAudit(AuditEventType.GOVERNANCE_DECISION, error)
                    service = self.service(audit_write=audit)
                    request = self.request("update_work_order_status", target)
                    with self.assertRaises(AuditWriteError if error is OSError else error):
                        service.issue(request)
                    self.assertEqual(service._records, {})
                    self.assertIn(request.action_id, service._seen)
                    self.assertFalse(service._busy)
                    self.assertEqual(audit.events[-1].details["resolved_target"], target)
                    audit.fail_at = None
                    with self.assertRaises(DuplicateActionError):
                        service.issue(request)
                    self.assertNotIn(AuditEventType.AUTHORIZATION_ISSUED, [e.event_type for e in audit.events])
                    self.assertIsNotNone(service.issue(self.request())[1])

    def test_duplicate_audit_failure_does_not_revoke_or_reissue_existing_authority(self):
        for gate in (AuditEventType.ACTION_SUBMITTED, AuditEventType.GOVERNANCE_DECISION):
            for error in (OSError, KeyboardInterrupt, SystemExit):
                with self.subTest(gate=gate, error=error):
                    audit = RecordingAudit()
                    policy = Mock(wraps=TOOLS["get_work_order"].evaluate_policy)
                    service = self.service(audit_write=audit, tools={"get_work_order": replace(
                        TOOLS["get_work_order"], evaluate_policy=policy,
                    )})
                    request = self.request()
                    _, reference = service.issue(request)
                    stored = service._records[reference].authorization.action
                    audit.fail_at, audit.error = gate, error
                    changed = replace(self.request(target="WO-9001"), action_id=request.action_id)
                    with self.assertRaises(AuditWriteError if error is OSError else error):
                        service.issue(changed)
                    policy.assert_called_once()
                    self.assertEqual(len(service._records), 1)
                    self.assertFalse(service._records[reference].consumed)
                    audit.fail_at = None
                    if gate is AuditEventType.GOVERNANCE_DECISION:
                        self.assertIsNone(audit.events[-1].details["resolved_target"])
                        self.assertFalse(audit.events[-1].details["action_validated"])
                    self.assertIs(service.consume(reference), stored)
                    with self.assertRaises(InvalidReferenceError):
                        service.consume(reference)
                    with self.assertRaises(DuplicateActionError):
                        service.issue(request)
                    self.assertEqual(sum(e.event_type is AuditEventType.AUTHORIZATION_ISSUED
                                         for e in audit.events), 1)

    def test_exact_validated_snapshot_survives_without_second_resolution_or_evaluation(self):
        rules = TOOLS["get_work_order"]
        resolver = Mock(side_effect=["WO-1001", AssertionError("Second resolution")])
        validator = Mock(wraps=rules.validate_arguments)
        policy = Mock(wraps=rules.evaluate_policy)
        service = self.service(tools={"get_work_order": replace(
            rules, resolve_target=resolver, validate_arguments=validator, evaluate_policy=policy,
        )})
        request = self.request()
        result, reference = service.issue(request)
        evaluated = policy.call_args.args[0]
        self.assertIs(validator.call_args.args[0], evaluated.arguments)
        self.assertIs(service._records[reference].authorization.action, evaluated)
        consumed = service.consume(reference)
        self.assertIs(consumed, evaluated)
        self.assertIsNot(consumed, request)
        self.assertIs(consumed.caller, request.caller)
        self.assertEqual(consumed.action_id, request.action_id)
        self.assertEqual(consumed.tool_name, request.tool_name)
        self.assertIsNone(request.target)
        resolver.assert_called_once()
        validator.assert_called_once()
        policy.assert_called_once()
        self.assertIs(result.decision, Decision.ALLOW)

    def test_nested_source_mutation_cannot_change_issued_action(self):
        rules = TOOLS["get_work_order"]
        # Trusted test schema accepts an additional nested JSON field.
        service = self.service(tools={"get_work_order": replace(rules, validate_arguments=lambda _: True)})
        source = {"work_order_id": "WO-1001", "options": {"items": ["original"]}}
        request = self.request(arguments=source)
        _, reference = service.issue(request)
        source["work_order_id"] = "WO-9001"
        source["options"]["items"].append("changed")
        action = service.consume(reference)
        self.assertEqual(action.target, "WO-1001")
        self.assertEqual(action.arguments["options"]["items"], ("original",))
        with self.assertRaises(TypeError):
            action.arguments["options"]["items"] = ()
        with self.assertRaises(FrozenInstanceError):
            action.target = "WO-9001"

    def test_consume_accepts_no_replacement_arguments_or_action(self):
        service = self.service()
        _, reference = service.issue(self.request())
        for field, value in (("action", self.request(target="WO-9001")),
                             ("arguments", {"work_order_id": "WO-9001"}),
                             ("caller", CallerContext(caller_id="other"))):
            with self.subTest(field=field):
                with self.assertRaises(TypeError):
                    service.consume(reference, **{field: value})
        self.assertEqual(service.consume(reference).target, "WO-1001")

    def test_unknown_forged_copied_and_cross_registry_references_are_rejected(self):
        service = self.service()
        other = self.service()
        _, reference = service.issue(self.request())
        _, foreign = other.issue(self.request())
        record = service._records[reference].authorization
        for supplied in (None, object(), uuid4(), record.authorization_id, record,
                         type(reference)(), copy(reference), foreign, {"approved": True}):
            with self.subTest(kind=type(supplied).__name__):
                with self.assertRaises(InvalidReferenceError):
                    service.consume(supplied)
        self.assertEqual(service.consume(reference).target, "WO-1001")
        self.assertEqual(other.consume(foreign).target, "WO-1001")

    def test_unknown_reference_does_not_invoke_custom_hash_or_equality(self):
        class Impostor:
            def __hash__(self):
                raise AssertionError("Untrusted hash invoked")

            def __eq__(self, other):
                raise AssertionError("Untrusted equality invoked")

        with self.assertRaises(InvalidReferenceError):
            self.service().consume(Impostor())

    def test_consumption_replay_is_blocked_and_audited_with_original_id(self):
        service = self.service()
        request = self.request()
        _, reference = service.issue(request)
        action = service.consume(reference)
        self.assertEqual(action.action_id, request.action_id)
        self.assertIs(service._records[reference].authorization.state, AuthorizationState.CONSUMED)
        with self.assertRaises(InvalidReferenceError):
            service.consume(reference)
        event = self.audit.events[-1]
        self.assertIs(event.event_type, AuditEventType.EXECUTION_BLOCKED)
        self.assertEqual(event.action_id, request.action_id)
        self.assertEqual(event.details["reason_code"], "AUTHORIZATION_CONSUMED")

    def test_duplicate_ids_never_reissue_before_or_after_consumption(self):
        service = self.service()
        request = self.request()
        _, reference = service.issue(request)
        changed = replace(self.request(target="WO-9001"), action_id=request.action_id)
        for repeated in (request, changed):
            with self.assertRaises(DuplicateActionError):
                service.issue(repeated)
        self.assertEqual(service.consume(reference).target, "WO-1001")
        with self.assertRaises(DuplicateActionError):
            service.issue(request)
        self.assertEqual(sum(e.event_type is AuditEventType.AUTHORIZATION_ISSUED
                             for e in self.audit.events), 1)

    def test_audit_failure_or_interruption_at_each_issuance_gate_is_terminal(self):
        for gate in (AuditEventType.ACTION_SUBMITTED, AuditEventType.GOVERNANCE_DECISION,
                     AuditEventType.AUTHORIZATION_ISSUED):
            for error in (OSError, KeyboardInterrupt, SystemExit):
                with self.subTest(gate=gate, error=error):
                    audit = RecordingAudit(gate, error)
                    policy = Mock(wraps=TOOLS["get_work_order"].evaluate_policy)
                    service = self.service(audit_write=audit, tools={"get_work_order": replace(
                        TOOLS["get_work_order"], evaluate_policy=policy,
                    )})
                    request = self.request()
                    expected = AuditWriteError if error is OSError else error
                    with self.assertRaises(expected):
                        service.issue(request)
                    self.assertEqual(service._records, {})
                    self.assertIn(request.action_id, service._seen)
                    self.assertEqual(policy.call_count, 0 if gate is AuditEventType.ACTION_SUBMITTED else 1)
                    audit.fail_at = None
                    with self.assertRaises(DuplicateActionError):
                        service.issue(request)
                    self.assertIsNotNone(service.issue(self.request())[1])  # Guard released.

    def test_failure_after_issuance_audit_before_or_during_publication_leaves_no_authority(self):
        for partial in (False, True):
            for error in (RuntimeError, KeyboardInterrupt, SystemExit):
                with self.subTest(partial=partial, error=error):
                    audit = RecordingAudit()
                    service = self.service(audit_write=audit)
                    request = self.request()
                    captured = []
                    publish = service._publish

                    def fail_publication(reference, authorization):
                        self.assertIs(audit.events[-1].event_type, AuditEventType.AUTHORIZATION_ISSUED)
                        captured.append(reference)  # White-box fault probe, never production output.
                        if partial:
                            publish(reference, authorization)
                        raise error("failure before issue() returns")

                    with patch.object(service, "_publish", side_effect=fail_publication):
                        with self.assertRaises(error):
                            service.issue(request)
                    self.assertEqual(service._records, {})
                    with self.assertRaises(InvalidReferenceError):
                        service.consume(captured[0])
                    with self.assertRaises(DuplicateActionError):
                        service.issue(request)

    def test_governance_callback_failure_and_interruption_are_terminal(self):
        for field in ("resolve_target", "validate_arguments", "evaluate_policy"):
            for error in (ValueError, KeyboardInterrupt, SystemExit):
                with self.subTest(callback=field, error=error):
                    callback = Mock(side_effect=error("injected callback failure"))
                    service = self.service(tools={"get_work_order": replace(
                        TOOLS["get_work_order"], **{field: callback},
                    )})
                    request = self.request()
                    if error is ValueError:
                        result, reference = service.issue(request)
                        self.assertIs(result.decision, Decision.DENY)
                        self.assertIsNone(reference)
                        expected_target = None if field == "resolve_target" else "WO-1001"
                        self.assertEqual(self.audit.events[-1].details["resolved_target"], expected_target)
                        self.assertFalse(self.audit.events[-1].details["action_validated"])
                    else:
                        with self.assertRaises(error):
                            service.issue(request)
                    self.assertIn(request.action_id, service._seen)
                    self.assertFalse(service._busy)
                    with self.assertRaises(DuplicateActionError):
                        service.issue(request)
                    callback.assert_called_once()
                    self.assertEqual(service._records, {})

    def test_reentrant_non_issued_decision_audit_aborts_without_authority(self):
        for target in ("WO-1001", "WO-9001"):
            with self.subTest(target=target):
                audit = RecordingAudit()
                active = True

                def writer(event):
                    audit(event)
                    if active and event.event_type is AuditEventType.GOVERNANCE_DECISION:
                        with self.assertRaises(ReentrantCallError):
                            service.issue(self.request())

                service = self.service(audit_write=writer)
                request = self.request("update_work_order_status", target)
                with self.assertRaises(ReentrantCallError):
                    service.issue(request)
                self.assertEqual(len(audit.events), 2)
                self.assertEqual(service._records, {})
                self.assertEqual(service._seen, {request.action_id})
                self.assertFalse(service._busy)
                active = False
                with self.assertRaises(DuplicateActionError):
                    service.issue(request)

    def test_reentrant_consume_after_partial_publication_cannot_leak_authority(self):
        service = self.service()
        request = self.request()
        publish = service._publish
        captured = []

        def reenter_after_insertion(reference, authorization):
            publish(reference, authorization)
            captured.append(reference)
            with self.assertRaises(ReentrantCallError):
                service.consume(reference)

        with patch.object(service, "_publish", side_effect=reenter_after_insertion):
            with self.assertRaises(ReentrantCallError):
                service.issue(request)
        self.assertEqual(service._records, {})
        self.assertFalse(service._busy)
        with self.assertRaises(InvalidReferenceError):
            service.consume(captured[0])
        with self.assertRaises(DuplicateActionError):
            service.issue(request)

    def test_interruption_after_consumption_burns_reference(self):
        service = self.service()
        _, reference = service.issue(self.request())
        with patch("controlled_agent.authorization.replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                service.consume(reference)
        with self.assertRaises(InvalidReferenceError):
            service.consume(reference)

    def test_missing_or_unsuccessful_audit_writer_cannot_issue(self):
        with self.assertRaises(TypeError):
            AuthorizationService(tools=TOOLS, permissions=PERMISSIONS, resources=WORK_ORDERS)
        with self.assertRaises(TypeError):
            self.service(audit_write=None)
        for returned in (False, True, "success"):
            with self.subTest(returned=returned):
                service = self.service(audit_write=lambda event: returned)
                request = self.request()
                with self.assertRaises(AuditWriteError):
                    service.issue(request)
                self.assertEqual(service._records, {})
                self.assertIn(request.action_id, service._seen)

    def test_blocked_reference_audit_failure_returns_no_action_and_preserves_consumption(self):
        service = self.service()
        request = self.request()
        _, reference = service.issue(request)
        service.consume(reference)
        self.audit.fail_at = AuditEventType.EXECUTION_BLOCKED
        for supplied in (reference, object()):
            with self.assertRaises(AuditWriteError):
                service.consume(supplied)
        unknown_event = self.audit.events[-1]
        self.assertNotEqual(unknown_event.action_id, request.action_id)
        self.assertIsNone(unknown_event.caller)
        self.audit.fail_at = None
        with self.assertRaises(InvalidReferenceError):
            service.consume(reference)

    def test_reentrant_issue_from_each_audit_gate_aborts_even_if_callback_catches_it(self):
        for gate in (AuditEventType.ACTION_SUBMITTED, AuditEventType.GOVERNANCE_DECISION,
                     AuditEventType.AUTHORIZATION_ISSUED):
            with self.subTest(gate=gate):
                request = self.request()

                def writer(event):
                    self.audit(event)
                    if event.event_type is gate:
                        self.assertIn(request.action_id, service._seen)
                        with self.assertRaises(ReentrantCallError):
                            service.issue(request)

                service = self.service(audit_write=writer)
                with self.assertRaises(ReentrantCallError):
                    service.issue(request)
                self.assertEqual(service._records, {})
                self.assertIn(request.action_id, service._seen)
                self.assertFalse(service._busy)

    def test_reentrant_issue_from_governance_callbacks_aborts_even_if_caught(self):
        rules = TOOLS["get_work_order"]
        for field in ("resolve_target", "validate_arguments", "evaluate_policy"):
            with self.subTest(callback=field):
                request = self.request()
                original = getattr(rules, field)

                def callback(*args):
                    self.assertIn(request.action_id, service._seen)
                    with self.assertRaises(ReentrantCallError):
                        service.issue(self.request())
                    return original(*args)

                service = self.service(tools={"get_work_order": replace(rules, **{field: callback})})
                with self.assertRaises(ReentrantCallError):
                    service.issue(request)
                self.assertEqual(service._records, {})
                with self.assertRaises(DuplicateActionError):
                    service.issue(request)

    def test_reentrant_consume_cannot_take_existing_authority(self):
        active = False

        def writer(event):
            self.audit(event)
            if active:
                with self.assertRaises(ReentrantCallError):
                    service.consume(reference)

        service = self.service(audit_write=writer)
        _, reference = service.issue(self.request())
        active = True
        with self.assertRaises(ReentrantCallError):
            service.issue(self.request())
        active = False
        self.assertEqual(service.consume(reference).target, "WO-1001")

    def test_reentrancy_during_blocked_consumption_does_not_recurse_audit(self):
        def writer(event):
            self.audit(event)
            with self.assertRaises(ReentrantCallError):
                service.consume(object())

        service = self.service(audit_write=writer)
        with self.assertRaises(ReentrantCallError):
            service.consume(object())
        self.assertEqual(len(self.audit.events), 1)
        self.assertFalse(service._busy)

    def test_configuration_is_snapshotted_and_cannot_change_after_construction(self):
        tools = dict(TOOLS)
        permissions = {"demo_operator": set(PERMISSIONS["demo_operator"])}
        resources = {name: dict(context) for name, context in WORK_ORDERS.items()}
        service = self.service(tools=tools, permissions=permissions, resources=resources)
        tools.clear()
        permissions["demo_operator"].clear()
        resources["WO-1001"]["protected"] = True
        resources.clear()
        result, reference = service.issue(self.request())
        self.assertIs(result.decision, Decision.ALLOW)
        self.assertEqual(service.consume(reference).target, "WO-1001")
        result, reference = service.issue(self.request("update_work_order_status"))
        self.assertIs(result.decision, Decision.REQUIRE_APPROVAL)
        self.assertIsNone(reference)

    def test_service_has_no_handler_or_human_input_path_and_does_not_change_demo_state(self):
        handler = Mock(side_effect=AssertionError("No dispatch"))
        service = self.service()
        before = {name: dict(context) for name, context in WORK_ORDERS.items()}
        with self.assertRaises(TypeError):
            self.service(handlers={"get_work_order": handler})
        with patch("builtins.input", side_effect=AssertionError("No human approval")) as human_input:
            for tool in TOOLS:
                for target in WORK_ORDERS:
                    _, reference = service.issue(self.request(tool, target))
                    if reference is not None:
                        service.consume(reference)
            human_input.assert_not_called()
        handler.assert_not_called()
        self.assertEqual({name: dict(context) for name, context in WORK_ORDERS.items()}, before)
        self.assertFalse(any(e.event_type in (AuditEventType.EXECUTION_STARTED,
                                             AuditEventType.APPROVAL_REQUESTED)
                             for e in self.audit.events))

    def test_references_never_appear_in_reportable_results_or_audit_payloads(self):
        service = self.service()
        result, reference = service.issue(self.request())
        record = service._records[reference].authorization
        self.assertFalse(hasattr(result, "reference"))
        for event in self.audit.events:
            self.assertNotIn("reference", event.details)
            self.assertNotIn(repr(reference), str(event.details))
        # The auditable UUID is explicitly not a usable reference.
        with self.assertRaises(InvalidReferenceError):
            service.consume(record.authorization_id)
        self.assertEqual(service.consume(reference).target, "WO-1001")

    def test_new_registry_does_not_restore_rights_from_existing_records_or_logs(self):
        original = self.service()
        _, reference = original.issue(self.request())
        record = original._records[reference].authorization
        restarted = self.service()
        for supplied in (reference, record, record.authorization_id, self.audit.events[-1]):
            with self.assertRaises(InvalidReferenceError):
                restarted.consume(supplied)


if __name__ == "__main__":
    unittest.main()
