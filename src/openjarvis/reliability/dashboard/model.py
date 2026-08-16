"""The Control Center read model — JARVIS state, shaped for a screen.

Everything in this module is a *derivation*. It reads the incident store, the
probe specs, the resolved target and the last live diagnostic, and turns them
into the handful of values a screen shows. It stores nothing, decides nothing
operational, and contacts nothing.

Two rules from :mod:`openjarvis.reliability.health` are carried through
unchanged, because a dashboard is exactly where they are most easily lost:

* **Not-checked is never green.** A probe JARVIS has not observed reports
  ``NOT_VERIFIED``, never ``PASS``. A card whose check could not reach a verdict
  keeps its ``UNKNOWN`` / ``BLOCKED`` / ``NOT_CONFIGURED`` state instead of
  being rounded down to a traffic light. The rule is about things JARVIS is
  *meant* to have checked: a probe switched off in its spec reports
  ``DISABLED`` and sits outside the verdict entirely, since counting a
  deliberate decision as a gap makes the amber permanent and the signal
  worthless.
* **Only a real observed failure is a failure.** A missing token makes JARVIS
  blind; it does not make the target broken, and the overall status says so.

Nothing here can emit a credential. Values are read from the incident store,
from probe specs (which hold environment-variable *names*, never values) and
from the diagnostic report; free text is additionally passed through
:func:`redact` before it leaves the process.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.reliability.health import HealthState
from openjarvis.reliability.types import Incident, IncidentState, Severity

logger = logging.getLogger(__name__)

__all__ = [
    "CardView",
    "IncidentView",
    "OverallStatus",
    "ProbeStatus",
    "ProbeView",
    "SafetyPanel",
    "SafetyRow",
    "Snapshot",
    "build_snapshot",
    "incident_detail",
    "redact",
    "wiz_message",
]


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact(text: str) -> str:
    """Strip secrets and PII from text before it is rendered.

    The same boundary the notification router uses, for the same reason: an
    incident's evidence is captured from an outside system, and a page rendered
    in a browser is an exit point like any other. A scanner failure falls back
    to the regex stripper rather than passing the text through — the failure
    mode of a redaction step must be "less text", never "more".
    """
    if not text:
        return text
    try:
        from openjarvis.security.boundary import BoundaryGuard

        return BoundaryGuard(mode="redact").scan_outbound(text, destination="dashboard")
    except Exception:  # noqa: BLE001 - never let redaction failure leak raw text
        logger.exception("boundary scan failed; falling back to the stripper")
        try:
            from openjarvis.security.credential_stripper import CredentialStripper

            return CredentialStripper().strip(text)
        except Exception:  # pragma: no cover - defensive
            logger.exception("credential stripping failed; withholding the text")
            return "[withheld: content could not be scanned]"


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class OverallStatus(str, Enum):
    """The single word at the top of the screen.

    ``UNVERIFIED`` exists for the same reason ``HealthState.UNKNOWN`` does: from
    process start until the first diagnostic cycle completes, JARVIS has not
    looked yet, and printing ``HEALTHY`` in that window would be a lie with a
    very short shelf life and a very long consequence.
    """

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    UNVERIFIED = "UNVERIFIED"


class ProbeStatus(str, Enum):
    """What JARVIS knows about one probe right now.

    ``DISABLED`` is deliberately separate from ``NOT_VERIFIED``. They look
    alike — neither is a pass — but they mean opposite things to an operator.
    ``NOT_VERIFIED`` is a gap: something JARVIS is *supposed* to be watching
    and is not, which is a problem to fix. ``DISABLED`` is a decision: somebody
    turned the probe off, the watcher does not schedule it, and there is
    nothing to fix. Collapsing the two makes every deliberately-parked probe
    read as a permanent blind spot, and an amber dashboard that can never go
    green is one nobody looks at.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_VERIFIED = "NOT_VERIFIED"
    KNOWN_NOISE = "KNOWN_NOISE"
    DISABLED = "DISABLED"


#: Severities that make an open incident a full production failure rather than a
#: degradation. Matches the escalation floor used elsewhere in the system.
_FAILING_SEVERITIES = (Severity.CRITICAL, Severity.HIGH)


