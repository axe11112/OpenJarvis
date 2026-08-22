"""The Control Center's data service — gathering, caching, never mutating.

Splits the work in two by cost, because the two questions a dashboard asks have
very different price tags:

*Cheap, every poll.* Incidents, probe specs, safety interlocks, the emergency
stop flag, the watcher's launchd state. All local reads: a SQLite query, a few
``stat`` calls, a ``launchctl print``. Safe to do every couple of seconds.

*Expensive, on a cadence.* The live diagnostic, which talks to GitHub, Vercel,
Supabase and the site itself. Run on a background thread at the configured
watch interval, and always with ``open_incidents=False`` — the dashboard reports
what JARVIS found, it does not get to open incidents of its own. Two systems
opening incidents for the same failure is precisely the duplicated state this
package exists to avoid.

Nothing here writes to the incident database, starts a repair, or touches the
emergency stop. The one action it can take is asking launchd to start the
watcher service, and that goes through
:class:`~openjarvis.reliability.dashboard.supervisor.LaunchdSupervisor`, which
refuses while a stop is engaged.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openjarvis.reliability.dashboard.model import (
    Snapshot,
    build_snapshot,
    incident_detail,
)
from openjarvis.reliability.dashboard.supervisor import (
    LaunchdSupervisor,
    WatcherStatus,
)

logger = logging.getLogger(__name__)

__all__ = ["DashboardService"]

#: Never refresh the diagnostic faster than this, whatever the configuration
#: says. The diagnostic makes real API calls against the same integrations the
#: watcher is polling; a dashboard that hammered them would be a second load
#: source pretending to be a viewer.
MIN_CYCLE_SECONDS = 30.0


class DashboardService:
    """Owns the read model, the refresh thread and the supervisor handle.

    Parameters
    ----------
    config:
        A ``JarvisConfig``.
    store:
        An :class:`~openjarvis.reliability.store.IncidentStore`. Read-only from
        here; the service never calls a method that writes.
    probe_verification:
        How much of the probe fleet the dashboard runs *itself*, to get a real
        verdict rather than an inference:

        ``"none"``
            Run nothing. Probe rows are derived from the incident store and the
            evidence the watcher left on disk. Purely observational.
        ``"http"``
            Default. Run the ``http`` probes only. They are single requests, so
            the added load is negligible, and they are also the probes that
            leave *no* trace on disk — without this they would read
            ``NOT_VERIFIED`` forever, which is honest but useless.
        ``"all"``
            Also run the browser probes. Real durations for everything, at the
            cost of driving a second browser against the target alongside the
            watcher's. Opt in deliberately.

        No mode ever opens an incident: the executor here is built without a
        store, so a failure the dashboard observes is displayed and discarded.
        The watcher remains the only thing that records anything.
    auto_recover:
        Whether an offline watcher should be restarted through launchd when the
        dashboard notices. Honours the emergency stop and the restart budget.
    """

    #: Accepted ``probe_verification`` modes.
    PROBE_MODES = ("none", "http", "all")

    def __init__(
        self,
        config: Any,
        *,
        store: Any,
        supervisor: Optional[LaunchdSupervisor] = None,
        probe_verification: str = "http",
        auto_recover: bool = True,
        cycle_seconds: Optional[float] = None,
        diagnostic_factory: Any = None,
        clock: Any = time.monotonic,
    ) -> None:
        if probe_verification not in self.PROBE_MODES:
            raise ValueError(
                f"probe_verification must be one of {self.PROBE_MODES}, "
                f"got {probe_verification!r}"
            )
        self._config = config
        self._store = store
        self._supervisor = supervisor or LaunchdSupervisor(config)
        self._probe_verification = probe_verification
        self._auto_recover = auto_recover
        self._clock = clock
        self._diagnostic_factory = diagnostic_factory
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        configured = float(
            cycle_seconds
            if cycle_seconds is not None
            else getattr(config.reliability.watch, "interval_seconds", 60) or 60
        )
        self.cycle_seconds = max(MIN_CYCLE_SECONDS, configured)

        self._report: Any = None
        self._verified: Dict[str, Any] = {}
        self._last_cycle_at: str = ""
        self._next_cycle_monotonic: float = 0.0
        self._cycle_running = False
        self._notes: List[str] = []
        self._recovery_attempted = False

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Begin refreshing in the background."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="jarvis-control-center",
        )
        self._thread.start()

    def close(self, *, timeout: float = 10.0) -> None:
        """Stop refreshing and release the store."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        try:
            self._store.close()
        except Exception:  # pragma: no cover - defensive
            logger.exception("could not close the incident store")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - a refresh failure is not fatal
                logger.exception("dashboard refresh failed; continuing")
            self._stop.wait(self.cycle_seconds)

    # -- the expensive half ------------------------------------------------

    def refresh(self) -> None:
        """Run one diagnostic cycle and cache the result.

        ``open_incidents=False`` is the load-bearing argument: the diagnostic is
        capable of opening incidents, and a viewer that did so would put a
        second writer on the incident store alongside the watcher.
        """
        from openjarvis.reliability.diagnostic import LiveDiagnostic
        from openjarvis.reliability.types import now_iso

        with self._lock:
            self._cycle_running = True

        try:
            factory = self._diagnostic_factory or (
                lambda: LiveDiagnostic(self._config, store=None)
            )
            diagnostic = factory()
            report = diagnostic.run(
                # The diagnostic's own probe pass is always skipped: the
                # dashboard runs probes through _run_probes, where it controls
                # which runners are involved and nothing reaches a store.
                include_probes=False,
                open_incidents=False,
            )
            verified: Dict[str, Any] = self._run_probes()
        finally:
            with self._lock:
                self._cycle_running = False

        with self._lock:
            self._report = report
            self._verified = verified
            self._last_cycle_at = now_iso()
            self._next_cycle_monotonic = self._clock() + self.cycle_seconds

        # Housekeeping that has to happen somewhere and belongs to nobody else:
        # the supervised logs grow while the watcher runs, and the wrapper's own
        # bounder only covers the case where the wrapper is alive.
        try:
            self._supervisor.bound_logs()
        except Exception:  # pragma: no cover - defensive
            logger.exception("could not bound the watcher logs")

    def _run_probes(self) -> Dict[str, Any]:
        """Execute the probes this mode covers, recording nothing.

        A placeholder probe is skipped rather than run, exactly as the live
        diagnostic skips it: a probe nobody has aimed at the real application
        must never be able to produce a ``PASS``.
        """
        from openjarvis.reliability.probes.placeholder import is_placeholder

        if self._probe_verification == "none":
            return {}

        results: Dict[str, Any] = {}
        executor = self._build_executor()
        for spec in self._load_specs():
            if not spec.enabled or is_placeholder(spec):
                continue
            if self._probe_verification == "http" and spec.runner != "http":
                continue
            try:
                results[spec.id] = executor.run(spec)
            except Exception:  # noqa: BLE001 - one bad probe is not a bad cycle
                logger.exception(
                    "probe %s raised during dashboard verification", spec.id
                )
        return results

    def _build_executor(self) -> Any:
        from openjarvis.reliability.probes.executor import ProbeExecutor
        from openjarvis.reliability.target import resolve_target

        rc = self._config.reliability
        browser: Dict[str, Any] = {
            "headless": self._config.tools.browser.headless,
            "viewport": (
                self._config.tools.browser.viewport_width,
                self._config.tools.browser.viewport_height,
            ),
        }
        if getattr(rc.probes, "browser_executable_path", ""):
            browser["executable_path"] = rc.probes.browser_executable_path
        return ProbeExecutor(
            base_url=resolve_target(self._config).production_url or rc.site.base_url,
            evidence_dir=str(self.evidence_root()),
            runner_options={
                "browser": browser,
                "http": {"verify_ssrf": not rc.probes.allow_private_targets},
            },
        )

    # -- the cheap half ----------------------------------------------------

    def probe_directory(self) -> Path:
        """Where the probe specs live, resolved the way the CLI resolves it."""
        from openjarvis.core.paths import get_config_dir

        configured = getattr(self._config.reliability.probes, "directory", "")
        return Path(
            configured or str(get_config_dir() / "reliability" / "probes")
        ).expanduser()

    def evidence_root(self) -> Path:
        """Where probe evidence is written."""
        from openjarvis.core.paths import get_config_dir

        configured = getattr(self._config.reliability.probes, "evidence_dir", "")
        return Path(
            configured or str(get_config_dir() / "reliability" / "evidence")
        ).expanduser()

    def _load_specs(self) -> List[Any]:
        from openjarvis.reliability.probes.spec import load_probes

        try:
            return load_probes(self.probe_directory())
        except Exception:  # noqa: BLE001 - a bad spec must not blank the screen
            logger.exception("could not load probe specs")
            return []

    def watcher_state(self) -> Any:
        """The watcher's launchd state, with auto-recovery applied once.

        Requirement, stated plainly: opening the dashboard while the watcher is
        unexpectedly dead should bring it back. That happens here, exactly once
        per service lifetime plus whatever the restart budget allows, and never
        while the emergency stop is engaged — the supervisor refuses that.
        """
        state = self._supervisor.status()
        if not self._auto_recover:
            return state
        if state.status not in (WatcherStatus.OFFLINE, WatcherStatus.ERROR):
            if state.status is WatcherStatus.ONLINE:
                self._recovery_attempted = False
            return state
        if not state.service_installed or not state.supervisor_supported:
            return state

        ok, message = self._supervisor.start()
        self._recovery_attempted = True
        self._note(
            f"watcher was {state.status.value}; "
            + ("asked launchd to start it" if ok else message)
        )
        if ok:
            # Report the transition rather than the stale reading: launchd has
            # accepted the request and the next poll will confirm.
            state.status = WatcherStatus.STARTING
            state.detail = "a start was requested; waiting for launchd"
        return state

    def _note(self, message: str) -> None:
        with self._lock:
            self._notes.append(message)
            del self._notes[:-10]

    # -- assembly ----------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """Build the current read model. Never raises on a partial system."""
        incidents = self._incidents()
        specs = self._load_specs()
        watcher = self.watcher_state()

        with self._lock:
            report = self._report
            verified = dict(self._verified)
            last_cycle = self._last_cycle_at
            running = self._cycle_running
            next_due = self._next_cycle_monotonic
            notes = list(self._notes)

        next_at = ""
        if last_cycle:
            remaining = max(0.0, next_due - self._clock())
            next_at = (
                datetime.now(timezone.utc) + timedelta(seconds=remaining)
            ).isoformat()

        return build_snapshot(
            self._config,
            incidents=incidents,
            specs=specs,
            report=report,
            evidence_root=self.evidence_root(),
            probe_directory=self.probe_directory(),
            verified_probes=verified,
            audit_chain_intact=self._audit_chain_intact(),
            stop_flag_engaged=self._supervisor.stop_engaged(),
            last_cycle_at=last_cycle,
            next_cycle_at=next_at,
            cycle_interval_seconds=self.cycle_seconds,
            cycle_running=running,
            probe_verification=self._probe_verification,
            watcher=watcher.to_dict(),
            notes=notes,
            autonomy=self._autonomy(),
            outages=self._outages(),
            ledger=self._ledger(),
        )

    def _outages(self) -> List[Any]:
        """Correlated outages, newest first. Never fatal to the page."""
        from openjarvis.reliability.outage import OutageRegistry, outages_path

        try:
            return OutageRegistry(path=outages_path(self._config)).all_outages()[:50]
        except Exception:  # noqa: BLE001
            logger.exception("could not read the outage registry")
            return []

    def _ledger(self) -> Any:
        """The notification ledger, read-only, for the "what was Sir told" panel."""
        from openjarvis.reliability.notify_ledger import NotificationLedger, ledger_path

        try:
            return NotificationLedger(path=ledger_path(self._config))
        except Exception:  # noqa: BLE001
            logger.exception("could not read the notification ledger")
            return None

    def _autonomy(self) -> Dict[str, Any]:
        """How much JARVIS is actually handling. Never fatal to the page."""
        from openjarvis.reliability.playbook import AutonomyMetrics

        try:
            return AutonomyMetrics(self._store).snapshot()
        except Exception:  # noqa: BLE001
            logger.exception("could not compute autonomy metrics")
            return {"available": False}

    def _incidents(self) -> List[Any]:
        try:
            return self._store.list(limit=200)
        except Exception:  # noqa: BLE001 - the watcher may hold a write lock
            logger.exception("could not read incidents")
            return []

    def _audit_chain_intact(self) -> Optional[bool]:
        try:
            return bool(self._store.verify_chain()[0])
        except Exception:  # noqa: BLE001
            logger.exception("could not verify the audit chain")
            return None

    def incident(self, incident_id: str) -> Optional[Dict[str, Any]]:
        """One incident in full, redacted, or ``None`` when it does not exist."""
        try:
            found = self._store.get(incident_id)
        except Exception:  # noqa: BLE001
            logger.exception("could not read incident %s", incident_id)
            return None
        if found is None:
            return None
        try:
            transitions = self._store.transitions_for(incident_id)
        except Exception:  # noqa: BLE001
            logger.exception("could not read transitions for %s", incident_id)
            transitions = []
        return incident_detail(
            found, transitions, chain_intact=self._audit_chain_intact()
        )

    # -- the only actions --------------------------------------------------

    def start_watcher(self) -> Tuple[bool, str]:
        """Ask launchd to start the watcher service."""
        ok, message = self._supervisor.start()
        self._note(message)
        return ok, message

    def restart_watcher(self) -> Tuple[bool, str]:
        """Ask launchd to restart the watcher service."""
        ok, message = self._supervisor.restart()
        self._note(message)
        return ok, message

    @property
    def supervisor(self) -> LaunchdSupervisor:
        """The launchd handle, for the CLI."""
        return self._supervisor
