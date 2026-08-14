"""Signal source ABC, resilient HTTP client, and the circuit breaker.

Every infrastructure integration (Vercel, Supabase, GitHub) polls a third-party
API that will, eventually, be slow, rate-limited or down.  When that happens the
correct behaviour is to back off and report *that source* as degraded — not to
raise, not to open an incident about the website, and above all not to keep
hammering.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx

from openjarvis.reliability.types import Signal

logger = logging.getLogger(__name__)

__all__ = [
    "BaseSignalSource",
    "CircuitBreaker",
    "CircuitOpenError",
    "MissingTokenError",
    "ResilientClient",
    "SourceHealth",
    "resolve_token",
]

#: Cap on any single response body we retain, so a chatty API cannot blow up
#: an incident record.
MAX_BODY_CHARS = 20_000


class MissingTokenError(RuntimeError):
    """Raised when a source is enabled but its token env var is unset."""


class CircuitOpenError(RuntimeError):
    """Raised when a source's circuit breaker is open."""


def resolve_token(env_name: str, *, source: str) -> str:
    """Read a token from the named environment variable.

    Only the variable *name* ever appears in configuration, logs or errors.

    Raises
    ------
    MissingTokenError
        When the variable is unset, naming the variable and never a value.
    """
    if not env_name:
        raise MissingTokenError(f"{source}: no token_env configured")
    value = os.environ.get(env_name, "")
    if not value:
        raise MissingTokenError(f"{source}: ${env_name} is not set")
    return value


@dataclass(slots=True)
class SourceHealth:
    """A source's own health, distinct from the health of what it monitors."""

    source: str
    reachable: bool = True
    degraded: bool = False
    detail: str = ""
    checked_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "source": self.source,
            "reachable": self.reachable,
            "degraded": self.degraded,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


class CircuitBreaker:
    """Stops calling a failing dependency until it has had time to recover.

    Three states, in the classic arrangement:

    * **closed** — calls pass through; consecutive failures are counted.
    * **open** — calls are refused immediately for ``reset_timeout`` seconds.
    * **half-open** — one trial call is allowed; success closes the breaker,
      failure re-opens it.

    Without this, an API outage turns into a tight retry loop that burns rate
    limit and delays recovery for everyone.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._reset_timeout = reset_timeout
        self._clock = clock
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._half_open = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        """Return ``"closed"``, ``"open"`` or ``"half-open"``."""
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if self._half_open:
                return "half-open"
            if self._clock() - self._opened_at >= self._reset_timeout:
                return "half-open"
            return "open"

    def allow(self) -> bool:
        """Return ``True`` when a call may proceed."""
        with self._lock:
            if self._opened_at is None:
                return True
            if self._clock() - self._opened_at >= self._reset_timeout:
                self._half_open = True
                return True
            return False

    def record_success(self) -> None:
        """Reset the breaker after a successful call."""
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._half_open = False

    def record_failure(self) -> None:
        """Count a failure, opening the breaker at the threshold."""
        with self._lock:
            if self._half_open:
                # A trial call failed: straight back to open, timer restarted.
                self._opened_at = self._clock()
                self._half_open = False
                return
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._opened_at = self._clock()

    def reset(self) -> None:
        """Force the breaker closed (used by tests and manual recovery)."""
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._half_open = False


@dataclass
class ResilientClient:
    """HTTP client with retries, backoff, rate-limit awareness and a breaker.

    Shared by every source so the politeness rules live in exactly one place.

    Parameters
    ----------
    base_url:
        Prefix for relative paths.
    headers:
        Sent on every request.  Never logged.
    source:
        Name used in log lines and errors.
    """

    base_url: str
    source: str = "source"
    headers: Dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    max_retries: int = 3
    backoff_base: float = 0.5
    backoff_cap: float = 30.0
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    sleep: Callable[[float], None] = time.sleep
    jitter: Callable[[], float] = random.random
    _client: Optional[httpx.Client] = field(default=None, repr=False)

    #: Status codes worth retrying: transient server-side or throttling.
    RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=self.headers,
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        """Close the underlying connection pool."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def _delay(self, attempt: int, response: Optional[httpx.Response]) -> float:
        """Compute the wait before the next attempt.

        Honours ``Retry-After`` when the server sends it — guessing a shorter
        delay than the server asked for is how you get banned.
        """
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after:
                try:
                    return min(float(retry_after), self.backoff_cap)
                except ValueError:
                    pass
            # GitHub-style: absolute reset timestamp with zero remaining.
            remaining = response.headers.get("X-RateLimit-Remaining", "")
            reset = response.headers.get("X-RateLimit-Reset", "")
            if remaining == "0" and reset:
                try:
                    wait = float(reset) - time.time()
                    if wait > 0:
                        return min(wait, self.backoff_cap)
                except ValueError:
                    pass
        # Exponential backoff with full jitter, so parallel sources do not
        # synchronise into a thundering herd.
        exponential = min(self.backoff_base * (2**attempt), self.backoff_cap)
        return exponential * (0.5 + 0.5 * self.jitter())

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Any = None,
        expected: tuple[int, ...] = (200, 201),
    ) -> httpx.Response:
        """Issue a request, retrying transient failures.

        Raises
        ------
        CircuitOpenError
            When the breaker is open.
        httpx.HTTPStatusError
            For a non-retryable unexpected status.
        """
        if not self.breaker.allow():
            raise CircuitOpenError(
                f"{self.source}: circuit breaker is open; skipping {method} {path}"
            )

        client = self._ensure_client()
        last_error: Optional[Exception] = None
        response: Optional[httpx.Response] = None

        for attempt in range(self.max_retries + 1):
            try:
                response = client.request(method, path, params=params, json=json)
            except httpx.HTTPError as exc:
                last_error = exc
                response = None
                logger.warning(
                    "%s: %s %s failed (%s)",
                    self.source,
                    method,
                    path,
                    type(exc).__name__,
                )
            else:
                if response.status_code in expected:
                    self.breaker.record_success()
                    return response
                if response.status_code not in self.RETRY_STATUSES:
                    # A 401/403/404 will not improve with retrying; surface it.
                    self.breaker.record_success()  # the API is up, we asked wrong
                    response.raise_for_status()
                    return response
                last_error = httpx.HTTPStatusError(
                    f"{self.source}: HTTP {response.status_code} for {path}",
                    request=response.request,
                    response=response,
                )
                logger.warning(
                    "%s: HTTP %d for %s (attempt %d/%d)",
                    self.source,
                    response.status_code,
                    path,
                    attempt + 1,
                    self.max_retries + 1,
                )

            if attempt < self.max_retries:
                self.sleep(self._delay(attempt, response))

        self.breaker.record_failure()
        assert last_error is not None
        raise last_error

    def get_json(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        default: Any = None,
    ) -> Any:
        """GET *path* and parse JSON, returning *default* on a decode failure."""
        response = self.request("GET", path, params=params, expected=(200,))
        try:
            return response.json()
        except ValueError:
            logger.warning("%s: %s returned non-JSON", self.source, path)
            return default


class BaseSignalSource(ABC):
    """Polls an infrastructure API and reports notable events as signals."""

    source_id: str

    @abstractmethod
    def poll(self, *, since: Optional[str] = None) -> List[Signal]:
        """Return signals observed since *since* (ISO 8601).

        Implementations must not raise for an ordinary API failure — return an
        empty list and let :meth:`health` report the problem.
        """

    @abstractmethod
    def health(self) -> SourceHealth:
        """Report whether this source can currently see its API."""