# ---------------------------------------------------------------------------
# View records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SafetyRow:
    """One interlock, as read from configuration.

    ``dangerous`` marks a value that widens what JARVIS may do, so the screen
    can colour it without re-deriving the policy.
    """

    label: str
    value: str
    dangerous: bool = False
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize. Contains no secrets."""
        return {
            "label": self.label,
            "value": self.value,
            "dangerous": self.dangerous,
            "detail": self.detail,
        }


@dataclass(slots=True)
class SafetyPanel:
    """Every interlock a reader wants confirmed, from the real configuration."""

    rows: List[SafetyRow] = field(default_factory=list)
    emergency_stop_engaged: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize."""
        return {
            "rows": [r.to_dict() for r in self.rows],
            "emergency_stop_engaged": self.emergency_stop_engaged,
        }


@dataclass(slots=True)
class CardView:
    """One monitored surface.

    ``state`` is a :class:`~openjarvis.reliability.health.HealthState` value,
    kept verbatim rather than collapsed, so ``UNKNOWN`` and ``NOT_CONFIGURED``
    stay visibly different from ``HEALTHY``.
    """

    key: str
    title: str
    state: str = HealthState.NOT_CHECKED.value
    summary: str = ""
    facts: List[Dict[str, str]] = field(default_factory=list)
    blind_spots: List[str] = field(default_factory=list)
    remediation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize."""
        return {
            "key": self.key,
            "title": self.title,
            "state": self.state,
            "summary": self.summary,
            "facts": list(self.facts),
            "blind_spots": list(self.blind_spots),
            "remediation": self.remediation,
        }


@dataclass(slots=True)
class ProbeView:
    """One configured probe and what JARVIS has observed of it."""

    id: str
    name: str
    component: str
    severity: str
    schedule: str
    runner: str
    enabled: bool
    status: str = ProbeStatus.NOT_VERIFIED.value
    reason: str = ""
    last_run: str = ""
    duration_seconds: Optional[float] = None
    incident_id: str = ""
    noise_profiles: List[str] = field(default_factory=list)
    credentials: List[str] = field(default_factory=list)
    expects: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize. ``credentials`` holds variable names, never values."""
        return {
            "id": self.id,
            "name": self.name,
            "component": self.component,
            "severity": self.severity,
            "schedule": self.schedule,
            "runner": self.runner,
            "enabled": self.enabled,
            "status": self.status,
            "reason": self.reason,
            "last_run": self.last_run,
            "duration_seconds": self.duration_seconds,
            "incident_id": self.incident_id,
            "noise_profiles": list(self.noise_profiles),
            "credentials": list(self.credentials),
            "expects": self.expects,
        }


