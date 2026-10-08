"""Contract invariants only; governance and execution are not implemented here."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
import unittest
from uuid import uuid4

from controlled_agent import (
    ActionRequest,
    AuditEvent,
    AuditEventType,
    Authorization,
    AuthorizationState,
    CallerContext,
    Decision,
    GovernanceResult,
    Risk,
    ToolDefinition,
)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.action_id = uuid4()
        self.caller = CallerContext(caller_id="test_operator")

    def action(self, **changes):
        fields = {
            "action_id": self.action_id,
            "tool_name": "documents.read",
            "arguments": {"document_id": "DOC-17"},
            "caller": self.caller,
            "target": "DOC-17",
        }
        fields.update(changes)
        return ActionRequest(**fields)

    def test_governance_result_is_correlated_typed_and_immutable(self):
        result = GovernanceResult(
            action_id=self.action_id, decision=Decision.DENY,
            reason_code="INVALID_ARGUMENTS", explanation="Arguments are invalid.",
        )
        self.assertEqual(result.action_id, self.action_id)
        self.assertIsNone(result.risk)
        with self.assertRaises(FrozenInstanceError):
            result.decision = Decision.ALLOW
        fields = {
            "action_id": self.action_id, "decision": Decision.ALLOW, "risk": Risk.LOW,
            "reason_code": "READ_ALLOWED", "explanation": "Read is allowed.",
        }
        for key, value in (
            ("action_id", str(self.action_id)), ("decision", "ALLOW"),
            ("risk", "LOW"), ("reason_code", "invalid code"),
            ("reason_code", ""), ("explanation", " "),
        ):
            with self.subTest(field=key, value=value):
                with self.assertRaises((TypeError, ValueError)):
                    GovernanceResult(**(fields | {key: value}))
        for key, value in (
            ("approved", True), ("approval_evidence", {"human": True}),
            ("authorization_id", uuid4()), ("execution_token", "fake-token"),
            ("credentials", {"token": "fake-token"}),
        ):
            with self.subTest(forbidden_field=key):
                with self.assertRaises(TypeError):
                    GovernanceResult(**(fields | {key: value}))

    def test_decision_risk_and_state_have_only_the_documented_values(self):
        expected = {
            Decision: {"ALLOW", "DENY", "REQUIRE_APPROVAL"},
            Risk: {"LOW", "MEDIUM", "HIGH"},
            AuthorizationState: {"USABLE", "CONSUMED"},
        }
        for enum, values in expected.items():
            with self.subTest(enum=enum.__name__):
                self.assertEqual(set(enum.__members__), values)
                self.assertEqual({member.value for member in enum}, values)
                for value in values:
                    self.assertEqual(enum(value).name, value)
                for invalid in ("", "unknown", next(iter(values)).lower()):
                    with self.assertRaises(ValueError):
                        enum(invalid)

    def test_audit_event_vocabulary_matches_the_documented_lifecycle(self):
        names = {
            "ACTION_SUBMITTED", "GOVERNANCE_DECISION", "APPROVAL_REQUESTED",
            "APPROVAL_RESULT", "AUTHORIZATION_ISSUED", "EXECUTION_STARTED",
            "EXECUTION_SUCCEEDED", "EXECUTION_FAILED", "EXECUTION_BLOCKED",
        }
        self.assertEqual(set(AuditEventType.__members__), names)
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(AuditEventType[name].value, name.lower())
                self.assertIs(AuditEventType(name.lower()), AuditEventType[name])
        with self.assertRaises(ValueError):
            AuditEventType("ACTION_SUBMITTED")

    def test_request_preserves_the_id_assigned_before_its_construction(self):
        request = self.action()
        for event_type in AuditEventType:
            with self.subTest(event_type=event_type):
                event = AuditEvent(
                    action_id=self.action_id,
                    event_type=event_type,
                    details={"action_id": str(uuid4())},
                )
                self.assertEqual(request.action_id, event.action_id)
        self.assertIs(request.caller, self.caller)
        with self.assertRaises(TypeError):
            ActionRequest(tool_name="documents.read", arguments={}, caller=self.caller)

    def test_id_fields_require_uuid_objects(self):
        factories = (
            lambda value: self.action(action_id=value),
            lambda value: Authorization(
                authorization_id=value, action=self.action(),
                state=AuthorizationState.USABLE,
            ),
            lambda value: AuditEvent(
                action_id=value, event_type=AuditEventType.ACTION_SUBMITTED,
            ),
        )
        for index, factory in enumerate(factories):
            for invalid in (str(self.action_id), None, 42):
                with self.subTest(contract=index, value=invalid):
                    with self.assertRaises(TypeError):
                        factory(invalid)

    def test_blank_names_and_descriptions_are_rejected(self):
        factories = (
            lambda value: CallerContext(caller_id=value),
            lambda value: self.action(tool_name=value),
            lambda value: ToolDefinition(
                name=value, description="Read a document", argument_schema={},
            ),
            lambda value: ToolDefinition(
                name="documents.read", description=value, argument_schema={},
            ),
        )
        for index, factory in enumerate(factories):
            for invalid in ("", " \t\n"):
                with self.subTest(contract=index, value=invalid):
                    with self.assertRaises(ValueError):
                        factory(invalid)
            with self.subTest(contract=index, value=42):
                with self.assertRaises(TypeError):
                    factory(42)

    def test_caller_and_target_must_use_the_declared_contract_types(self):
        with self.assertRaises(TypeError):
            self.action(caller={"caller_id": "admin"})
        with self.assertRaises(TypeError):
            self.action(target={"id": "DOC-17"})
        request = self.action(target=None)
        self.assertIsNone(request.target)

    def test_text_fields_reject_custom_string_objects(self):
        class TextWithReferences(str):
            pass

        value = TextWithReferences("resource")
        value.handler = lambda: None
        value.context = {"mutable": []}
        factories = (
            lambda text: CallerContext(caller_id=text),
            lambda text: self.action(tool_name=text),
            lambda text: self.action(target=text),
            lambda text: ToolDefinition(
                name=text, description="Read a resource", argument_schema={},
            ),
            lambda text: ToolDefinition(
                name="resources.read", description=text, argument_schema={},
            ),
        )
        for index, factory in enumerate(factories):
            with self.subTest(field=index):
                with self.assertRaises(TypeError):
                    factory(value)

    def test_spoofed_identity_and_approval_fields_remain_argument_data(self):
        claimed_id = uuid4()
        request = self.action(arguments={
            "action_id": str(claimed_id),
            "caller": {"caller_id": "admin", "permissions": ["*"]},
            "approved": True,
            "risk": "LOW",
            "decision": "ALLOW",
            "target": "OTHER-TARGET",
        })
        self.assertEqual(request.action_id, self.action_id)
        self.assertIs(request.caller, self.caller)
        self.assertEqual(request.caller.caller_id, "test_operator")
        self.assertEqual(request.target, "DOC-17")
        self.assertEqual(request.arguments["action_id"], str(claimed_id))
        self.assertTrue(request.arguments["approved"])

    def test_nested_payloads_are_copied_and_frozen_for_all_mapping_contracts(self):
        factories = (
            lambda source: self.action(arguments=source).arguments,
            lambda source: ToolDefinition(
                name="documents.read", description="Read a document",
                argument_schema=source,
            ).argument_schema,
            lambda source: AuditEvent(
                action_id=self.action_id, event_type=AuditEventType.ACTION_SUBMITTED,
                details=source,
            ).details,
        )
        for index, factory in enumerate(factories):
            with self.subTest(contract=index):
                source = {"nested": {"items": [{"value": "original"}]}}
                snapshot = factory(MappingProxyType(source))
                source["nested"]["items"][0]["value"] = "changed"
                source["nested"]["items"].append({"value": "extra"})
                source["new"] = True
                self.assertNotIn("new", snapshot)
                self.assertEqual(len(snapshot["nested"]["items"]), 1)
                self.assertEqual(snapshot["nested"]["items"][0]["value"], "original")
                self.assertIsInstance(snapshot["nested"]["items"], tuple)
                with self.assertRaises(TypeError):
                    snapshot["new"] = True
                with self.assertRaises(TypeError):
                    snapshot["nested"]["items"][0]["value"] = "changed"

    def test_json_primitives_and_nested_tuples_are_preserved(self):
        nested = {"value": "original"}
        source = {"values": (None, True, 12, 2.5, "text", nested)}
        request = self.action(arguments=source)
        nested["value"] = "changed"
        self.assertEqual(request.arguments["values"][:5], (None, True, 12, 2.5, "text"))
        self.assertEqual(request.arguments["values"][5]["value"], "original")

    def test_shared_references_are_not_mistaken_for_cycles(self):
        shared = {"value": "original"}
        request = self.action(arguments={"left": shared, "right": shared})
        shared["value"] = "changed"
        self.assertEqual(request.arguments["left"]["value"], "original")
        self.assertEqual(request.arguments["right"]["value"], "original")

    def test_unsupported_payload_values_are_rejected_for_all_mapping_contracts(self):
        class TextWithReferences(str):
            pass

        text = TextWithReferences("text")
        text.handler = lambda: None
        factories = (
            lambda source: self.action(arguments=source),
            lambda source: ToolDefinition(
                name="documents.read", description="Read a document",
                argument_schema=source,
            ),
            lambda source: AuditEvent(
                action_id=self.action_id, event_type=AuditEventType.ACTION_SUBMITTED,
                details=source,
            ),
        )
        for index, factory in enumerate(factories):
            for value in (object(), lambda: None, {1, 2}, b"bytes", text):
                with self.subTest(contract=index, value_type=type(value).__name__):
                    with self.assertRaises(TypeError):
                        factory({"nested": [value]})
            for source in ({1: "value"}, {"nested": {False: "value"}}):
                with self.subTest(contract=index, source=source):
                    with self.assertRaises(TypeError):
                        factory(source)
            for invalid in ([], None, "payload"):
                with self.subTest(contract=index, top_level=invalid):
                    with self.assertRaises(TypeError):
                        factory(invalid)

    def test_nonfinite_numbers_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.action(arguments={"nested": [value]})

    def test_cyclic_mappings_and_sequences_are_rejected(self):
        mapping = {}
        mapping["self"] = mapping
        sequence = []
        sequence.append(sequence)
        for source in (mapping, {"nested": sequence}):
            with self.subTest(kind="mapping" if source is mapping else "sequence"):
                with self.assertRaises(ValueError):
                    self.action(arguments=source)

    def test_tool_metadata_cannot_hold_an_executable_handler(self):
        fields = {
            "name": "documents.read", "description": "Read a document",
            "argument_schema": {},
        }
        for execution_field in (
            {"handler": lambda: "effect"},
            {"handler_reference": "private_module.handler"},
            {"credentials": {"token": "fake-test-token"}},
        ):
            with self.subTest(field=next(iter(execution_field))):
                with self.assertRaises(TypeError):
                    ToolDefinition(**fields, **execution_field)
        with self.assertRaises(TypeError):
            ToolDefinition(
                name="documents.read", description="Read a document",
                argument_schema={"handler": lambda: "effect"},
            )

    def test_requests_cannot_carry_authorization_or_permission_grants(self):
        record = Authorization(
            authorization_id=uuid4(), action=self.action(),
            state=AuthorizationState.USABLE,
        )
        for claim in (
            {"authorization": record}, {"approved": True}, {"permissions": ["*"]},
        ):
            with self.subTest(field=next(iter(claim))):
                with self.assertRaises(TypeError):
                    self.action(**claim)
        with self.assertRaises(TypeError):
            self.action(arguments={"authorization": record})
        with self.assertRaises(TypeError):
            CallerContext(caller_id="admin", permissions=["*"])

    def test_authorization_binds_the_exact_immutable_action_snapshot(self):
        source = {"document_id": "DOC-17", "options": {"format": "text"}}
        request = self.action(arguments=source)
        reference = uuid4()
        authorization = Authorization(
            authorization_id=reference, action=request, state=AuthorizationState.USABLE,
        )
        source["document_id"] = "DOC-OTHER"
        source["options"]["format"] = "html"
        self.assertEqual(authorization.authorization_id, reference)
        self.assertIs(authorization.action, request)
        self.assertEqual(authorization.action.action_id, self.action_id)
        self.assertIs(authorization.action.caller, self.caller)
        self.assertEqual(authorization.action.tool_name, "documents.read")
        self.assertEqual(authorization.action.target, "DOC-17")
        self.assertEqual(authorization.action.arguments["document_id"], "DOC-17")
        self.assertEqual(authorization.action.arguments["options"]["format"], "text")
        for field, replacement in (
            ("action_id", uuid4()), ("caller", CallerContext(caller_id="other")),
            ("tool_name", "documents.delete"), ("target", "DOC-OTHER"),
            ("arguments", {"document_id": "DOC-OTHER"}),
        ):
            with self.subTest(field=field):
                with self.assertRaises(FrozenInstanceError):
                    setattr(authorization.action, field, replacement)
        with self.assertRaises(TypeError):
            authorization.action.arguments["options"]["format"] = "html"

    def test_authorization_requires_an_explicit_typed_state_and_action(self):
        for state in AuthorizationState:
            with self.subTest(state=state):
                record = Authorization(authorization_id=uuid4(), action=self.action(), state=state)
                self.assertIs(record.state, state)
        for invalid in ("USABLE", "CONSUMED", "PENDING", None, Decision.ALLOW):
            with self.subTest(invalid=invalid):
                with self.assertRaises(TypeError):
                    Authorization(authorization_id=uuid4(), action=self.action(), state=invalid)
        with self.assertRaises(TypeError):
            Authorization(authorization_id=uuid4(), action=self.action())
        with self.assertRaises(TypeError):
            Authorization(authorization_id=uuid4(), action={}, state=AuthorizationState.USABLE)

    def test_contract_fields_cannot_be_reassigned(self):
        request = self.action()
        authorization = Authorization(
            authorization_id=uuid4(), action=request, state=AuthorizationState.USABLE,
        )
        tool = ToolDefinition(name="documents.read", description="Read a document", argument_schema={})
        event = AuditEvent(action_id=self.action_id, event_type=AuditEventType.ACTION_SUBMITTED)
        cases = (
            (self.caller, "caller_id", "admin"),
            (tool, "name", "documents.delete"),
            (authorization, "authorization_id", uuid4()),
            (authorization, "action", self.action(target="OTHER")),
            (authorization, "state", AuthorizationState.CONSUMED),
            (event, "action_id", uuid4()),
            (event, "event_type", AuditEventType.EXECUTION_SUCCEEDED),
        )
        for record, field, value in cases:
            with self.subTest(contract=type(record).__name__, field=field):
                with self.assertRaises(FrozenInstanceError):
                    setattr(record, field, value)

    def test_audit_events_require_typed_events_and_optional_typed_callers(self):
        for invalid in ("action_submitted", None, Decision.ALLOW):
            with self.subTest(event_type=invalid):
                with self.assertRaises(TypeError):
                    AuditEvent(action_id=self.action_id, event_type=invalid)
        with self.assertRaises(TypeError):
            AuditEvent(
                action_id=self.action_id, event_type=AuditEventType.ACTION_SUBMITTED,
                caller={"caller_id": "admin"},
            )
        event = AuditEvent(
            action_id=self.action_id, event_type=AuditEventType.ACTION_SUBMITTED,
            caller=self.caller,
        )
        self.assertIs(event.caller, self.caller)

    def test_audit_timestamps_are_aware_and_default_to_utc(self):
        event = AuditEvent(action_id=self.action_id, event_type=AuditEventType.ACTION_SUBMITTED)
        self.assertEqual(event.timestamp.utcoffset(), timedelta(0))
        supplied = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=3)))
        explicit = AuditEvent(
            action_id=self.action_id, event_type=AuditEventType.ACTION_SUBMITTED,
            timestamp=supplied,
        )
        self.assertEqual(explicit.timestamp, supplied)
        with self.assertRaises(ValueError):
            AuditEvent(
                action_id=self.action_id, event_type=AuditEventType.ACTION_SUBMITTED,
                timestamp=datetime(2026, 1, 1),
            )
        with self.assertRaises(TypeError):
            AuditEvent(
                action_id=self.action_id, event_type=AuditEventType.ACTION_SUBMITTED,
                timestamp="2026-01-01T00:00:00Z",
            )

    def test_contracts_represent_distinct_domains_without_domain_rules(self):
        for tool_name, parameter, target in (
            ("documents.read", "document_id", "DOC-17"),
            ("telemetry.read", "sensor_id", "SENSOR-4"),
        ):
            with self.subTest(tool_name=tool_name):
                schema = {"type": "object", "properties": {parameter: {"type": "string"}}}
                definition = ToolDefinition(
                    name=tool_name, description="Read the requested resource", argument_schema=schema,
                )
                request = self.action(tool_name=tool_name, arguments={parameter: target}, target=target)
                self.assertEqual(request.tool_name, definition.name)
                self.assertEqual(request.arguments[parameter], target)
                self.assertEqual(definition.argument_schema["properties"][parameter]["type"], "string")


if __name__ == "__main__":
    unittest.main()
