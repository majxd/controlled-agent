"""Deterministic evaluation of host-assembled requests; no execution facilities.

The host supplies fixed tools, permissions, resource metadata, and pure policy
functions. These are trusted application configuration, never proposal fields.
ToolRules is internal configuration, not agent-facing ToolDefinition metadata.
"""

from collections.abc import Callable, Mapping, Set
from dataclasses import dataclass, replace

from .contracts import ActionRequest, Decision, GovernanceResult, JSONValue, Risk, ToolDefinition


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolRules:
    """Trusted, pure validation/resolution/policy functions, never tool handlers.

    Resolution and policy must tolerate incomplete arguments: safely identifying
    a target allows a hard denial even when other argument checks would fail.
    A policy result proposes an outcome; only the core applies permission and
    validation gates. None means the policy could not resolve an outcome.
    """

    definition: ToolDefinition
    resolve_target: Callable[[Mapping[str, JSONValue]], str | None]
    validate_arguments: Callable[[Mapping[str, JSONValue]], bool]
    evaluate_policy: Callable[[ActionRequest, Mapping[str, JSONValue]], GovernanceResult | None]


def evaluate_action(
    request: ActionRequest,
    *,
    tools: Mapping[str, ToolRules],
    permissions: Mapping[str, Set[str]],
    resources: Mapping[str, Mapping[str, JSONValue]],
) -> GovernanceResult:
    """Return a decision without mutating the request or invoking a tool.

    The caller and action ID must already come from the host. Non-record input
    raises TypeError: raw proposal parsing and submission IDs are outside this
    API. Semantically malformed records return DENY. Configuration callbacks
    must be pure; Python module boundaries cannot sandbox malicious callbacks.
    """
    if not isinstance(request, ActionRequest):
        raise TypeError("request must be a host-assembled ActionRequest")

    risk: Risk | None = None

    def deny(code: str, explanation: str) -> GovernanceResult:
        return GovernanceResult(
            action_id=request.action_id, decision=Decision.DENY, risk=risk,
            reason_code=code, explanation=explanation,
        )

    try:
        rules = tools.get(request.tool_name)
        if rules is None:
            return deny("UNKNOWN_TOOL", "The tool is not registered.")
        if not isinstance(rules, ToolRules) or rules.definition.name != request.tool_name:
            return deny("GOVERNANCE_ERROR", "The tool configuration is invalid.")

        target = rules.resolve_target(request.arguments)
        if type(target) is not str or not target.strip() or target not in resources:
            return deny("INVALID_TARGET", "The arguments do not identify a known target.")
        context = resources[target]
        if not isinstance(context, Mapping):
            return deny("GOVERNANCE_ERROR", "The target context is invalid.")

        # A separate snapshot carries the derived target; the original is unchanged.
        resolved = replace(request, target=target)
        policy = rules.evaluate_policy(resolved, context)
        if not isinstance(policy, GovernanceResult) or policy.action_id != request.action_id:
            return deny("POLICY_UNRESOLVED", "Policy did not produce a correlated outcome.")
        risk = policy.risk
        if policy.decision is Decision.DENY:
            return policy

        if request.target is not None and request.target != target:
            return deny("TARGET_MISMATCH", "The supplied target differs from the resolved target.")
        if rules.validate_arguments(request.arguments) is not True:
            return deny("INVALID_ARGUMENTS", "The arguments do not match the tool's requirements.")
        allowed_tools = permissions.get(request.caller.caller_id, frozenset())
        if not isinstance(allowed_tools, Set) or request.tool_name not in allowed_tools:
            return deny("PERMISSION_DENIED", "The caller has no permission for this tool.")
        if risk is None:
            return deny("POLICY_UNRESOLVED", "Policy did not classify the action's risk.")
        return policy
    except Exception:
        # Configuration failures never become approval. Do not expose exception
        # messages, which can contain proposal data or internal configuration.
        return deny("GOVERNANCE_ERROR", "Governance could not complete evaluation.")