@dataclass(slots=True)
class IncidentView:
    """One incident, in list form."""

    id: str
    severity: str
    state: str
    component: str
    title: str
    summary: str
    detected_at: str
    last_seen_at: str
    occurrences: int
    attempts: int
    probe_id: str
    source: str
    is_open: bool
    flapping: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize."""
        return {
            "id": self.id,
            "severity": self.severity,
            "state": self.state,
            "component": self.component,
            "title": self.title,
            "summary": self.summary,
            "detected_at": self.detected_at,
            "last_seen_at": self.last_seen_at,
            "occurrences": self.occurrences,
            "attempts": self.attempts,
            "probe_id": self.probe_id,
            "source": self.source,
            "is_open": self.is_open,
            "flapping": self.flapping,
        }


@dataclass(slots=True)
class Snapshot:
    """Everything the screen shows, at one instant."""

    generated_at: str
    overall: str
    target_name: str
    target_url: str
    target_repository: str
    environment: str
    monitoring_enabled: bool
    watch_armed: bool
    wiz: Dict[str, str] = field(default_factory=dict)
    cards: List[CardView] = field(default_factory=list)
    probes: List[ProbeView] = field(default_factory=list)
    incidents: List[IncidentView] = field(default_factory=list)
    safety: SafetyPanel = field(default_factory=SafetyPanel)
    blind_spots: List[str] = field(default_factory=list)
    open_incident_count: int = 0
    resolved_incident_count: int = 0
    audit_chain_intact: Optional[bool] = None
    last_cycle_at: str = ""
    next_cycle_at: str = ""
    cycle_interval_seconds: float = 60.0
    cycle_running: bool = False
    probe_verification: str = "off"
    watcher: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the whole snapshot for the API."""
        return {
            "generated_at": self.generated_at,
            "overall": self.overall,
            "target": {
                "name": self.target_name,
                "url": self.target_url,
                "repository": self.target_repository,
                "environment": self.environment,
            },
            "monitoring_enabled": self.monitoring_enabled,
            "watch_armed": self.watch_armed,
            "wiz": dict(self.wiz),
            "cards": [c.to_dict() for c in self.cards],
            "probes": [p.to_dict() for p in self.probes],
            "incidents": [i.to_dict() for i in self.incidents],
            "safety": self.safety.to_dict(),
            "blind_spots": list(self.blind_spots),
            "open_incident_count": self.open_incident_count,
            "resolved_incident_count": self.resolved_incident_count,
            "audit_chain_intact": self.audit_chain_intact,
            "cycle": {
                "last_at": self.last_cycle_at,
                "next_at": self.next_cycle_at,
                "interval_seconds": self.cycle_interval_seconds,
                "running": self.cycle_running,
                "probe_verification": self.probe_verification,
            },
            "watcher": dict(self.watcher),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


#: Human titles for the diagnostic's check names, in the order the screen wants.
_CARD_TITLES = {
    "website": "Website",
    "github": "GitHub",
    "vercel": "Vercel",
    "supabase": "Supabase",
    "probes": "Probes",
    "notifications": "Telegram notifications",
    "code_agent": "Code agent",
    "configuration": "Configuration",
}

_CARD_ORDER = (
    "website",
    "github",
    "vercel",
    "supabase",
    "probes",
    "notifications",
    "code_agent",
    "configuration",
)


def target_display_name(target: Any) -> str:
    """A human name for the monitored application.

    Derived from the identifiers already configured — the repository name, or
    failing that the site host — because inventing a separate "display name"
    setting would be one more thing to keep in sync with reality.
    """
    repository = getattr(target, "repository", "") or ""
    if repository and "/" in repository:
        name = repository.split("/", 1)[1]
    else:
        name = repository
    if not name:
        url = getattr(target, "production_url", "") or ""
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        name = host.removeprefix("www.")
    if not name:
        return "not configured"
    # "Wize-Performance" and "wizeperformance.com" both read better spaced.
    return name.replace("-", " ").replace("_", " ").strip() or name


def safety_panel(config: Any, *, stop_flag_engaged: bool) -> SafetyPanel:
    """Read every interlock out of the live configuration.

    Nothing here is a constant string chosen by the UI, and ``Automatic PR
    merge`` stopped being one the moment merging was implemented. It used to be
    hardcoded ``OFF`` with the detail "no merge path exists in the repair loop",
    which was true and then, on the commit that added
    :mod:`openjarvis.reliability.merge`, silently was not. A safety panel that
    reassures from a constant is worse than no panel, so it now reads the flag.

    ``Production deployment`` is still a constant, and still accurate: nothing
    in this codebase can trigger a deployment. The detail names the deploy mode
    beside it so a reader can check the claim rather than take it.
    """
    rc = config.reliability
    deploy_mode = rc.policy.deploy_mode
    rows = [
        SafetyRow(
            label="Automatic repair",
            value="ON" if rc.repair.enabled else "OFF",
            dangerous=bool(rc.repair.enabled),
            detail=f"max {rc.repair.max_attempts} attempt(s) per incident",
        ),
        SafetyRow(
            label="Production deployment",
            value="OFF",
            detail=f"deploy_mode = {deploy_mode}",
        ),
        SafetyRow(
            label="Default branch push",
            value="ON" if rc.policy.allow_push_to_default_branch else "OFF",
            dangerous=bool(rc.policy.allow_push_to_default_branch),
            detail=f"base branch {rc.github.base_branch or 'main'}",
        ),
        SafetyRow(
            label="Automatic PR merge",
            value="ON" if rc.merge.enabled else "OFF",
            dangerous=bool(rc.merge.enabled),
            detail=(
                f"{rc.merge.method} merge of the incident PR at the verified SHA"
                + ("" if rc.merge.require_status_checks else "; CI checks NOT required")
                if rc.merge.enabled
                else "[reliability.merge] enabled = false"
            ),
        ),
        SafetyRow(
            label="Supabase writes",
            value="ON" if rc.supabase.allow_production_writes else "OFF",
            dangerous=bool(rc.supabase.allow_production_writes),
            detail="SQL write guard active",
        ),
        SafetyRow(
            label="Deploy mode",
            value=deploy_mode,
            dangerous=deploy_mode not in ("pr_only", "never"),
        ),
        SafetyRow(
            label="Emergency stop",
            value="ENGAGED" if stop_flag_engaged else "not engaged",
            dangerous=stop_flag_engaged,
            detail="new repairs are blocked" if stop_flag_engaged else "",
        ),
    ]
    return SafetyPanel(rows=rows, emergency_stop_engaged=stop_flag_engaged)


def _fact_rows(facts: Dict[str, Any]) -> List[Dict[str, str]]:
    """Flatten a check's facts into label/value pairs, redacted."""
    rows: List[Dict[str, str]] = []
    for key, value in sorted(facts.items()):
        if value in (None, "", [], {}):
            continue
        rows.append(
            {
                "label": key.replace("_", " "),
                "value": redact(str(value))[:200],
            }
        )
    return rows


def cards_from_report(
    report: Any, probe_summary: Optional[CardView] = None
) -> List[CardView]:
    """Turn a :class:`DiagnosticReport` into one card per surface.

    ``probe_summary`` replaces the report's own probes check when the dashboard
    is not executing probes itself: the report would say ``NOT_CHECKED``, and
    the dashboard has a better answer built from the specs and the incident
    store.
    """
    by_name = {check.name: check for check in getattr(report, "checks", [])}
    cards: List[CardView] = []
    for key in _CARD_ORDER:
        if key == "probes" and probe_summary is not None:
            cards.append(probe_summary)
            continue
        check = by_name.get(key)
        if check is None:
            cards.append(
                CardView(
                    key=key,
                    title=_CARD_TITLES.get(key, key),
                    state=HealthState.NOT_CHECKED.value,
                    summary="not checked yet",
                )
            )
            continue
        blind = [
            f"{name}: {redact(cap.summary)[:200]}"
            for name, cap in check.capabilities.items()
            if not cap.state.was_checked
        ]
        facts = _fact_rows(check.facts)
        for name, cap in check.capabilities.items():
            if not cap.state.was_checked:
                # Already listed under blind spots, with its reason. Repeating
                # it as a fact made the unhealthiest cards the longest ones,
                # which buries the summary a reader came for.
                continue
            facts.append(
                {
                    "label": name.replace("_", " "),
                    "value": f"{cap.state.value} — {redact(cap.summary)[:160]}"
                    if cap.summary
                    else cap.state.value,
                }
            )
        cards.append(
            CardView(
                key=key,
                title=_CARD_TITLES.get(key, key),
                state=check.state.value,
                summary=redact(check.summary)[:300],
                facts=facts,
                blind_spots=blind,
                remediation=redact(check.remediation)[:300],
            )
        )
    return cards


def _last_observed_run(evidence_root: Path, probe_id: str) -> str:
    """When JARVIS last ran this probe, from the evidence it left behind.

    The probe runner creates ``<evidence>/<probe id>/<run id>/`` for every run,
    populated only when something needed capturing. The directory's existence is
    therefore a record that the run happened, and its modification time is when.

    This is an *observation*, not a verdict: it says JARVIS looked, not what it
    saw. The verdict comes from the incident store.
    """
    directory = evidence_root / probe_id
    try:
        runs = [entry for entry in directory.iterdir() if entry.is_dir()]
    except (OSError, ValueError):
        return ""
    if not runs:
        return ""
    newest = max(run.stat().st_mtime for run in runs)
    return datetime.fromtimestamp(newest, tz=timezone.utc).isoformat()


def _suppressed_count(result: Any) -> int:
    """How many events a run's noise profiles actually filtered out.

    A pass that filtered something is not the same fact as a pass that saw
    nothing, and an operator who cannot tell them apart has no way to notice a
    profile that has quietly grown too broad. The executor already counts this;
    the dashboard only has to stop discarding it.
    """
    metadata = getattr(result, "metadata", None) or {}
    try:
        return int(metadata.get("suppressed_console_count", 0)) + int(
            metadata.get("suppressed_request_count", 0)
        )
    except (TypeError, ValueError):
        return 0


def probe_views(
    specs: List[Any],
    incidents: List[Incident],
    *,
    evidence_root: Optional[Path] = None,
    verified: Optional[Dict[str, Any]] = None,
) -> List[ProbeView]:
    """Build one row per configured probe.

    The verdict is derived in a fixed order, and ``PASS`` is the hardest one to
    reach on purpose:

    ``FAIL``
        An incident attributed to this probe is open. That is the only evidence
        of failure the system records, and it is authoritative.
    ``NOT_VERIFIED``
        The probe is still a placeholder, or JARVIS has no record of ever
        having run it. Neither is a pass, and a dashboard that showed them as
        green would be exactly the false confidence the reliability system is
        built to avoid.
    ``DISABLED``
        ``enabled = false`` in the spec, so the watcher never schedules it.
        Reported as its own state and kept in the table with whatever history
        it has, because the row is still worth reading — but it is not a blind
        spot, because nobody is expecting it to run. Note the order: an open
        incident still wins. Switching a probe off must not be a way to clear
        the failure it already found.
    ``KNOWN_NOISE``
        The run passed, but only after a noise profile filtered something out.
        That is a materially different fact from a clean pass — the probe is
        green *because* JARVIS agreed in advance not to look at a class of
        error — and it needs its own colour rather than a footnote. Reported
        only when a suppression was actually counted in the run, never inferred
        from the spec having a profile configured.
    ``PASS``
        JARVIS ran it and no incident is open for it.

    ``verified`` carries results from probes the dashboard ran itself. When
    present those results win, because they are a direct observation rather
    than an inference from what the watcher happened to leave on disk.
    """
    from openjarvis.reliability.probes.placeholder import placeholder_reasons

    verified = verified or {}
    open_by_probe: Dict[str, Incident] = {}
    for incident in incidents:
        if not incident.is_open or not incident.probe_id:
            continue
        current = open_by_probe.get(incident.probe_id)
        if current is None or incident.severity.at_least(current.severity):
            open_by_probe[incident.probe_id] = incident

    views: List[ProbeView] = []
    for spec in specs:
        noise = list(getattr(spec.assertions, "ignore_known_noise", []) or [])
        view = ProbeView(
            id=spec.id,
            name=spec.display_name,
            component=spec.component,
            severity=spec.severity.value,
            schedule=f"{spec.schedule_type}:{spec.schedule_value}",
            runner=spec.runner,
            enabled=bool(spec.enabled),
            noise_profiles=noise,
            # Names only. A probe spec never holds a credential value, and this
            # reads the mapping's values, which are variable names.
            credentials=sorted(spec.credentials.values()),
            expects=spec.expectation_summary(),
        )
        if evidence_root is not None:
            view.last_run = _last_observed_run(evidence_root, spec.id)

        result = verified.get(spec.id)
        if result is not None:
            view.duration_seconds = round(float(result.duration_seconds), 3)
            view.last_run = result.started_at or view.last_run

        incident = open_by_probe.get(spec.id)
        placeholder = placeholder_reasons(spec)

        if incident is not None:
            view.status = ProbeStatus.FAIL.value
            view.incident_id = incident.id
            view.reason = redact(incident.title)[:200]
        elif result is not None and not result.success:
            view.status = ProbeStatus.FAIL.value
            view.reason = redact(result.error)[:200]
        elif not spec.enabled:
            view.status = ProbeStatus.DISABLED.value
            view.reason = "disabled in its spec; the watcher does not schedule it" + (
                f" — last observed {view.last_run}" if view.last_run else ""
            )
        elif placeholder:
            view.status = ProbeStatus.NOT_VERIFIED.value
            view.reason = "placeholder probe, refused rather than run: " + "; ".join(
                placeholder
            )
        elif result is not None:
            suppressed = _suppressed_count(result)
            if suppressed:
                view.status = ProbeStatus.KNOWN_NOISE.value
                view.reason = (
                    f"passed in {result.duration_seconds:.2f}s after ignoring "
                    f"{suppressed} known-noise event(s)"
                )
            else:
                view.status = ProbeStatus.PASS.value
                view.reason = f"passed in {result.duration_seconds:.2f}s"
        elif view.last_run:
            view.status = ProbeStatus.PASS.value
            view.reason = "JARVIS ran it and no incident is open"
        else:
            view.status = ProbeStatus.NOT_VERIFIED.value
            view.reason = "JARVIS has no record of running this probe"
        views.append(view)

    # Worst first, and disabled last: it is history, not news.
    order = {
        ProbeStatus.FAIL.value: 0,
        ProbeStatus.NOT_VERIFIED.value: 1,
        ProbeStatus.KNOWN_NOISE.value: 2,
        ProbeStatus.PASS.value: 3,
        ProbeStatus.DISABLED.value: 4,
    }
    views.sort(key=lambda v: (order.get(v.status, 9), v.id))
    return views


def probe_card(views: List[ProbeView], *, directory: Path) -> CardView:
    """Summarize the probe fleet as one card.

    ``HEALTHY`` requires every *active* probe to have actually been observed
    passing. A fleet where half the probes have never run is ``DEGRADED``, not
    green with a footnote.

    Disabled probes are excluded from the verdict and from the denominators
    entirely. They are counted in their own fact so the row is still visible,
    but a probe nobody asked to run cannot be a blind spot, and one parked spec
    must not hold the whole card amber forever.

    The exception is a disabled probe with an open incident: that row is
    ``FAIL``, set before this function sees it, and it still counts. Otherwise
    switching a probe off would clear the failure it just found.
    """
    card = CardView(key="probes", title=_CARD_TITLES["probes"])
    card.facts = [{"label": "directory", "value": str(directory)}]
    if not views:
        card.state = HealthState.NOT_CONFIGURED.value
        card.summary = f"no probe specs in {directory}"
        card.remediation = "Add probe specs describing real workflows of the target."
        return card

    disabled = [v for v in views if v.status == ProbeStatus.DISABLED.value]
    active = [v for v in views if v.status != ProbeStatus.DISABLED.value]

    failing = [v for v in active if v.status == ProbeStatus.FAIL.value]
    unverified = [v for v in active if v.status == ProbeStatus.NOT_VERIFIED.value]
    passing = [
        v
        for v in active
        if v.status in (ProbeStatus.PASS.value, ProbeStatus.KNOWN_NOISE.value)
    ]

    card.facts += [
        {"label": "configured", "value": str(len(active))},
        {"label": "passing", "value": str(len(passing))},
        {"label": "failing", "value": str(len(failing))},
        {"label": "not verified", "value": str(len(unverified))},
    ]
    if disabled:
        card.facts.append({"label": "disabled", "value": str(len(disabled))})
    card.blind_spots = [f"{v.id}: {v.reason}" for v in unverified]

    if not active:
        # Every spec is switched off. Nothing is watching the target, which is
        # a configuration state and emphatically not "all probes passing".
        card.state = HealthState.NOT_CONFIGURED.value
        card.summary = f"all {len(disabled)} probe(s) are disabled"
        card.remediation = (
            "Enable at least one probe, or JARVIS is not watching this target."
        )
        return card

    if failing:
        card.state = HealthState.FAILED.value
        card.summary = f"{len(failing)} of {len(active)} probes are failing"
    elif unverified and passing:
        card.state = HealthState.DEGRADED.value
        card.summary = (
            f"{len(passing)} passing, {len(unverified)} not verified of {len(active)}"
        )
    elif unverified:
        card.state = HealthState.NOT_CONFIGURED.value
        card.summary = f"none of {len(active)} probes have been verified"
    else:
        card.state = HealthState.HEALTHY.value
        card.summary = f"all {len(active)} probes passing"
    if disabled:
        card.summary += f" ({len(disabled)} disabled)"
    return card


def incident_views(incidents: List[Incident]) -> List[IncidentView]:
    """Open incidents first, then the most recent resolved ones."""
    views = [
        IncidentView(
            id=i.id,
            severity=i.severity.value,
            state=i.state.value,
            component=i.component,
            title=redact(i.title)[:300],
            summary=redact(i.summary)[:600],
            detected_at=i.created_at,
            last_seen_at=i.last_seen_at,
            occurrences=i.occurrences,
            attempts=i.attempts_used,
            probe_id=i.probe_id,
            source=i.source,
            is_open=i.is_open,
            flapping=bool(i.metadata.get("flapping")),
        )
        for i in incidents
    ]
    severity_rank = {s.value: -s.rank for s in Severity}
    views.sort(
        key=lambda v: (
            0 if v.is_open else 1,
            severity_rank.get(v.severity, 0),
            # Newest first inside each band.
            [-ord(c) for c in v.detected_at],
        )
    )
    return views


def derive_overall(
    *,
    report: Any,
    incidents: List[IncidentView],
    monitoring_enabled: bool,
) -> str:
    """Collapse everything into the one word at the top of the screen.

    Only a genuine observed failure — an open incident, or a check that ran and
    found something broken — reaches ``FAILED``. A check that could not run at
    all leaves the system ``DEGRADED``: JARVIS is blind there, which is a
    problem with JARVIS rather than evidence about the target.
    """
    if not monitoring_enabled:
        return OverallStatus.UNVERIFIED.value
    if report is None:
        return OverallStatus.UNVERIFIED.value

    open_incidents = [i for i in incidents if i.is_open]
    if any(Severity.parse(i.severity) in _FAILING_SEVERITIES for i in open_incidents):
        return OverallStatus.FAILED.value

    state = getattr(getattr(report, "overall", None), "state", None)
    if state is HealthState.FAILED:
        return OverallStatus.FAILED.value
    if open_incidents:
        return OverallStatus.DEGRADED.value
    if state is HealthState.HEALTHY:
        return OverallStatus.HEALTHY.value
    if state in (
        HealthState.DEGRADED,
        HealthState.UNKNOWN,
        HealthState.BLOCKED,
        HealthState.NOT_CONFIGURED,
    ):
        return OverallStatus.DEGRADED.value
    return OverallStatus.UNVERIFIED.value


def wiz_message(
    *,
    overall: str,
    incidents: List[IncidentView],
    blind_spots: List[str],
    repair_enabled: bool,
    emergency_stop: bool,
    monitoring_enabled: bool,
    watcher_status: str = "",
) -> Dict[str, str]:
    """What Wiz says, derived deterministically from state.

    No model is involved. Every branch here is a function of values already on
    the screen, so the character can never contradict the numbers next to it —
    which is the entire failure mode of a generated status line.
    """
    open_incidents = [i for i in incidents if i.is_open]

    if emergency_stop:
        return {
            "mood": "stopped",
            "headline": "An emergency stop is engaged.",
            "detail": (
                "I am still recording what I see, but I will not start any new "
                "work until the stop is lifted."
            ),
        }
    # An offline watcher outranks everything below it. Whatever the last cycle
    # concluded, nothing is being checked *now*, and saying "all systems
    # operational" over a dead watcher is the worst sentence this screen could
    # produce.
    if watcher_status in ("OFFLINE", "ERROR"):
        return {
            "mood": "alert",
            "headline": "My watcher is not running.",
            "detail": (
                "Nothing is being checked right now. Start it from here, or "
                "with `jarvis reliability service start`."
            ),
        }
    if watcher_status == "STARTING":
        return {
            "mood": "thinking",
            "headline": "My watcher is starting up.",
            "detail": "Give me a moment to get eyes on the target again.",
        }
    if not monitoring_enabled:
        return {
            "mood": "idle",
            "headline": "Monitoring is switched off.",
            "detail": "Set [reliability] enabled = true and I will start watching.",
        }
    if overall == OverallStatus.UNVERIFIED.value:
        return {
            "mood": "thinking",
            "headline": "Running my first checks.",
            "detail": "I will not claim anything is healthy until I have looked.",
        }

    if open_incidents:
        worst = open_incidents[0]
        plural = "" if len(open_incidents) == 1 else f" ({len(open_incidents)} open)"
        detail = (
            "I can diagnose this, but automatic repair is disabled."
            if not repair_enabled
            else "Automatic repair is enabled; I will not deploy or merge anything."
        )
        return {
            "mood": "alert",
            "headline": (f"I detected a production issue. {worst.id} is open{plural}."),
            "detail": detail,
        }

    if blind_spots:
        count = len(blind_spots)
        noun = "blind spot" if count == 1 else "blind spots"
        return {
            "mood": "watchful",
            "headline": f"Monitoring is active, but I have {count} {noun}.",
            "detail": (
                "Those checks did not reach a verdict, so I am not counting "
                "them as passes."
            ),
        }

    return {
        "mood": "happy",
        "headline": "All systems operational.",
        "detail": "Every check I can run reached a verdict, and all of them passed.",
    }


def build_snapshot(
    config: Any,
    *,
    incidents: List[Incident],
    specs: List[Any],
    report: Any = None,
    evidence_root: Optional[Path] = None,
    probe_directory: Optional[Path] = None,
    verified_probes: Optional[Dict[str, Any]] = None,
    audit_chain_intact: Optional[bool] = None,
    stop_flag_engaged: bool = False,
    last_cycle_at: str = "",
    next_cycle_at: str = "",
    cycle_interval_seconds: float = 60.0,
    cycle_running: bool = False,
    probe_verification: str = "off",
    watcher: Optional[Dict[str, Any]] = None,
    generated_at: str = "",
    notes: Optional[List[str]] = None,
) -> Snapshot:
    """Assemble the whole read model from state that was already gathered.

    Pure: every input is passed in, so the same inputs always produce the same
    screen. The service layer is what decides when to gather them.
    """
    from openjarvis.reliability.target import resolve_target
    from openjarvis.reliability.types import now_iso

    rc = config.reliability
    target = resolve_target(config)

    views = incident_views(incidents)
    probes = probe_views(
        specs,
        incidents,
        evidence_root=evidence_root,
        verified=verified_probes,
    )
    directory = probe_directory or Path(".")
    cards = cards_from_report(
        report, probe_summary=probe_card(probes, directory=directory)
    )

    blind_spots: List[str] = []
    if report is not None:
        blind_spots = [redact(spot)[:300] for spot in report.blind_spots()]
    blind_spots += [
        f"probes.{v.id}: {v.reason}"
        for v in probes
        if v.status == ProbeStatus.NOT_VERIFIED.value
    ]

    overall = derive_overall(
        report=report, incidents=views, monitoring_enabled=rc.enabled
    )
    panel = safety_panel(config, stop_flag_engaged=stop_flag_engaged)
    watcher = dict(watcher or {})
    watcher_status = str(watcher.get("status", ""))

    return Snapshot(
        generated_at=generated_at or now_iso(),
        overall=overall,
        target_name=target_display_name(target),
        target_url=target.production_url,
        target_repository=target.repository,
        environment=target.environment,
        monitoring_enabled=bool(rc.enabled),
        watch_armed=bool(rc.watch.enabled),
        wiz=wiz_message(
            overall=overall,
            incidents=views,
            blind_spots=blind_spots,
            repair_enabled=bool(rc.repair.enabled),
            emergency_stop=stop_flag_engaged,
            monitoring_enabled=bool(rc.enabled),
            watcher_status=watcher_status,
        ),
        cards=cards,
        probes=probes,
        incidents=views,
        safety=panel,
        blind_spots=blind_spots,
        open_incident_count=sum(1 for v in views if v.is_open),
        resolved_incident_count=sum(1 for v in views if not v.is_open),
        audit_chain_intact=audit_chain_intact,
        last_cycle_at=last_cycle_at,
        next_cycle_at=next_cycle_at,
        cycle_interval_seconds=cycle_interval_seconds,
        cycle_running=cycle_running,
        probe_verification=probe_verification,
        watcher=watcher,
        notes=list(notes or []),
    )


# ---------------------------------------------------------------------------
# Incident detail
# ---------------------------------------------------------------------------


def incident_detail(
    incident: Incident,
    transitions: List[Any],
    *,
    chain_intact: Optional[bool] = None,
) -> Dict[str, Any]:
    """One incident in full: evidence, history, transitions, repair, audit.

    Two things are deliberately removed on the way out:

    * **Artifact filesystem paths.** They are host paths, and the browser has no
      use for one it cannot open. Screenshots and traces stay on disk, named by
      kind and summary only.
    * **Raw evidence text.** Evidence is captured from the target — page text,
      console output, logs — and is untrusted by construction. It goes through
      :func:`redact` before it is rendered.
    """
    payload = incident.to_dict()

    for item in payload.get("evidence", []):
        item.pop("artifact_path", None)
        item["has_artifact"] = bool(
            next(
                (e.artifact_path for e in incident.evidence if e.id == item.get("id")),
                "",
            )
        )
        item["summary"] = redact(item.get("summary", ""))[:600]
        item["content"] = redact(item.get("content", ""))[:4000]
        item["metadata"] = {
            key: redact(str(value))[:300]
            for key, value in (item.get("metadata") or {}).items()
        }

    for attempt in payload.get("attempts", []):
        # A worktree path is a host path like an artifact path, and says nothing
        # a reader of the dashboard can act on.
        attempt.pop("worktree_path", None)
        attempt["claim"] = redact(attempt.get("claim", ""))[:2000]
        attempt["test_summary"] = redact(attempt.get("test_summary", ""))[:2000]
        attempt["diff_stat"] = redact(attempt.get("diff_stat", ""))[:1000]

    payload["title"] = redact(payload.get("title", ""))[:300]
    payload["summary"] = redact(payload.get("summary", ""))[:2000]
    payload["repro_steps"] = [
        redact(step)[:300] for step in payload.get("repro_steps", [])
    ]
    payload["metadata"] = {
        key: redact(str(value))[:600]
        for key, value in (payload.get("metadata") or {}).items()
    }
    payload["audit"] = {
        "transitions": [t.to_dict() for t in transitions],
        "chain_intact": chain_intact,
        "recorded_transitions": len(transitions),
    }
    payload["is_open"] = incident.is_open
    payload["is_terminal"] = incident.is_terminal
    return payload


def open_states() -> List[str]:
    """Every state that counts as an open incident. Used by the UI legend."""
    return [s.value for s in IncidentState if s is not IncidentState.RESOLVED]


def environ_has(name: str) -> bool:
    """Whether an environment variable is set, without reading its value.

    The only environment access this module performs. Everything the dashboard
    renders is a name and a boolean, so there is no path from a credential's
    value to a page — subscripting the environment anywhere in here would open
    one, and a test asserts that none exists.
    """
    return bool(name) and bool(os.environ.get(name, "").strip())
