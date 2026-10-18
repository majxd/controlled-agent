"""One configured offline response or failure; no network, tools, or retries."""

from .provider import ProviderError, ProviderFailure, ProviderRequest


class MockProvider:
    """Test source, including deliberately malformed untrusted responses.

    Does not validate or bless output; ProposalAdapter takes a checked copy.
    Repeated explicit calls return the configured response, not an agent loop.
    """

    def __init__(self, response: object = None, *, failure: ProviderFailure | None = None):
        if failure is not None and type(failure) is not ProviderFailure:
            raise TypeError("Expected provider failure code")
        if failure is not None and response is not None:
            raise ValueError("Configure a response or a failure")
        self._response = response
        self._failure = failure

    def propose(self, request: ProviderRequest) -> object:
        if type(request) is not ProviderRequest:
            raise TypeError("Expected public provider request")
        if self._failure is not None:
            raise ProviderError(self._failure)
        return self._response
