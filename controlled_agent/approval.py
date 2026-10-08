"""Trusted review data and injected human I/O; no approval authority or CLI.

The service owns ordering and response interpretation. Presenters must faithfully
display the canonical identity/change and readers must collect a fresh human
response, never a proposal, cached answer, or default approval.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .contracts import ActionRequest, JSONValue, _freeze_mapping, _require_text


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalChange:
    """Domain formatter output for human display only, never audit payloads."""

    current_state: Mapping[str, JSONValue]
    proposed_state: Mapping[str, JSONValue]
    intended_effect: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "current_state", _freeze_mapping(self.current_state, "current_state"))
        object.__setattr__(self, "proposed_state", _freeze_mapping(self.proposed_state, "proposed_state"))
        _require_text(self.intended_effect, "intended_effect")


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalReview:
    """Service-assembled identity and change, not a reusable approval record.

    action supplies action ID, caller, canonical tool, target, and exact arguments.
    The formatter supplies only change details; it cannot replace this action.
    """

    action: ActionRequest
    change: ApprovalChange

    def __post_init__(self) -> None:
        if not isinstance(self.action, ActionRequest) or self.action.target is None:
            raise TypeError("review requires a resolved ActionRequest")
        if type(self.change) is not ApprovalChange:
            raise TypeError("review requires ApprovalChange data")


@dataclass(frozen=True, slots=True, kw_only=True)
class HumanApprovalAdapter:
    """Trusted synchronous callbacks, supplied once by the application.

    display must return None only after presenting the complete review, or raise.
    read_response returns one fresh human response, or raises. The service checks
    reentrancy between these callbacks and accepts only plain-string 'approve'.
    No concrete terminal I/O, authentication, or display verification is provided.
    """

    display: Callable[[ApprovalReview], None]
    read_response: Callable[[], str | None]

    def __post_init__(self) -> None:
        if not callable(self.display) or not callable(self.read_response):
            raise TypeError("human approval requires display and input callbacks")
