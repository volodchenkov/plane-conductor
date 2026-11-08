class PlaneConductorError(Exception):
    """Base class for all Plane Conductor errors."""


class ConfigError(PlaneConductorError):
    """Raised when configuration is invalid or missing."""


class PlaneAPIError(PlaneConductorError):
    """Raised when a Plane REST call returns a non-2xx response."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        url: str | None = None,
        method: str | None = None,
        retry_exhausted: bool = False,
    ) -> None:
        super().__init__(f"Plane API {status_code}: {message}" + (f" ({url})" if url else ""))
        self.status_code = status_code
        self.message = message
        self.url = url
        #: HTTP method of the failed call (``None`` when unknown) — tower uses it to tell a
        #: failed *write* (outcome possibly unknown) from a failed read.
        self.method = method
        #: A 429 whose ``Retry-After`` budget (per-sleep / total) was exhausted.
        self.retry_exhausted = retry_exhausted

    @property
    def is_transient(self) -> bool:
        """5xx, transport errors (status_code == 0) and an exhausted 429 budget are worth retrying."""
        return self.status_code == 0 or self.status_code >= 500 or self.retry_exhausted


class AgentSpawnError(PlaneConductorError):
    """Raised when the claude subprocess could not be spawned."""


class SessionAlreadyRunningError(PlaneConductorError):
    """Raised when (nickname, issue) is already running in this orchestrator."""


class CapacityFullError(PlaneConductorError):
    """Raised when MAX_CONCURRENT_SESSIONS is reached."""


class RootUnresolvedError(PlaneConductorError):
    """The root issue of a trigger could not be determined (parent cycle or depth limit)."""
