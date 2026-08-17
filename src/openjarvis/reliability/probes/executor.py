"""Probe execution with retries and flake suppression.

A single failed probe run is not an incident.  Production sites blip: a slow
cold start, a transient DNS hiccup, one dropped connection.  Opening an incident
on every blip produces noise that trains the owner to ignore JARVIS, which is
worse than not monitoring at all.

Two mechanisms, deliberately distinct:

* **In-run retries** (``retry.attempts``) — retry immediately within one tick.
  Absorbs a one-off blip so the tick reports success.
* **N-of-M confirmation** (``retry.confirm_runs``) — require N *consecutive*
  failing ticks before the failure is believed.  Absorbs a longer wobble.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from openjarvis.reliability.probes._stubs import (
    MissingCredentialError,
    ProbeRunnerRegistry,
)
from openjarvis.reliability.probes.spec import ProbeSpec
from openjarvis.reliability.types import ProbeResult, Severity, now_iso

logger = logging.getLogger(__name__)

__all__ = ["ConfirmationTracker", "ProbeExecutor", "escalate_severity"]


def escalate_severity(spec: ProbeSpec, result: ProbeResult) -> Severity:
    """Refine a probe's declared severity using observed impact.

    The spec states the *baseline* severity of the workflow.  What actually
    happened can raise it: a site that will not load at all is worse than a
    single failed assertion inside it, whatever the spec said.

    Severity is never lowered — a probe author declaring something CRITICAL
    knows something the runtime does not.
    """
    declared = spec.severity
    kind = result.failure_kind

    escalated = declared
    if kind in ("navigation", "timeout") and result.steps_completed == 0:
        # Never got off the ground: the site is unreachable.
        escalated = Severity.CRITICAL
    elif result.http_status >= 500:
        escalated = (
            Severity.CRITICAL if declared.at_least(Severity.HIGH) else Severity.HIGH
        )
    elif kind == "network_failure":
        escalated = declared if declared.at_least(Severity.HIGH) else Severity.HIGH

    return escalated if escalated.at_least(declared) else declared


@dataclass
class ConfirmationTracker:
    """Counts consecutive failures per probe until they are believed.

    A success resets the counter, so an intermittent failure never accumulates
    its way to an incident.
    """

    consecutive_failures: Dict[str, int] = field(default_factory=dict)

    def record(self, probe_id: str, *, failed: bool) -> int:
        """Record a tick outcome; return the current consecutive-failure count."""
        if not failed:
            self.consecutive_failures.pop(probe_id, None)
            return 0
        count = self.consecutive_failures.get(probe_id, 0) + 1
        self.consecutive_failures[probe_id] = count
        return count

    def is_confirmed(self, probe_id: str, required: int) -> bool:
        """``True`` when the failure has been seen enough times to be believed."""
        return self.consecutive_failures.get(probe_id, 0) >= max(1, required)

    def reset(self, probe_id: str) -> None:
        """Forget a probe's failure history (used after an incident opens)."""
        self.consecutive_failures.pop(probe_id, None)


class ProbeExecutor:
    """Runs probe specs through the right runner, with retries.

    Parameters
    ----------
    base_url:
        Site root that relative probe URLs are resolved against.
    evidence_dir:
        Where screenshots and traces are written.
    runners:
        Optional pre-built runners keyed by ``runner`` name.  Defaults to
        lazily-created registry instances, so importing this module never
        requires Playwright.
    sleep:
        Injected for tests, so backoff does not make the suite slow.
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        evidence_dir: str = "",
        runners: Optional[Dict[str, Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
        runner_options: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self._base_url = base_url
        self._evidence_dir = evidence_dir
        self._runners: Dict[str, Any] = dict(runners or {})
        self._sleep = sleep
        self._runner_options = runner_options or {}

    def runner_for(self, spec: ProbeSpec) -> Any:
        """Return (creating if needed) the runner for *spec*."""
        if spec.runner not in self._runners:
            options = self._runner_options.get(spec.runner, {})
            self._runners[spec.runner] = ProbeRunnerRegistry.create(
                spec.runner, **options
            )
        return self._runners[spec.runner]

    def run(self, spec: ProbeSpec) -> ProbeResult:
        """Run *spec*, retrying in-run up to ``spec.retry.attempts`` times.

        Returns the last result, or the first successful one.  A missing
        credential is a configuration error, not a site failure, and is reported
        as its own ``failure_kind`` so it never opens a site incident.
        """
        attempts = max(1, spec.retry.attempts)
        result: Optional[ProbeResult] = None

        for attempt in range(1, attempts + 1):
            try:
                result = self.runner_for(spec).run(
                    spec,
                    base_url=self._base_url,
                    evidence_dir=self._evidence_dir or None,
                )
            except MissingCredentialError as exc:
                # Never an incident about the website: JARVIS is misconfigured.
                return ProbeResult(
                    probe_id=spec.id,
                    success=False,
                    failure_kind="misconfigured",
                    error=str(exc),
                    started_at=now_iso(),
                )
            except Exception as exc:
                logger.exception("Probe %s raised", spec.id)
                result = ProbeResult(
                    probe_id=spec.id,
                    success=False,
                    failure_kind="runner_error",
                    error=f"{type(exc).__name__}: {exc}",
                    started_at=now_iso(),
                )

            if result.success:
                if attempt > 1:
                    logger.info("Probe %s passed on retry %d", spec.id, attempt)
                return result
            if attempt < attempts:
                self._sleep(spec.retry.backoff_seconds)

        assert result is not None  # loop runs at least once
        return result

    def run_all(self, specs: List[ProbeSpec]) -> List[ProbeResult]:
        """Run every enabled spec, in order."""
        return [self.run(spec) for spec in specs if spec.enabled]
