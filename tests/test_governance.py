"""Evaluation boundaries only: no executor, approval flow, or tool handlers."""

from dataclasses import replace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from controlled_agent import ActionRequest, CallerContext, Decision, GovernanceResult, Risk, ToolDefinition
from controlled_agent.demo import PERMISSIONS, TOOLS, WORK_ORDERS, evaluate_demo
from controlled_agent.governance import ToolRules, _evaluate_action, evaluate_action


class GovernanceTests(unittest.TestCase):
    def request(self, tool="get_work_order", target="WO-1001", caller="demo_operator", **changes):
        arguments = {"work_order_id": target}
        if tool == "update_work_order_status":
            arguments["new_status"] = "closed"
        return ActionRequest(**({
            "action_id": uuid4(), "tool_name": tool, "arguments": arguments,
            "caller": CallerContext(caller_id=caller), "target": target,
        } | changes))

    def check(self, request, decision, risk, code, **config):
        result = evaluate_demo(request, **config)
        self.assertEqual(result.action_id, request.action_id)
        self.assertIs(result.decision, decision)
        self.assertIs(result.risk, risk)
        self.assertEqual(result.reason_code, code)
        return result

    def test_read_allowed_on_both_targets(self):
        for target in WORK_ORDERS:
            with self.subTest(target=target):
                self.check(self.request(target=target), Decision.ALLOW, Risk.LOW, "READ_ALLOWED")

    def test_internal_evaluation_retains_only_fully_validated_actions_and_public_api_is_unchanged(self):
        for request in (
            self.request(), self.request("update_work_order_status"),
            self.request("update_work_order_status", "WO-9001"),
            self.request(arguments={"work_order_id": "WO-1001", "unexpected": True}),
            self.request(caller="unknown"), self.request("unknown"),
        ):
            with self.subTest(tool=request.tool_name, arguments=request.arguments):
                config = {"tools": TOOLS, "permissions": PERMISSIONS, "resources": WORK_ORDERS}
                result, action, resolved_target = _evaluate_action(request, **config)
                self.assertEqual(evaluate_action(request, **config), result)
                expected_target = request.arguments["work_order_id"] if request.tool_name in TOOLS else None
                self.assertEqual(resolved_target, expected_target)
                if result.decision is Decision.DENY:
                    self.assertIsNone(action)
                else:
                    self.assertEqual(action.target, request.arguments["work_order_id"])
                    self.assertEqual(action.action_id, request.action_id)

    def test_same_update_tool_changes_decision_with_context_for_every_status(self):
        for status in ("open", "in_progress", "closed"):
            for target, decision, risk, code in (
                ("WO-1001", Decision.REQUIRE_APPROVAL, Risk.MEDIUM, "UPDATE_REQUIRES_APPROVAL"),
                ("WO-9001", Decision.DENY, Risk.HIGH, "PROTECTED_TARGET"),
            ):
                with self.subTest(status=status, target=target):
                    request = self.request("update_work_order_status", target, arguments={
                        "work_order_id": target, "new_status": status,
                    })
                    self.check(request, decision, risk, code)

    def test_unknown_tool_is_denied(self):
        self.check(self.request("delete_work_order"), Decision.DENY, None, "UNKNOWN_TOOL")

    def test_missing_permissions_deny_even_low_risk_reads(self):
        for tool, risk in (("get_work_order", Risk.LOW), ("update_work_order_status", Risk.MEDIUM)):
            for permissions in ({}, {"demo_operator": frozenset()}, {"someone_else": frozenset({tool})}):
                with self.subTest(tool=tool, permissions=permissions):
                    self.check(self.request(tool), Decision.DENY, risk, "PERMISSION_DENIED",
                               permissions=permissions)
        self.check(self.request(caller="unknown_caller"), Decision.DENY, Risk.LOW, "PERMISSION_DENIED")

    def test_no_risk_or_favorable_policy_outcome_can_bypass_permission(self):
        request = self.request()
        for risk in Risk:
            for decision in (Decision.ALLOW, Decision.REQUIRE_APPROVAL):
                with self.subTest(risk=risk, decision=decision):
                    policy = Mock(return_value=GovernanceResult(
                        action_id=request.action_id, decision=decision, risk=risk,
                        reason_code="POLICY_PERMITS", explanation="Policy permits this action.",
                    ))
                    rules = replace(TOOLS[request.tool_name], evaluate_policy=policy)
                    result = evaluate_action(request, tools={request.tool_name: rules},
                                             permissions={}, resources=WORK_ORDERS)
                    self.assertIs(result.decision, Decision.DENY)
                    self.assertIs(result.risk, risk)
                    self.assertEqual(result.reason_code, "PERMISSION_DENIED")
                    policy.assert_called_once()

    def test_invalid_targets_do_not_fall_back_to_top_level_target(self):
        for value in ("WO-UNKNOWN", "", " WO-1001", None, 1001, ["WO-1001"], {"id": "WO-1001"}):
            with self.subTest(value=value):
                self.check(self.request(arguments={"work_order_id": value}),
                           Decision.DENY, None, "INVALID_TARGET")
        self.check(self.request(arguments={}), Decision.DENY, None, "INVALID_TARGET")

    def test_invalid_arguments_deny(self):
        for status in (None, True, 1, "deleted", "CLOSED", " closed", ["closed"], {"status": "closed"}):
            with self.subTest(status=status):
                self.check(self.request("update_work_order_status", arguments={
                    "work_order_id": "WO-1001", "new_status": status,
                }), Decision.DENY, Risk.MEDIUM, "INVALID_ARGUMENTS")
        self.check(self.request("update_work_order_status", arguments={"work_order_id": "WO-1001"}),
                   Decision.DENY, Risk.MEDIUM, "INVALID_ARGUMENTS")
        self.check(self.request(arguments={"work_order_id": "WO-1001", "new_status": "closed"}),
                   Decision.DENY, Risk.LOW, "INVALID_ARGUMENTS")

    def test_protected_denial_precedes_invalid_arguments_permission_and_target_mismatch(self):
        for arguments in (
            {"work_order_id": "WO-9001"},
            {"work_order_id": "WO-9001", "new_status": "invalid"},
            {"work_order_id": "WO-9001", "new_status": "closed", "approved": True},
        ):
            with self.subTest(arguments=arguments):
                self.check(self.request("update_work_order_status", arguments=arguments),
                           Decision.DENY, Risk.HIGH, "PROTECTED_TARGET", permissions={})

    def test_protected_or_critical_alone_is_a_hard_deny(self):
        for protected, critical in ((True, False), (False, True)):
            with self.subTest(protected=protected, critical=critical):
                result = evaluate_action(
                    self.request("update_work_order_status"), tools=TOOLS, permissions=PERMISSIONS,
                    resources={"WO-1001": {"editable": True, "protected": protected, "critical": critical}},
                )
                self.assertIs(result.decision, Decision.DENY)
                self.assertIs(result.risk, Risk.HIGH)
                self.assertEqual(result.reason_code, "PROTECTED_TARGET")

    def test_spoofed_claims_cannot_supply_authority_or_change_risk(self):
        claims = {
            "approved": True, "approval": {"human": True}, "risk": "LOW", "decision": "ALLOW",
            "caller": {"caller_id": "demo_operator"}, "permissions": ["*"],
            "protected": False, "critical": False, "target": "WO-1001",
            "evaluate_policy": "lambda request, context: 'ALLOW'",
            "resolve_target": "controlled_agent.demo.resolve_work_order",
        }
        for field, value in claims.items():
            for target, risk, code in (
                ("WO-1001", Risk.MEDIUM, "INVALID_ARGUMENTS"),
                ("WO-9001", Risk.HIGH, "PROTECTED_TARGET"),
            ):
                with self.subTest(field=field, target=target):
                    self.check(self.request("update_work_order_status", target, caller="unprivileged",
                                            arguments={"work_order_id": target, "new_status": "closed",
                                                       field: value}), Decision.DENY, risk, code)

    def test_spoofed_claims_cannot_create_or_weaken_resolved_policy_context(self):
        tool = "update_work_order_status"
        for argument_target, claimed_target, expected_risk, expected_code in (
            ("WO-UNKNOWN", "WO-9001", None, "INVALID_TARGET"),
            (None, "WO-9001", None, "INVALID_TARGET"),
            ("WO-1001", "WO-9001", Risk.MEDIUM, "TARGET_MISMATCH"),
            ("WO-9001", "WO-1001", Risk.HIGH, "PROTECTED_TARGET"),
        ):
            with self.subTest(argument_target=argument_target, claimed_target=claimed_target):
                claimed_protected = claimed_target == "WO-9001"
                request = self.request(tool, claimed_target, arguments={
                    "work_order_id": argument_target, "new_status": "closed",
                    "target": claimed_target, "risk": "HIGH" if claimed_protected else "LOW",
                    "approved": True, "decision": "ALLOW",
                    "protected": claimed_protected, "critical": claimed_protected,
                    "resource_context": {"id": claimed_target, "protected": claimed_protected},
                })
                policy = Mock(wraps=TOOLS[tool].evaluate_policy)
                result = evaluate_action(
                    request, tools={tool: replace(TOOLS[tool], evaluate_policy=policy)},
                    permissions=PERMISSIONS, resources=WORK_ORDERS,
                )
                self.assertIs(result.decision, Decision.DENY)
                self.assertIs(result.risk, expected_risk)
                self.assertEqual(result.reason_code, expected_code)
                if argument_target not in WORK_ORDERS:
                    policy.assert_not_called()
                else:
                    policy.assert_called_once()
                    resolved, context = policy.call_args.args
                    self.assertIsNot(resolved, request)
                    self.assertEqual(resolved.target, argument_target)
                    self.assertIs(context, WORK_ORDERS[argument_target])
                self.assertEqual(request.target, claimed_target)

    def test_protected_id_cannot_replace_missing_or_incomplete_trusted_metadata(self):
        tool = "update_work_order_status"
        request = self.request(tool, "WO-9001", arguments={
            "work_order_id": "WO-9001", "new_status": "closed",
            "protected": True, "critical": True, "risk": "HIGH", "decision": "DENY",
        })
        for resources, code in (({}, "INVALID_TARGET"), ({"WO-9001": {}}, "POLICY_UNRESOLVED")):
            with self.subTest(resources=resources):
                policy = Mock(wraps=TOOLS[tool].evaluate_policy)
                result = evaluate_action(
                    request, tools={tool: replace(TOOLS[tool], evaluate_policy=policy)},
                    permissions=PERMISSIONS, resources=resources,
                )
                self.assertIs(result.decision, Decision.DENY)
                self.assertIsNone(result.risk)
                self.assertEqual(result.reason_code, code)
                if not resources:
                    policy.assert_not_called()
                else:
                    policy.assert_called_once()
                    self.assertIs(policy.call_args.args[1], resources["WO-9001"])

    def test_target_is_derived_without_mutating_the_original(self):
        request = replace(self.request(), target=None)
        self.check(request, Decision.ALLOW, Risk.LOW, "READ_ALLOWED")
        self.assertIsNone(request.target)
        self.check(replace(request, target="WO-9001"), Decision.DENY, Risk.LOW, "TARGET_MISMATCH")

    def test_requests_configuration_and_resource_state_are_unchanged_and_results_repeat(self):
        before = {key: dict(value) for key, value in WORK_ORDERS.items()}
        for tool in TOOLS:
            for target in WORK_ORDERS:
                request = self.request(tool, target)
                arguments = dict(request.arguments)
                result = evaluate_demo(request)
                self.assertEqual(evaluate_demo(request), result)
                self.assertEqual(dict(request.arguments), arguments)
                self.assertEqual(request.target, target)
        self.assertEqual({key: dict(value) for key, value in WORK_ORDERS.items()}, before)
        with self.assertRaises(TypeError):
            WORK_ORDERS["WO-1001"]["protected"] = True
        with self.assertRaises(TypeError):
            PERMISSIONS["demo_operator"] = frozenset()

    def test_governance_exposes_no_handler_dispatch_or_authorization_issuance(self):
        handler = Mock(side_effect=AssertionError("A tool must not execute"))
        rules = TOOLS["get_work_order"]
        with self.assertRaises(TypeError):
            replace(rules, handler=handler)
        with self.assertRaises(TypeError):
            evaluate_action(self.request(), tools=TOOLS, permissions=PERMISSIONS,
                            resources=WORK_ORDERS, handlers={"get_work_order": handler})
        with self.assertRaises(TypeError):
            self.request(arguments={"work_order_id": "WO-1001", "handler": handler})
        with patch("controlled_agent.contracts.Authorization", side_effect=AssertionError("No issuance")) as auth:
            for tool in TOOLS:
                for target in WORK_ORDERS:
                    evaluate_demo(self.request(tool, target))
            auth.assert_not_called()
        handler.assert_not_called()
        self.assertEqual(WORK_ORDERS["WO-1001"]["status"], "open")

    def test_demo_evaluation_does_not_open_files_or_request_human_input(self):
        requests = (
            (self.request(), Decision.ALLOW),
            (self.request("update_work_order_status"), Decision.REQUIRE_APPROVAL),
            (self.request("update_work_order_status", "WO-9001"), Decision.DENY),
            (self.request("unknown_tool"), Decision.DENY),
        )
        with (
            patch("builtins.open", side_effect=AssertionError("No file I/O")) as builtin_open,
            patch("io.open", side_effect=AssertionError("No file I/O")) as io_open,
            patch("os.open", side_effect=AssertionError("No file I/O")) as os_open,
            patch("builtins.input", side_effect=AssertionError("No approval flow")) as human_input,
        ):
            for request, decision in requests:
                self.assertIs(evaluate_demo(request).decision, decision)
            for operation in (builtin_open, io_open, os_open, human_input):
                operation.assert_not_called()

    def test_unresolved_policy_and_incomplete_context_fail_closed(self):
        request = self.request()
        for outcome in (None, "ALLOW", GovernanceResult(
            action_id=uuid4(), decision=Decision.ALLOW, risk=Risk.LOW,
            reason_code="READ_ALLOWED", explanation="Wrong action ID.",
        ), GovernanceResult(
            action_id=request.action_id, decision=Decision.ALLOW,
            reason_code="READ_ALLOWED", explanation="Missing risk.",
        )):
            with self.subTest(outcome=outcome):
                rules = replace(TOOLS[request.tool_name], evaluate_policy=lambda action, context: outcome)
                result = evaluate_action(request, tools={request.tool_name: rules},
                                         permissions=PERMISSIONS, resources=WORK_ORDERS)
                self.assertIs(result.decision, Decision.DENY)
                self.assertEqual(result.reason_code, "POLICY_UNRESOLVED")
        result = evaluate_action(self.request("update_work_order_status"), tools=TOOLS,
                                 permissions=PERMISSIONS, resources={"WO-1001": {"editable": True}})
        self.assertIs(result.decision, Decision.DENY)
        self.assertEqual(result.reason_code, "POLICY_UNRESOLVED")

    def test_configuration_errors_and_non_boolean_validation_fail_closed(self):
        for field in ("resolve_target", "validate_arguments", "evaluate_policy"):
            with self.subTest(callback=field):
                broken = Mock(side_effect=RuntimeError("sensitive internal detail"))
                rules = replace(TOOLS["get_work_order"], **{field: broken})
                result = evaluate_action(self.request(), tools={"get_work_order": rules},
                                         permissions=PERMISSIONS, resources=WORK_ORDERS)
                self.assertIs(result.decision, Decision.DENY)
                self.assertEqual(result.reason_code, "GOVERNANCE_ERROR")
                self.assertNotIn("sensitive", result.explanation)
                broken.assert_called_once()
        rules = replace(TOOLS["get_work_order"], validate_arguments=lambda arguments: "yes")
        result = evaluate_action(self.request(), tools={"get_work_order": rules},
                                 permissions=PERMISSIONS, resources=WORK_ORDERS)
        self.assertIs(result.decision, Decision.DENY)
        self.assertEqual(result.reason_code, "INVALID_ARGUMENTS")
        self.check(self.request(), Decision.DENY, Risk.LOW, "PERMISSION_DENIED",
                   permissions={"demo_operator": "get_work_order"})

    def test_raw_proposals_are_not_deserialized_into_trusted_requests(self):
        with self.assertRaises(TypeError):
            evaluate_demo({"caller": "demo_operator", "tool_name": "get_work_order", "approved": True})

    def test_core_supports_an_unrelated_domain_and_risk_does_not_choose_decision(self):
        request = ActionRequest(action_id=uuid4(), caller=CallerContext(caller_id="reader"),
                                tool_name="documents.read", arguments={"document_id": "DOC-1"})

        def policy(action, context):
            self.assertEqual(action.target, "DOC-1")
            return GovernanceResult(action_id=action.action_id, decision=Decision.REQUIRE_APPROVAL,
                                    risk=Risk.LOW, reason_code="REVIEW_REQUIRED",
                                    explanation="This document requires review.")

        rules = ToolRules(
            definition=ToolDefinition(name="documents.read", description="Read a document", argument_schema={}),
            resolve_target=lambda arguments: arguments.get("document_id"),
            validate_arguments=lambda arguments: set(arguments) == {"document_id"}, evaluate_policy=policy,
        )
        config = {"tools": {"documents.read": rules}, "resources": {"DOC-1": {}}}
        result = evaluate_action(request, permissions={"reader": frozenset({"documents.read"})}, **config)
        self.assertIs(result.decision, Decision.REQUIRE_APPROVAL)
        self.assertIs(result.risk, Risk.LOW)
        denied = evaluate_action(request, permissions={}, **config)
        self.assertIs(denied.decision, Decision.DENY)
        self.assertIs(denied.risk, Risk.LOW)


if __name__ == "__main__":
    unittest.main()
