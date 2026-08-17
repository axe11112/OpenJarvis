"""REST endpoints for the JARVIS reliability dashboard.

Read-only by design. The dashboard shows what JARVIS knows and did; it does not
provide a way to make JARVIS do anything, because an HTTP endpoint that can
trigger a production repair is a much larger attack surface than one that
cannot.

Evidence artifacts are served through a path-validated endpoint rather than as
static files, so a crafted incident record cannot be used to read arbitrary
files off the host.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from fastapi import APIRouter, HTTPException, Query
    from fastapi.responses import FileResponse
except ImportError:  # pragma: no cover - server extra not installed
    raise ImportError("fastapi is required for reliability routes")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reliability", tags=["reliability"])

_store: Optional[Any] = None
_config: Optional[Any] = None


def _get_config() -> Any:
    global _config
    if _config is None:
        from openjarvis.core.config import load_config

        _config = load_config()
    return _config


def _get_store() -> Any:
    global _store
    if _store is None:
        from openjarvis.core.paths import get_config_dir
        from openjarvis.reliability.store import IncidentStore

        config = _get_config()
        db_path = getattr(config.reliability, "db_path", "") or str(
            get_config_dir() / "reliability" / "incidents.db"
        )
        _store = IncidentStore(db_path)
    return _store


def reset_state() -> None:
    """Drop cached singletons (used by tests)."""
    global _store, _config
    if _store is not None:
        try:
            _store.close()
        except Exception:  # pragma: no cover - defensive
            logger.exception("could not close the incident store")
    _store = None
    _config = None


def _evidence_root() -> Path:
    from openjarvis.core.paths import get_config_dir

    config = _get_config()
    configured = getattr(config.reliability.probes, "evidence_dir", "")
    return Path(
        configured or str(get_config_dir() / "reliability" / "evidence")
    ).resolve()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/health")
def get_health() -> Dict[str, Any]:
    """Overall system status: one traffic light per monitored surface."""
    from openjarvis.reliability.types import IncidentState, Severity

    config = _get_config()
    rc = config.reliability
    store = _get_store()

    open_incidents = store.list(open_only=True, limit=500)
    by_component: Dict[str, str] = {}
    for incident in open_incidents:
        worst = by_component.get(incident.component)
        if worst is None or Severity.parse(incident.severity).at_least(
            Severity.parse(worst)
        ):
            by_component[incident.component] = incident.severity.value

    def _status(enabled: bool, component_keys: List[str]) -> str:
        if not enabled:
            return "disabled"
        for key in component_keys:
            severity = by_component.get(key)
            if severity in ("CRITICAL", "HIGH"):
                return "down"
            if severity:
                return "degraded"
        return "healthy"

    return {
        "enabled": rc.enabled,
        "site": rc.site.base_url,
        "environment": rc.site.environment,
        "surfaces": {
            "website": _status(
                bool(rc.site.base_url),
                ["website", "authentication", "dashboard", "api"],
            ),
            "vercel": _status(rc.vercel.enabled, ["deployment"]),
            "supabase": _status(rc.supabase.enabled, ["database"]),
            "github": _status(rc.github.enabled, ["ci"]),
        },
        "open_incidents": len(open_incidents),
        "by_state": {
            state.value: sum(1 for i in open_incidents if i.state is state)
            for state in IncidentState
            if any(i.state is state for i in open_incidents)
        },
        "policy": {
            "deploy_mode": rc.policy.deploy_mode,
            "repair_enabled": rc.repair.enabled,
            "max_attempts": rc.repair.max_attempts,
            # Reported rather than assumed, so a reader of the dashboard does
            # not have to infer the answer to the question they actually have.
            "production_deployment": "OFF",
            "automatic_merge": "OFF",
            "default_branch_push": (
                "ON" if rc.policy.allow_push_to_default_branch else "OFF"
            ),
            "supabase_writes": ("ON" if rc.supabase.allow_production_writes else "OFF"),
        },
        "watch": {
            "enabled": rc.watch.enabled,
            "interval_seconds": rc.watch.interval_seconds,
            "max_concurrent_repairs": rc.watch.max_concurrent_repairs,
            "cooldown_seconds": rc.watch.cooldown_seconds,
            "flapping_window": rc.flapping.window,
            "flapping_threshold": rc.flapping.failure_threshold,
        },
        "recovery_required": [
            incident.id
            for incident in open_incidents
            if incident.state is IncidentState.RECOVERY_REQUIRED
        ],
        "flapping": [
            incident.id
            for incident in open_incidents
            if incident.metadata.get("flapping")
        ],
        "audit_chain_intact": store.verify_chain()[0],
    }


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


@router.get("/incidents")
def list_incidents(
    state: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    open_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    """List incidents, newest first."""
    from openjarvis.reliability.types import IncidentState, Severity

    store = _get_store()
    try:
        incidents = store.list(
            state=IncidentState.parse(state) if state else None,
            severity=Severity.parse(severity) if severity else None,
            open_only=open_only,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "incidents": [
            {
                "id": i.id,
                "severity": i.severity.value,
                "state": i.state.value,
                "component": i.component,
                "title": i.title,
                "environment": i.environment,
                "source": i.source,
                "occurrences": i.occurrences,
                "created_at": i.created_at,
                "updated_at": i.updated_at,
                "attempts": i.attempts_used,
            }
            for i in incidents
        ],
        "count": len(incidents),
    }


@router.get("/incidents/{incident_id}")
def get_incident(incident_id: str) -> Dict[str, Any]:
    """Full incident detail, including its audited history."""
    incident = _get_store().get(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"No such incident: {incident_id}")
    payload = incident.to_dict()
    # Artifact paths are host filesystem paths; expose a fetchable reference
    # instead so the browser never sees them.
    for item in payload.get("evidence", []):
        if item.get("artifact_path"):
            item["artifact_url"] = (
                f"/api/reliability/incidents/{incident_id}/evidence/{item['id']}"
            )
            item.pop("artifact_path", None)
    return payload


@router.get("/incidents/{incident_id}/evidence/{evidence_id}")
def get_evidence_artifact(incident_id: str, evidence_id: str) -> Any:
    """Serve one evidence artifact.

    The path is taken from the stored record and then re-validated against the
    evidence root, so a crafted record cannot escape the directory.
    """
    incident = _get_store().get(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="No such incident")

    match = next((e for e in incident.evidence if e.id == evidence_id), None)
    if match is None or not match.artifact_path:
        raise HTTPException(status_code=404, detail="No such artifact")

    root = _evidence_root()
    try:
        resolved = Path(match.artifact_path).resolve()
        resolved.relative_to(root)
    except (ValueError, OSError) as exc:
        logger.warning(
            "refusing artifact outside the evidence root: %s", match.artifact_path
        )
        raise HTTPException(
            status_code=403, detail="Artifact path is out of bounds"
        ) from exc

    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="Artifact is missing from disk")
    return FileResponse(str(resolved))


@router.get("/incidents/{incident_id}/history")
def get_incident_history(incident_id: str) -> Dict[str, Any]:
    """The incident's append-only transition log."""
    store = _get_store()
    if store.get(incident_id) is None:
        raise HTTPException(status_code=404, detail=f"No such incident: {incident_id}")
    return {
        "incident_id": incident_id,
        "transitions": [t.to_dict() for t in store.transitions_for(incident_id)],
        "chain_intact": store.verify_chain()[0],
    }


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


