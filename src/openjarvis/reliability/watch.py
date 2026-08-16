"""The 24/7 supervisor.

:mod:`openjarvis.reliability.monitor` knows how to run checks on a cadence. This
module is what makes running it continuously, unattended, for weeks, a
defensible thing to do. It owns the decisions that only matter once nobody is
watching:

* **Startup safety** — refuse to start at all in a configuration that could
  reach production. A combination nobody intended is discovered at 3am
  otherwise.
* **Crash recovery** — an incident found mid-repair after a restart is parked in
  ``RECOVERY_REQUIRED``, never resumed. A process that died during ``FIXING``
  may have left a worktree, a branch, or a half-applied change; starting a
  second coding agent on top of that is how one outage becomes two.
* **Concurrency** — one repair at a time by default. Two agents in two
  worktrees producing two pull requests for the same root cause is worse than a
  slower queue.
* **Cooldown** — after a failed repair, wait before trying that incident again.
  Without this, a permanently broken check retries as fast as the loop spins.
* **Flapping** — an alternating check goes to a human, not to Claude.
* **Emergency stop** — one command that blocks new work without destroying
  anything already recorded.

The endpoint of everything here is a pull request and a notification. Nothing in
this module can deploy or write to production, and the startup gate refuses to
run if the configuration says otherwise.

Merging is the one endpoint beyond the pull request, added separately in
:mod:`openjarvis.reliability.merge` and off unless
``[reliability.merge] enabled`` says otherwise. It still deploys nothing — but
it does put code on the default branch without a human, so the startup banner
reports its real state rather than a constant.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from openjarvis.reliability.events import (
    RELIABILITY_FLAPPING_DETECTED,
    RELIABILITY_RECOVERY_REQUIRED,
    RELIABILITY_WATCH_STARTED,
    RELIABILITY_WATCH_STOPPED,
)
from openjarvis.reliability.flapping import FlappingDetector, FlappingVerdict
from openjarvis.reliability.types import (
    Evidence,
    EvidenceKind,
    Incident,
    IncidentState,
    Severity,
    TrustLevel,
    now_iso,
)

logger = logging.getLogger(__name__)

__all__ = [
    "RepairGate",
    "UnsafeConfigurationError",
    "WatchSupervisor",
    "assert_safe_to_start",
    "startup_banner",
    "stop_flag_path",
]

#: States that mean a repair was in flight when the process stopped.
#:
#: ``MERGED`` is the most important entry: a process that died there left a
#: change on the default branch whose production verification never finished.
#: Parking it demands a human look, which is the only safe reading — the
#: alternative is an incident that is neither being verified nor known to be
#: bad, sitting quietly while the code is live.
IN_FLIGHT_STATES = (
    IncidentState.FIXING,
    IncidentState.TESTING,
    IncidentState.VERIFYING,
    IncidentState.MERGED,
)


class UnsafeConfigurationError(RuntimeError):
    """Raised when the configuration combines repair with a production reach.

    Refusing to start is the correct response. Every one of these combinations
    is individually defensible and jointly dangerous, which is exactly the kind
    of mistake that survives review and is discovered during an incident.
    """


# ---------------------------------------------------------------------------
# Startup safety
# ---------------------------------------------------------------------------


def stop_flag_path(config: Any) -> "Path":
    """Where the emergency stop flag lives.

    A file rather than a signal or a socket: it survives a restart, which is the
    behaviour an operator wants from a stop they pulled in a panic. JARVIS must
    not quietly come back on because the host rebooted.

    Defined here, next to the gate that honours it, so that every reader —
    ``jarvis reliability watch``, ``jarvis reliability stop`` and the Control
    Center — resolves the same file. A dashboard that computed this path
    independently could report "not engaged" while a stop was in force, which is
    the one thing a stop indicator must never do.
    """
    from openjarvis.core.paths import get_config_dir

    configured = getattr(config.reliability, "db_path", "")
    if configured:
        return Path(configured).expanduser().parent / "STOPPED"
    return get_config_dir() / "reliability" / "STOPPED"


def assert_safe_to_start(config: Any) -> None:
    """Refuse to start a watcher whose configuration could reach production.

    Checks the *combinations*, not the individual flags. ``deploy_mode`` being
    permissive matters only if repair is on; repair being on matters only if
    something downstream can ship. Reporting the pair is what makes the message
    actionable.
    """
    rc = config.reliability
    problems: List[str] = []

    if rc.repair.enabled:
        if rc.policy.allow_push_to_default_branch:
            problems.append(
                "automatic repair is enabled AND pushing to the default branch "
                "is allowed — a repair could rewrite "
                f"'{rc.github.base_branch}' with no review"
            )
        if rc.policy.deploy_mode not in ("pr_only", "never"):
            problems.append(
                "automatic repair is enabled AND deploy_mode is "
                f"'{rc.policy.deploy_mode}' — a repair could reach production "
                "without a human"
            )
        if rc.supabase.allow_production_writes:
            problems.append(
                "automatic repair is enabled AND Supabase production writes are "
                "allowed — a repair could modify live data"
            )
        if not rc.repair.workspace:
            problems.append(
                "automatic repair is enabled but [reliability.repair] workspace "
                "is unset — there is nowhere safe to work"
            )

    if rc.merge.enabled:
        # Validated at startup rather than at merge time. The merge verb comes
        # from configuration, and discovering it is invalid at the moment of the
        # most privileged call in the system means discovering it during an
        # outage, with a verified fix sitting unmerged.
        from openjarvis.reliability.sources.github import MERGE_METHODS

        if rc.merge.method not in MERGE_METHODS:
            problems.append(
                f"[reliability.merge] method is '{rc.merge.method}', which is not "
                f"a merge method GitHub accepts "
                f"({', '.join(sorted(MERGE_METHODS))})"
            )
        if not rc.github.enabled or not rc.github.repo:
            problems.append(
                "automatic merge is enabled but GitHub is not configured — "
                "there is no pull request to merge"
            )

    if problems:
        raise UnsafeConfigurationError(
            "JARVIS refuses to start:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n\nFix the configuration, or disable [reliability.repair]."
        )


def startup_banner(config: Any) -> str:
    """The state of every safety interlock, printed before monitoring begins.

    Printed on every start so the operator sees what JARVIS believes it is
    allowed to do, rather than what they remember configuring.
    """
    rc = config.reliability
    rows = [
        ("Monitoring", "ON"),
        ("Automatic repair", "ON" if rc.repair.enabled else "OFF"),
        ("Production deployment", "OFF"),
        (
            "Default branch push",
            "ON" if rc.policy.allow_push_to_default_branch else "OFF",
        ),
        # Read, not asserted. This line was a constant "OFF" for as long as no
        # merge code existed; leaving it constant after the code landed would
        # have made the startup banner reassure the operator about the one
        # capability it could no longer see.
        ("Automatic PR merge", "ON" if rc.merge.enabled else "OFF"),
        ("Supabase writes", "ON" if rc.supabase.allow_production_writes else "OFF"),
        ("Deploy mode", rc.policy.deploy_mode),
        ("Maximum repair attempts", str(rc.repair.max_attempts)),
        ("Concurrent repairs", str(rc.watch.max_concurrent_repairs)),
        ("Check interval", f"{rc.watch.interval_seconds}s"),
    ]
    width = max(len(name) for name, _ in rows)
    body = "\n".join(f"  {name.ljust(width)}  {value}" for name, value in rows)
    return f"JARVIS\n\n{body}\n"


# ---------------------------------------------------------------------------
# Repair admission
# ---------------------------------------------------------------------------


@dataclass
class RepairGate:
    """Decides whether an incident may start a repair *right now*.

    Separate from :class:`~openjarvis.reliability.policy.SafetyPolicy`, which
    answers the timeless question "is this kind of repair permitted at all?".
    This answers the operational one: is there capacity, has this incident just
    failed, is the check flapping, has an emergency stop been pulled.
    """

    max_concurrent: int = 1
    cooldown_seconds: float = 300.0
    #: How long to hold off after a repair that *succeeded*.
    #:
    #: Success means "a pull request is open", not "production is fixed" —
    #: nothing merges automatically, so the probe keeps failing until a human
    #: acts. Without this, every check interval opens a fresh incident, runs a
    #: fresh Claude session, and files another pull request for the same root
    #: cause. One outage became six pull requests in six ticks before this
    #: existed.
    pending_pr_cooldown_seconds: float = 3600.0
    clock: Callable[[], float] = time.monotonic
    _active: Dict[str, float] = field(default_factory=dict, repr=False)
    _cooldown_until: Dict[str, float] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _blocked: bool = False

    # -- emergency stop ---------------------------------------------------

    def block(self) -> None:
        """Refuse all further repairs. Existing records are untouched."""
        with self._lock:
            self._blocked = True

    def unblock(self) -> None:
        """Allow repairs again."""
        with self._lock:
            self._blocked = False

    @property
    def blocked(self) -> bool:
        """Whether new repairs are refused."""
        with self._lock:
            return self._blocked

    # -- admission --------------------------------------------------------

    def may_start(self, incident_id: str, *, fingerprint: str = "") -> tuple[bool, str]:
        """Whether this incident may start a repair now, and why not.

        Cooldowns are keyed on the *fingerprint* as well as the incident id,
        because a recurring failure gets a new incident each time it returns.
        Keying only on the id would let the same root cause be repaired again on
        the very next check.
        """
        with self._lock:
            if self._blocked:
                return False, "new repairs are blocked (emergency stop)"
            if incident_id in self._active:
                return False, "a repair for this incident is already running"
            if fingerprint and fingerprint in self._active.values():
                return False, "a repair for this same failure is already running"
            if len(self._active) >= max(1, self.max_concurrent):
                running = ", ".join(sorted(self._active))
                return (
                    False,
                    f"the concurrency limit of {self.max_concurrent} is reached "
                    f"(running: {running})",
                )
            now = self.clock()
            for key in (incident_id, fingerprint):
                if not key:
                    continue
                until, why = self._cooldown_until.get(key, (0.0, ""))
                if now < until:
                    return False, f"{why} for another {until - now:.0f}s"
            return True, ""

    def start(self, incident_id: str, *, fingerprint: str = "") -> bool:
        """Claim a repair slot. Returns ``False`` when it could not be claimed."""
        with self._lock:
            allowed, _reason = self.may_start(incident_id, fingerprint=fingerprint)
            if not allowed:
                return False
            self._active[incident_id] = fingerprint or incident_id
            return True

    def finish(
        self,
        incident_id: str,
        *,
        succeeded: bool,
        fingerprint: str = "",
        opened_pull_request: bool = False,
    ) -> None:
        """Release the slot and start the appropriate cooldown.

        Both outcomes get one. A failure should not be retried immediately; a
        success should not be retried at all until a human has had the chance to
        merge the pull request it produced.
        """
        with self._lock:
            self._active.pop(incident_id, None)
            if succeeded and opened_pull_request:
                seconds, why = (
                    self.pending_pr_cooldown_seconds,
                    (
                        "a pull request is already open for this failure and nothing "
                        "merges automatically; waiting"
                    ),
                )
            elif not succeeded:
                seconds, why = (
                    self.cooldown_seconds,
                    ("cooling down after a failed repair"),
                )
            else:
                return
            if seconds <= 0:
                return
            until = self.clock() + seconds
            for key in (incident_id, fingerprint):
                if key:
                    self._cooldown_until[key] = (until, why)

    @property
    def active(self) -> List[str]:
        """Incidents currently being repaired."""
        with self._lock:
            return sorted(self._active)

    def snapshot(self) -> Dict[str, Any]:
        """State for the dashboard and the CLI."""
        with self._lock:
            now = self.clock()
            return {
                "blocked": self._blocked,
                "active": sorted(self._active),
                "max_concurrent": self.max_concurrent,
                "cooling_down": {
                    key: round(until - now, 1)
                    for key, (until, _why) in self._cooldown_until.items()
                    if until > now
                },
            }


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


@dataclass
class WatchSupervisor:
    """Long-running supervision around :class:`ReliabilityMonitor`.

    Parameters
    ----------
    monitor:
        The check loop.
    store:
        Incident store, for crash recovery and state queries.
    gate:
        Repair admission control.
    flapping:
        Flapping detector, shared with the detector.
    notifier:
        Optional notification router.
    """

    monitor: Any
    store: Any
    gate: RepairGate = field(default_factory=RepairGate)
    flapping: FlappingDetector = field(default_factory=FlappingDetector)
    notifier: Any = None
    bus: Any = None
    interval_seconds: float = 60.0
    clock: Callable[[], float] = time.monotonic
    started_at: str = ""
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: Optional[threading.Thread] = field(default=None, repr=False)

    # -- lifecycle --------------------------------------------------------

    def recover_interrupted_repairs(self) -> List[Incident]:
        """Park every incident that was mid-repair when the process stopped.

        Called before the first check. Nothing is resumed: the whole point is
        that a restart is not evidence the previous repair is safe to continue.
        """
        parked: List[Incident] = []
        for incident in self.store.list(open_only=True, limit=500):
            if incident.state not in IN_FLIGHT_STATES:
                continue
            reason = (
                f"JARVIS restarted while this incident was in "
                f"{incident.state.value}; a repair may have been interrupted. "
                "It will not be resumed automatically."
            )
            try:
                self.store.add_evidence(
                    incident,
                    Evidence(
                        kind=EvidenceKind.NOTE,
                        summary="Interrupted repair detected on startup",
                        content=(
                            f"State at restart: {incident.state.value}\n"
                            f"Attempts used: {incident.attempts_used}\n"
                            f"Detected at: {incident.created_at}\n"
                            "Inspect the worktree and branch before resuming."
                        ),
                        source="watch",
                        trust=TrustLevel.TRUSTED,
                    ),
                )
                self.store.transition(
                    incident, IncidentState.RECOVERY_REQUIRED, reason=reason
                )
            except Exception:
                logger.exception("could not park %s for recovery", incident.id)
                continue
            parked.append(incident)
            logger.warning("Incident %s needs manual recovery", incident.id)
            self._publish(
                RELIABILITY_RECOVERY_REQUIRED,
                {"incident_id": incident.id, "state": incident.state.value},
            )
            self._notify("human_required", incident, reason=reason)
        return parked

    def start(self) -> List[Incident]:
        """Run crash recovery, then begin monitoring on a background thread."""
        self.started_at = now_iso()
        parked = self.recover_interrupted_repairs()
        self._stop.clear()
        self.gate.unblock()
        self._publish(
            RELIABILITY_WATCH_STARTED,
            {"at": self.started_at, "recovered": [i.id for i in parked]},
        )
        self.monitor.start(poll_interval=min(5.0, self.interval_seconds))
        return parked

    def run_forever(self) -> None:
        """Block until stopped, running crash recovery first."""
        self.started_at = now_iso()
        self.recover_interrupted_repairs()
        self._stop.clear()
        self._publish(RELIABILITY_WATCH_STARTED, {"at": self.started_at})
        try:
            self.monitor.run_forever(poll_interval=min(5.0, self.interval_seconds))
        finally:
            self._publish(RELIABILITY_WATCH_STOPPED, {"at": now_iso()})

    def stop(self, *, timeout: float = 10.0) -> None:
        """Emergency stop: no new cycles, no new repairs, nothing deleted.

        Evidence, incidents, branches and worktrees are all left exactly as they
        are. Stopping is meant to be a safe thing to do in a panic, which means
        it must never be destructive.
        """
        self.gate.block()
        self._stop.set()
        try:
            self.monitor.stop(timeout=timeout)
        except Exception:  # pragma: no cover - stopping must not raise
            logger.exception("the monitor did not stop cleanly")
        self._publish(RELIABILITY_WATCH_STOPPED, {"at": now_iso()})

    @property
    def running(self) -> bool:
        """Whether the monitor loop is alive."""
        return bool(self.monitor.health().get("running"))

    # -- flapping ---------------------------------------------------------

    def record_result(self, probe_id: str, *, failed: bool) -> FlappingVerdict:
        """Feed a probe verdict to the flapping detector."""
        return self.flapping.record(probe_id, failed=failed)

    def escalate_flapping(self, incident: Incident, verdict: FlappingVerdict) -> bool:
        """Mark *incident* flapping and hand it to a human.

        Returns ``True`` when the incident was escalated by this call.
        """
        if incident.state is IncidentState.HUMAN_REQUIRED:
            return False
        incident.metadata["flapping"] = verdict.to_dict()
        try:
            self.store.add_evidence(
                incident,
                Evidence(
                    kind=EvidenceKind.NOTE,
                    summary="Flapping detected",
                    content=verdict.reason,
                    source="watch",
                    trust=TrustLevel.TRUSTED,
                ),
            )
            self.store.save(incident)
            if incident.can_transition_to(IncidentState.HUMAN_REQUIRED):
                self.store.transition(
                    incident,
                    IncidentState.HUMAN_REQUIRED,
                    reason=f"flapping: {verdict.reason}",
                )
        except Exception:
            logger.exception("could not escalate %s as flapping", incident.id)
            return False

        self.flapping.reset(verdict.probe_id)
        self._publish(
            RELIABILITY_FLAPPING_DETECTED,
            {"incident_id": incident.id, **verdict.to_dict()},
        )
        self._notify(
            "human_required",
            incident,
            reason=(
                "the check is flapping, so an automated repair would be "
                f"chasing an intermittent failure. {verdict.reason}"
            ),
        )
        return True

    # -- introspection ----------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Everything the CLI and dashboard need in one call."""
        open_incidents = self.store.list(open_only=True, limit=100)
        by_state: Dict[str, int] = {}
        for incident in open_incidents:
            by_state[incident.state.value] = by_state.get(incident.state.value, 0) + 1
        return {
            "running": self.running,
            "started_at": self.started_at,
            "interval_seconds": self.interval_seconds,
            "monitor": self.monitor.health(),
            "repairs": self.gate.snapshot(),
            "flapping": self.flapping.snapshot(),
            "open_incidents": len(open_incidents),
            "incidents_by_state": by_state,
            "recovery_required": [
                incident.id
                for incident in open_incidents
                if incident.state is IncidentState.RECOVERY_REQUIRED
            ],
            "production_deployment": "OFF",
            "automatic_merge": "OFF",
        }

    # -- plumbing ---------------------------------------------------------

    def _publish(self, event: str, data: Dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(event, data)
        except Exception:
            logger.exception("could not publish %s", event)

    def _notify(self, method: str, incident: Incident, **kwargs: Any) -> None:
        if self.notifier is None:
            return
        try:
            handler = getattr(self.notifier, method)
        except AttributeError:  # pragma: no cover
            return
        payload = dict(kwargs)
        if method == "human_required":
            payload.setdefault("attempts", incident.attempts_used)
            payload.setdefault("max_attempts", 3)
        try:
            handler(incident, **payload)
        except Exception:
            logger.exception("could not send a %s notification", method)


def severity_floor(value: str) -> Severity:
    """Parse a configured severity floor, defaulting to MEDIUM."""
    try:
        return Severity.parse(value)
    except ValueError:
        return Severity.MEDIUM
