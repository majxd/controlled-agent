"""Bounded collection through a borrowed adapter; no execution or live attestation."""

from dataclasses import dataclass, field

from .proposal_evaluation import (
    CASES, BatchResult, FailureObservation, ResponseObservation, _summarize, score,
)
from .provider import ProposalAdapter, ProviderError


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionResult:
    """Scored records only; invocation counts do not establish HTTP attempts."""

    provenance: str = field(default="adapter_collection_source_unverified", init=False)
    batch: BatchResult
    status: str
    adapter_invocations: int
    stopped_case_id: str | None


def collect(adapter: ProposalAdapter) -> CollectionResult:
    """Visit the fixed corpus once, stopping on failure without closing resources.

    Only ProviderError from propose becomes a failure observation. Other errors
    and interruptions propagate unchanged, with a best-effort collection_result.
    If building or attaching that snapshot fails, the original exception wins.
    No raw response or exception is retained in the snapshot. The caller owns
    cleanup and must retain the returned/attached result before cleanup begins.
    """
    results = []
    invocations = 0
    case_id = None

    def snapshot(status):
        return CollectionResult(
            batch=_summarize(tuple(results)), status=status,
            adapter_invocations=invocations,
            stopped_case_id=None if status == "completed" else case_id,
        )

    try:
        for case in CASES:
            case_id = case.case_id
            invocations += 1
            try:
                response = adapter.propose(case.request)
            except ProviderError as error:
                # Scoring and summarization deliberately sit outside this catch.
                observation = FailureObservation(failure=error.reason)
            else:
                try:
                    result = score(case_id, ResponseObservation(response=response))
                finally:
                    del response
                results.append(result)
                continue
            results.append(score(case_id, observation))
            return snapshot("provider_failure")
        return snapshot("completed")
    except BaseException as error:
        try:
            status = "unexpected_failure" if isinstance(error, Exception) else "interrupted"
            error.collection_result = snapshot(status)
        except BaseException:
            pass  # Diagnostic construction/attachment must not replace the original.
        raise
