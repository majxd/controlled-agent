"""Bounded fake in-memory work orders; import only on the trusted execution side.

This is not an authorization API. Direct Python access is outside the interface
boundary; the application must invoke these handlers only through its executor.
"""

from types import MappingProxyType

from .authorization import AuthorizationService
from .contracts import ActionRequest, _freeze_mapping
from .demo import STATUSES, WORK_ORDERS
from .executor import RejectionReason, ToolRejected


class FakeWorkOrderTools:
    """One fixed-baseline fixture per run; no reset, external I/O, or live metadata.

    Bind to the same service used by the executor. Its captured resource snapshot
    supplies the reviewed baseline; the fixed fixtures supply live initial state
    and independent protection bounds. Only status can change.
    """

    def __init__(self, *, authorization: AuthorizationService):
        if type(authorization) is not AuthorizationService:
            raise TypeError("The fixture requires its executor's authorization service")
        self._baseline = authorization._resources
        self._statuses = {target: context["status"] for target, context in WORK_ORDERS.items()}
        self.handlers = MappingProxyType({
            "get_work_order": self._get_work_order,
            "update_work_order_status": self._update_work_order_status,
        })

    def _target(self, action: ActionRequest, tool: str, fields: set[str]) -> str:
        if (type(action) is not ActionRequest or action.tool_name != tool
                or type(action.target) is not str or action.target not in WORK_ORDERS
                or set(action.arguments) != fields
                or type(action.arguments.get("work_order_id")) is not str
                or action.arguments["work_order_id"] != action.target):
            raise ToolRejected(RejectionReason.INVALID_ACTION)
        return action.target

    def _snapshot(self, target):
        return _freeze_mapping({"work_order_id": target, **WORK_ORDERS[target],
                                "status": self._statuses[target]}, "work_order")

    def _get_work_order(self, action: ActionRequest):
        target = self._target(action, "get_work_order", {"work_order_id"})
        return self._snapshot(target)

    def _update_work_order_status(self, action: ActionRequest):
        target = self._target(action, "update_work_order_status", {"work_order_id", "new_status"})
        metadata = WORK_ORDERS[target]
        if metadata["protected"] or metadata["critical"] or not metadata["editable"]:
            raise ToolRejected(RejectionReason.PROTECTED_TARGET)
        status = action.arguments["new_status"]
        if type(status) is not str or status not in STATUSES:
            raise ToolRejected(RejectionReason.INVALID_ACTION)
        # Last check before the sole mutation, using the same fixed reviewed
        # baseline as governance/approval. No refresh or secondary resolution.
        baseline = self._baseline.get(target)
        if (baseline is None or type(baseline.get("status")) is not str
                or baseline["status"] not in STATUSES):
            raise ToolRejected(RejectionReason.INVALID_ACTION)
        if self._statuses[target] != baseline["status"]:
            raise ToolRejected(RejectionReason.STALE_BASELINE)
        self._statuses[target] = status
        return self._snapshot(target)
