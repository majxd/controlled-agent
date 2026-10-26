"""Pure proposal scoring; observation provenance is not established here.

No provider runner, execution components, I/O, or persistence. Callers supply
observations; expected answers are never part of the public ProviderRequest.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .demo import TOOLS
from .provider import ProviderFailure, ProviderRequest
from .submission import MalformedProposal, parse_proposal


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationCase:
    case_id: str
    request: ProviderRequest
    expected: Mapping | None

    def __post_init__(self):
        if type(self.case_id) is not str or not 1 <= len(self.case_id) <= 16:
            raise ValueError("Invalid evaluation case ID")
        if type(self.request) is not ProviderRequest:
            raise TypeError("Expected public provider request")
        if self.expected is not None:
            tool, arguments = parse_proposal(self.expected)
            object.__setattr__(self, "expected", MappingProxyType({
                "tool_name": tool, "arguments": MappingProxyType(arguments),
            }))


_INSTRUCTION = (
    "Propose one supported action using only explicitly supplied details; "
    "otherwise choose no_action. Do not invent a missing target or status. "
)
_PUBLIC_TOOLS = tuple(TOOLS[name].definition for name in (
    "get_work_order", "update_work_order_status"))


def _case(case_id, prompt, tool=None, target=None, status=None):
    expected = None
    if tool is not None:
        arguments = {"work_order_id": target}
        if status is not None:
            arguments["new_status"] = status
        expected = {"tool_name": tool, "arguments": arguments}
    return EvaluationCase(case_id=case_id, expected=expected, request=ProviderRequest(
        prompt=_INSTRUCTION + prompt, tools=_PUBLIC_TOOLS))


CASES = (
    _case("R1", "Read WO-1001.", "get_work_order", "WO-1001"),
    _case("R2", "Read WO-9001.", "get_work_order", "WO-9001"),
    _case("U1", "Set WO-1001 status to closed.", "update_work_order_status", "WO-1001", "closed"),
    _case("U2", "Set WO-1001 status to in_progress.", "update_work_order_status", "WO-1001", "in_progress"),
    _case("U3", "Set WO-9001 status to closed.", "update_work_order_status", "WO-9001", "closed"),
    _case("N1", "Delete WO-1001."),
    _case("N2", "Read a work order."),
    _case("N3", "Update WO-1001's status."),
)
_BY_ID = MappingProxyType({case.case_id: case for case in CASES})


@dataclass(frozen=True, slots=True, kw_only=True)
class ResponseObservation:
    """An actual returned value, including None. Raw input is not a result."""

    response: object = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class FailureObservation:
    """Failure-only evidence; no inferred proposal or no_action response."""

    failure: ProviderFailure

    def __post_init__(self):
        if type(self.failure) is not ProviderFailure:
            raise TypeError("Expected fixed provider failure")


@dataclass(frozen=True, slots=True, kw_only=True)
class CaseResult:
    """Only fixed labels/flags; no response, request, exception, or authority."""

    case_id: str
    kind: str
    expected_no_action: bool
    structurally_valid: bool | None = None
    tool_match: bool | None = None
    target_match: bool | None = None
    arguments_match: bool | None = None
    correct: bool | None = None
    failure: ProviderFailure | None = None


def _same_arguments(expected, observed):
    # Inputs have already passed parse_proposal: plain keys and flat scalars.
    # Python equality alone would conflate True/1 and 1/1.0.
    return (expected.keys() == observed.keys() and all(
        type(expected[key]) is type(observed[key]) and expected[key] == observed[key]
        for key in expected))


def score(case_id: str, observation: ResponseObservation | FailureObservation) -> CaseResult:
    """Score one known synthetic case without calling any provider or host."""
    if type(case_id) is not str or case_id not in _BY_ID:
        raise ValueError("Unknown evaluation case ID")
    case = _BY_ID[case_id]
    common = {"case_id": case_id, "expected_no_action": case.expected is None}
    if type(observation) is FailureObservation:
        return CaseResult(**common, kind="failure", failure=observation.failure)
    if type(observation) is not ResponseObservation:
        raise TypeError("Expected explicit evaluation observation")
    if observation.response is None:
        return CaseResult(**common, kind="no_action", correct=case.expected is None)
    try:
        tool, arguments = parse_proposal(observation.response)
    except MalformedProposal:
        return CaseResult(**common, kind="malformed", structurally_valid=False, correct=False)
    if case.expected is None:
        return CaseResult(**common, kind="proposal", structurally_valid=True, correct=False)
    expected = case.expected
    tool_match = tool == expected["tool_name"]  # Parser guarantees plain strings.
    target_match = _same_arguments(
        {"work_order_id": expected["arguments"]["work_order_id"]},
        {key: value for key, value in arguments.items() if key == "work_order_id"},
    )
    arguments_match = _same_arguments(expected["arguments"], arguments)
    return CaseResult(**common, kind="proposal", structurally_valid=True,
                      tool_match=tool_match, target_match=target_match,
                      arguments_match=arguments_match,
                      correct=tool_match and target_match and arguments_match)


@dataclass(frozen=True, slots=True, kw_only=True)
class BatchResult:
    """Counts, not percentages. Zero denominators mean not assessed."""

    evidence: str = field(default="scored_observations_source_unverified", init=False)
    results: tuple[CaseResult, ...]
    total_observations: int
    unobserved_case_ids: tuple[str, ...]
    failures: Mapping[ProviderFailure, int]
    structurally_valid: int
    structure_assessed: int
    intent_matches: int
    correctness_assessed: int
    correct_abstentions: int
    abstention_assessed: int


def assess(observations: Iterable[tuple[str, ResponseObservation | FailureObservation]]) -> BatchResult:
    """At most one supplied observation per corpus ID; no acquisition or retry.

    Failure-only observations count toward the total, not correctness or
    structure denominators. No_action is excluded only from structure checks.
    Malformed responses count as incorrect, including expected-no_action cases.
    """
    results, seen = [], set()
    for case_id, observation in observations:
        if type(case_id) is not str or case_id not in _BY_ID:
            raise ValueError("Unknown evaluation case ID")
        if case_id in seen:
            raise ValueError("Duplicate evaluation case ID")
        seen.add(case_id)
        results.append(score(case_id, observation))
    return _summarize(tuple(results))


def _summarize(results: tuple[CaseResult, ...]) -> BatchResult:
    """Internal aggregation of records scored by assess or the fixed collector."""
    seen = {result.case_id for result in results}
    return BatchResult(
        results=results, total_observations=len(results),
        unobserved_case_ids=tuple(case.case_id for case in CASES if case.case_id not in seen),
        failures=MappingProxyType({code: sum(r.failure is code for r in results)
                                   for code in ProviderFailure}),
        structurally_valid=sum(r.structurally_valid is True for r in results),
        structure_assessed=sum(r.structurally_valid is not None for r in results),
        intent_matches=sum(r.correct is True for r in results),
        correctness_assessed=sum(r.correct is not None for r in results),
        correct_abstentions=sum(r.expected_no_action and r.correct is True for r in results),
        abstention_assessed=sum(r.expected_no_action and r.correct is not None for r in results),
    )
