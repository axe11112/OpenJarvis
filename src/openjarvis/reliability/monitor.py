"""The monitoring loop — what makes JARVIS continuous rather than a script.

Registers probes and infrastructure sources with the framework's existing
:class:`~openjarvis.scheduler.scheduler.TaskScheduler`, so JARVIS adds no new
process and inherits the daemon, systemd and launchd plumbing that already
exists under ``deploy/``.

Two properties matter more than the scheduling itself:

* **Tick isolation.** One probe raising must never stop the loop. Every tick is
  wrapped, and a failure is logged and counted rather than propagated.
* **Politeness.** Probes start at jittered offsets so they do not stampede the
  site in lockstep, and each source has its own circuit breaker.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from openjarvis.reliability.detector import Detection, Detector
from openjarvis.reliability.events import (
    RELIABILITY_TICK_END,
    RELIABILITY_TICK_START,
)
from openjarvis.reliability.probes.executor import ProbeExecutor
from openjarvis.reliability.probes.spec import ProbeSpec
from openjarvis.reliability.types import IncidentState

logger = logging.getLogger(__name__)

__all__ = ["MonitorStats", "ReliabilityMonitor", "ScheduledCheck"]


@dataclass(slots=True)
class MonitorStats:
    """Counters for what the monitor has done."""

    ticks: int = 0
    failures: int = 0
    incidents_opened: int = 0
    incidents_recovered: int = 0
    recurrences: int = 0
    suppressed: int = 0
    flapping: int = 0
    repairs_deferred: int = 0

    def to_dict(self) -> Dict[str, int]:
        """Serialize to a plain dict."""
        return {
            "ticks": self.ticks,
            "failures": self.failures,
            "incidents_opened": self.incidents_opened,
            "incidents_recovered": self.incidents_recovered,
            "recurrences": self.recurrences,
            "suppressed": self.suppressed,
            "flapping": self.flapping,
            "repairs_deferred": self.repairs_deferred,
        }


@dataclass
class ScheduledCheck:
    """One thing the monitor runs on a cadence."""

    key: str
    interval_seconds: float
    run: Callable[[], None]
    next_due: float = 0.0
    kind: str = "probe"

    def due(self, now: float) -> bool:
        """Whether this check should run at *now*."""
        return now >= self.next_due

    def reschedule(self, now: float) -> None:
        """Set the next due time."""
        self.next_due = now + self.interval_seconds


class ReliabilityMonitor:
    """Runs probes and source polls on a cadence, feeding the detector.

    Parameters
    ----------
    detector:
        Turns results into incidents.
    executor:
        Runs probe specs.
    specs:
        Probe specs to schedule.
    sources:
        Signal sources to poll.
    repair_loop:
        Optional repair loop.  When absent, JARVIS monitors and notifies but
        never modifies code.
    supervisor:
        Optional :class:`~openjarvis.reliability.watch.WatchSupervisor`.  When
        present it decides whether a repair may start at all — concurrency,
        cooldown, flapping and the emergency stop all live there, so that the
        monitor stays a scheduler rather than growing a policy engine.
    clock:
        Injected for tests.
    """

    def __init__(
        self,
        *,
        detector: Detector,
        executor: ProbeExecutor,
        specs: Optional[List[ProbeSpec]] = None,
        sources: Optional[List[Any]] = None,
        source_interval_seconds: float = 300.0,
        repair_loop: Any = None,
        notifier: Any = None,
        bus: Any = None,
        supervisor: Any = None,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._detector = detector
        #: Newest signal timestamp seen per source, so a poll asks for news
        #: rather than for history. In memory on purpose: the durable guard
        #: against re-reporting after a restart is the source's own age cutoff,
        #: and a persisted watermark could silently swallow a real signal that
        #: arrived while the process was down.
        self._watermarks: Dict[str, str] = {}
        self._executor = executor
        self._repair_loop = repair_loop
        self._notifier = notifier
        self._bus = bus
        self._supervisor = supervisor
        self._clock = clock
        self.stats = MonitorStats()
        self._specs: Dict[str, ProbeSpec] = {}
        self._checks: List[ScheduledCheck] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        for spec in specs or []:
            self.add_probe(spec, jitter=jitter)
        for source in sources or []:
            self.add_source(
                source, interval_seconds=source_interval_seconds, jitter=jitter
            )

    # -- registration -----------------------------------------------------

    def add_probe(
        self, spec: ProbeSpec, *, jitter: Callable[[], float] = random.random
    ) -> None:
        """Schedule *spec*, with a jittered first run."""
        if not spec.enabled:
            logger.info("probe %s is disabled; not scheduling", spec.id)
            return
        interval = self._interval_for(spec)
        self._specs[spec.id] = spec
        check = ScheduledCheck(
            key=f"probe:{spec.id}",
            interval_seconds=interval,
            run=lambda s=spec: self._run_probe(s),
            kind="probe",
        )
        # Stagger first runs so N probes do not hit the site simultaneously.
        check.next_due = self._clock() + jitter() * min(interval, 60.0)
        self._checks.append(check)

    def add_source(
        self,
        source: Any,
        *,
        interval_seconds: float = 300.0,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        """Schedule polling for a signal source."""
        check = ScheduledCheck(
            key=f"source:{getattr(source, 'source_id', 'unknown')}",
            interval_seconds=interval_seconds,
            run=lambda s=source: self._poll_source(s),
            kind="source",
        )
        check.next_due = self._clock() + jitter() * min(interval_seconds, 60.0)
        self._checks.append(check)

    @staticmethod
    def _interval_for(spec: ProbeSpec) -> float:
        """Resolve a probe's cadence from its schedule declaration."""
        if spec.schedule_type == "interval":
            try:
                return max(30.0, float(spec.schedule_value))
            except (TypeError, ValueError):
                return 300.0
        # Cron scheduling is delegated to TaskScheduler when the monitor runs
        # under the daemon; standalone mode approximates with a default.
        return 300.0

    # -- execution --------------------------------------------------------

    def tick(self) -> int:
        """Run every check that is due. Returns how many ran.

        Never raises: one broken check must not stop the loop.
        """
        now = self._clock()
        ran = 0
        for check in self._checks:
            if not check.due(now):
                continue
            self.stats.ticks += 1
            self._publish(RELIABILITY_TICK_START, {"check": check.key})
            try:
                check.run()
            except Exception:
                self.stats.failures += 1
                logger.exception("check %s raised; continuing", check.key)
            finally:
                check.reschedule(self._clock())
                self._publish(RELIABILITY_TICK_END, {"check": check.key})
            ran += 1
        return ran

    def run_forever(self, *, poll_interval: float = 5.0) -> None:
        """Block, ticking until :meth:`stop` is called."""
        logger.info("JARVIS monitoring started (%d check(s))", len(self._checks))
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(poll_interval)
        logger.info("JARVIS monitoring stopped")

    def start(self, *, poll_interval: float = 5.0) -> None:
        """Run the loop on a background daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever,
            kwargs={"poll_interval": poll_interval},
            daemon=True,
            name="jarvis-reliability",
        )
        self._thread.start()

    def stop(self, *, timeout: float = 10.0) -> None:
        """Signal the loop to stop and wait for it."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # -- check bodies -----------------------------------------------------

    def _run_probe(self, spec: ProbeSpec) -> None:
        result = self._executor.run(spec)
        verdict = None
        if self._supervisor is not None:
            verdict = self._supervisor.record_result(spec.id, failed=not result.success)
        detection = self._detector.from_probe(spec, result)
        self._account(detection)
        incident = detection.incident
        if incident is None:
            return

        # A check that alternates is intermittent, not broken. Sending it to a
        # coding agent spends a Claude session chasing a failure that has
        # already cleared, and verification then "passes" for the wrong reason.
        if (
            verdict is not None
            and verdict.flapping
            and (detection.opened or detection.recurred)
        ):
            self.stats.flapping += 1
            if self._supervisor.escalate_flapping(incident, verdict):
                logger.warning(
                    "probe %s is flapping; escalating %s instead of repairing",
                    spec.id,
                    incident.id,
                )
            return

        if detection.opened:
            self._maybe_repair(incident, spec)

    def _poll_source(self, source: Any) -> None:
        """Poll one signal source, asking only for what is new.

        The watermark is the whole point. ``poll()`` was called with no ``since``
        for as long as this method existed, so every cycle re-reported every
        failed deployment still in the API's newest page. Four production
        deployments cancelled on 15 August — superseded seventeen seconds later,
        and followed by nine successful production deployments — were still being
        re-reported 328 times two days later, holding four HIGH incidents open
        against a site that was completely healthy.
        """
        source_id = str(getattr(source, "source_id", "") or id(source))
        since = self._watermarks.get(source_id)
        try:
            signals = source.poll(since=since) if since else source.poll()
        except TypeError:
            # A source that predates the argument. Never a reason to stop
            # polling it.
            signals = source.poll()
        for detection in self._detector.from_signals(signals):
            self._account(detection)
        newest = max(
            (str(getattr(s, "occurred_at", "") or "") for s in signals),
            default="",
        )
        if newest and newest > (since or ""):
            self._watermarks[source_id] = newest

    def _account(self, detection: Detection) -> None:
        if detection.opened:
            self.stats.incidents_opened += 1
        if detection.recovered:
            self.stats.incidents_recovered += 1
        if detection.recurred:
            self.stats.recurrences += 1
        if detection.suppressed:
            self.stats.suppressed += 1

    def _maybe_repair(self, incident: Any, spec: ProbeSpec) -> None:
        """Hand a fresh incident to the repair loop, if one is configured.

        Admission is the supervisor's decision, not this method's: concurrency,
        cooldown after a failed attempt, and the emergency stop are operational
        state that outlives any one tick.
        """
        if self._repair_loop is None:
            return

        gate = getattr(self._supervisor, "gate", None)
        fingerprint = getattr(incident, "fingerprint", "")
        if gate is not None:
            allowed, reason = gate.may_start(incident.id, fingerprint=fingerprint)
            if not allowed:
                logger.info("not repairing %s: %s", incident.id, reason)
                self.stats.repairs_deferred += 1
                return
            if not gate.start(incident.id, fingerprint=fingerprint):
                self.stats.repairs_deferred += 1
                return

        outcome = None
        try:
            outcome = self._repair_loop.run(incident, spec)
        except Exception:
            logger.exception("repair loop raised for %s", incident.id)
            return
        finally:
            if gate is not None:
                gate.finish(
                    incident.id,
                    fingerprint=fingerprint,
                    succeeded=bool(outcome is not None and outcome.resolved),
                    opened_pull_request=bool(
                        outcome is not None and outcome.pull_request_url
                    ),
                )
        if self._notifier is None:
            return
        try:
            if outcome.resolved:
                attempt = incident.attempts[-1] if incident.attempts else None
                self._notifier.resolved(
                    incident, attempt=attempt, verification=outcome.verification
                )
            elif incident.state is IncidentState.HUMAN_REQUIRED:
                self._notifier.human_required(
                    incident,
                    reason=outcome.reason,
                    attempts=outcome.attempts,
                    max_attempts=self._repair_loop.policy.max_attempts,
                )
        except Exception:
            logger.exception("could not notify about %s", incident.id)

    # -- introspection ----------------------------------------------------

    @property
    def checks(self) -> List[ScheduledCheck]:
        """Scheduled checks, for the CLI and dashboard."""
        return list(self._checks)

    def health(self) -> Dict[str, Any]:
        """Snapshot of what the monitor is doing."""
        return {
            "checks": len(self._checks),
            "probes": len(self._specs),
            "running": self._thread is not None and self._thread.is_alive(),
            **self.stats.to_dict(),
        }

    def _publish(self, event: str, data: Dict[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publish(event, data)
        except Exception:
            logger.exception("could not publish %s", event)
