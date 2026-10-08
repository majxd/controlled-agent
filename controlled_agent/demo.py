"""Fake work-order metadata and pure governance rules; no tool handlers."""

from collections.abc import Mapping, Set
from types import MappingProxyType

from .contracts import ActionRequest, Decision, GovernanceResult, JSONValue, Risk, ToolDefinition
from .governance import ToolRules, evaluate_action


STATUSES = ("open", "in_progress", "closed")
WORK_ORDERS = MappingProxyType({
    "WO-1001": MappingProxyType({
        "status": "open", "editable": True, "protected": False, "critical": False,
    }),
    "WO-9001": MappingProxyType({
        "status": "open", "editable": False, "protected": True, "critical": True,
    }),
})
PERMISSIONS = MappingProxyType({
    "demo_operator": frozenset({"get_work_order", "update_work_order_status"}),
})


def resolve_work_order(arguments: Mapping[str, JSONValue]) -> str | None:
    target = arguments.get("work_order_id")
    return target if type(target) is str and target in WORK_ORDERS else None


def valid_read_arguments(arguments: Mapping[str, JSONValue]) -> bool:
    return set(arguments) == {"work_order_id"} and resolve_work_order(arguments) is not None


def valid_update_arguments(arguments: Mapping[str, JSONValue]) -> bool:
    return (
        set(arguments) == {"work_order_id", "new_status"}
        and resolve_work_order(arguments) is not None
        and type(arguments["new_status"]) is str
        and arguments["new_status"] in STATUSES
    )


def read_policy(request: ActionRequest, context: Mapping[str, JSONValue]) -> GovernanceResult:
    return GovernanceResult(
        action_id=request.action_id, decision=Decision.ALLOW, risk=Risk.LOW,
        reason_code="READ_ALLOWED", explanation="Reading this sample work order is permitted by policy.",
    )


def update_policy(
    request: ActionRequest, context: Mapping[str, JSONValue],
) -> GovernanceResult | None:
    if context.get("protected") is True or context.get("critical") is True:
        return GovernanceResult(
            action_id=request.action_id, decision=Decision.DENY, risk=Risk.HIGH,
            reason_code="PROTECTED_TARGET",
            explanation="Mutations of protected or critical work orders are forbidden.",
        )
    if any(type(context.get(key)) is not bool for key in ("protected", "critical", "editable")):
        return None
    if not context["editable"]:
        return GovernanceResult(
            action_id=request.action_id, decision=Decision.DENY,
            reason_code="TARGET_NOT_EDITABLE", explanation="The work order is not editable.",
        )
    return GovernanceResult(
        action_id=request.action_id, decision=Decision.REQUIRE_APPROVAL, risk=Risk.MEDIUM,
        reason_code="UPDATE_REQUIRES_APPROVAL",
        explanation="Updating this editable work order requires explicit human approval.",
    )


TOOLS = MappingProxyType({
    "get_work_order": ToolRules(
        definition=ToolDefinition(
            name="get_work_order", description="Read one fake local work order.",
            argument_schema={
                "type": "object", "properties": {"work_order_id": {"type": "string"}},
                "required": ["work_order_id"], "additionalProperties": False,
            },
        ),
        resolve_target=resolve_work_order, validate_arguments=valid_read_arguments,
        evaluate_policy=read_policy,
    ),
    "update_work_order_status": ToolRules(
        definition=ToolDefinition(
            name="update_work_order_status", description="Propose a status change to a fake local work order.",
            argument_schema={
                "type": "object",
                "properties": {
                    "work_order_id": {"type": "string"},
                    "new_status": {"type": "string", "enum": STATUSES},
                },
                "required": ["work_order_id", "new_status"], "additionalProperties": False,
            },
        ),
        resolve_target=resolve_work_order, validate_arguments=valid_update_arguments,
        evaluate_policy=update_policy,
    ),
})


def evaluate_demo(
    request: ActionRequest, *, permissions: Mapping[str, Set[str]] = PERMISSIONS,
) -> GovernanceResult:
    """Evaluate with fixed fake metadata and host-supplied permission configuration."""
    return evaluate_action(request, tools=TOOLS, permissions=permissions, resources=WORK_ORDERS)