@router.get("/probes")
def list_probes() -> Dict[str, Any]:
    """The probe specs JARVIS is configured to run."""
    from openjarvis.core.paths import get_config_dir
    from openjarvis.reliability.probes.spec import load_probes

    config = _get_config()
    configured = getattr(config.reliability.probes, "directory", "")
    directory = Path(configured or str(get_config_dir() / "reliability" / "probes"))
    specs = load_probes(directory)
    return {
        "directory": str(directory),
        "probes": [
            {
                "id": s.id,
                "name": s.display_name,
                "component": s.component,
                "severity": s.severity.value,
                "runner": s.runner,
                "enabled": s.enabled,
                "schedule": f"{s.schedule_type}:{s.schedule_value}",
                "expects": s.expectation_summary(),
                # Names only: a credential value never reaches the API surface.
                "credentials": sorted(s.credentials.values()),
            }
            for s in specs
        ],
    }


# ---------------------------------------------------------------------------
# Repairs
# ---------------------------------------------------------------------------


@router.get("/repairs")
def list_repairs(limit: int = Query(20, ge=1, le=200)) -> Dict[str, Any]:
    """Recent repair attempts across all incidents."""
    store = _get_store()
    repairs: List[Dict[str, Any]] = []
    for incident in store.list(limit=200):
        for attempt in incident.attempts:
            repairs.append(
                {
                    "incident_id": incident.id,
                    "component": incident.component,
                    "attempt": attempt.number,
                    "branch": attempt.branch,
                    "changed_files": attempt.changed_files,
                    "diff_stat": attempt.diff_stat,
                    "tests_passed": attempt.tests_passed,
                    "verified": attempt.verified,
                    "outcome": attempt.outcome,
                    "started_at": attempt.started_at,
                }
            )
    repairs.sort(key=lambda r: r["started_at"], reverse=True)
    return {"repairs": repairs[:limit], "count": len(repairs)}


@router.get("/incidents/{incident_id}/report")
def get_incident_report(incident_id: str) -> Dict[str, Any]:
    """Post-incident report, built from the incident record alone.

    GET-only, like every route here: an HTTP endpoint that can trigger a
    production repair is a far larger attack surface than one that cannot.
    """
    from fastapi import HTTPException

    from openjarvis.reliability.report import build_report

    store = _get_store()
    incident = store.get(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="no such incident")
    return build_report(incident).to_dict()
