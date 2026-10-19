"""Sequential host wiring; proposal data never acquires execution authority here."""

from .controller import Controller, ControllerReport
from .provider import ProposalAdapter, ProviderRequest


class ProviderHostStopped(RuntimeError):
    """Stopped-host misuse is not a provider's no_action result."""

    def __init__(self):
        super().__init__("provider_host_stopped")


class ProviderHostReentry(RuntimeError):
    """A nested call was rejected before either collaborator was invoked."""

    def __init__(self):
        super().__init__("provider_host_reentry")


class ProviderHost:
    """Borrow one existing controller and adapter for a sequential session.

    The caller owns all resources and provider cleanup. No locks, concurrent use,
    reset, retries, fallback, or admission registry. Stopping this wrapper does
    not cancel a controller call already in progress or roll back its effects.
    """

    def __init__(self, *, controller: Controller, adapter: ProposalAdapter):
        if type(controller) is not Controller or type(adapter) is not ProposalAdapter:
            raise TypeError("Expected existing controller and proposal adapter")
        self._controller = controller
        self._adapter = adapter
        self._running = False
        self._stopped = False

    @property
    def stopped(self) -> bool:
        """Host lifetime state only; never execution or approval authority."""
        return self._stopped

    def run_once(self, request: ProviderRequest) -> ControllerReport | None:
        """Produce once, optionally submit once, and retain permanent stop state.

        Exceptions propagate unchanged, including controller_report on interrupts.
        Never render raw exception diagnostics or automatically feed results back.
        """
        if self._running:
            self._stopped = True
            raise ProviderHostReentry()
        if self._stopped:
            raise ProviderHostStopped()

        self._running = True
        try:
            proposal = self._adapter.propose(request)
            # A provider can swallow the nested rejection. Do not accept even
            # no_action after that misuse, and never reach controller submission.
            if self._stopped:
                raise ProviderHostReentry()
            if proposal is None:
                return None

            report = self._controller.submit(proposal)
            # Once submission begins, preserve the actual report, including
            # nonterminal success after a callback swallowed a reentry error.
            # Never clear an already-set stop flag or rewrite reported effects.
            if report.terminal:
                self._stopped = True
            return report
        except BaseException:
            self._stopped = True
            raise
        finally:
            self._running = False
