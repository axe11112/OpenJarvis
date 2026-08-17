"""Supabase source — backend health, logs, auth and RLS diagnostics.

Read-only by default and by design.  Every method here either reads state or
reads logs; the one method that can execute SQL routes through
:mod:`~openjarvis.reliability.sources.sql_guard` and refuses to write unless
every gate in ``docs/JARVIS_SECURITY.md`` §5 is open.

Auth diagnostics are derived from aggregate log data — error codes and counts —
never by reading user records.

**Free-tier log retention is short (roughly a day).**  Anything JARVIS wants to
keep must be snapshotted into evidence at detection time; there is no going back
for it later.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from openjarvis.reliability.sources._stubs import (
    BaseSignalSource,
    CircuitOpenError,
    MissingTokenError,
    ResilientClient,
    SourceHealth,
    resolve_token,
)
from openjarvis.reliability.sources.sql_guard import (
    WriteGateClosedError,
    check_sql,
)
from openjarvis.reliability.types import Severity, Signal, TrustLevel, now_iso

logger = logging.getLogger(__name__)

__all__ = ["SupabaseSource"]

_API_ROOT = "https://api.supabase.com"

#: Log lines matching these are RLS denials worth surfacing.
_RLS_DENIAL_RE = re.compile(
    r"row-level security|violates row-level security policy|new row violates",
    re.IGNORECASE,
)

#: Auth failures worth counting.
_AUTH_FAILURE_RE = re.compile(
    r"invalid_grant|invalid login|email not confirmed|signup.*disabled|"
    r"token.*expired|unauthorized",
    re.IGNORECASE,
)

_MAX_LOG_ENTRIES = 100


class SupabaseSource(BaseSignalSource):
    """Reads project health and logs from the Supabase Management API.

    Parameters
    ----------
    project_ref:
        The project reference (the subdomain of your project URL).
    token_env:
        Name of the environment variable holding a read-only management token.
    allow_production_writes:
        The outermost write gate.  Defaults to ``False`` and should stay there.
    """

    source_id = "supabase"

    def __init__(
        self,
        *,
        project_ref: str,
        token_env: str = "SUPABASE_READONLY_TOKEN",
        allow_production_writes: bool = False,
        client: Optional[ResilientClient] = None,
    ) -> None:
        self.project_ref = project_ref
        self._token_env = token_env
        self._allow_writes = allow_production_writes
        self._client = client

    @property
    def client(self) -> ResilientClient:
        """Lazily built HTTP client, so constructing a source needs no token."""
        if self._client is None:
            token = resolve_token(self._token_env, source=self.source_id)
            self._client = ResilientClient(
                base_url=_API_ROOT,
                source=self.source_id,
                headers={"Authorization": f"Bearer {token}"},
            )
        return self._client

    # -- project state ----------------------------------------------------

    def get_project(self) -> Dict[str, Any]:
        """Return the project's basic state."""
        raw = self.client.get_json(f"/v1/projects/{self.project_ref}", default={})
        return {
            "id": (raw or {}).get("id", ""),
            "name": (raw or {}).get("name", ""),
            "status": (raw or {}).get("status", ""),
            "region": (raw or {}).get("region", ""),
        }

    def list_migrations(self) -> List[Dict[str, Any]]:
        """Return applied database migrations, oldest first."""
        raw = self.client.get_json(
            f"/v1/projects/{self.project_ref}/database/migrations", default=[]
        )
        return [
            {"version": item.get("version", ""), "name": item.get("name", "")}
            for item in (raw or [])
        ]

    def list_edge_functions(self) -> List[Dict[str, Any]]:
        """Return deployed Edge Functions and their status."""
        raw = self.client.get_json(
            f"/v1/projects/{self.project_ref}/functions", default=[]
        )
        return [
            {
                "slug": item.get("slug", ""),
                "name": item.get("name", ""),
                "status": item.get("status", ""),
                "version": item.get("version", 0),
            }
            for item in (raw or [])
        ]

    def detect_migration_drift(self, repo_migrations: List[str]) -> List[str]:
        """Return migrations present in the repo but not applied to the project.

        Schema drift is a common cause of "works locally, 500s in production",
        and it is invisible to a browser probe.
        """
        try:
            applied = {m["version"] for m in self.list_migrations() if m["version"]}
        except (httpx.HTTPError, CircuitOpenError, MissingTokenError) as exc:
            logger.warning("supabase: could not list migrations (%s)", exc)
            return []
        return sorted(v for v in repo_migrations if v and v not in applied)

    # -- logs -------------------------------------------------------------

    def query_logs(
        self,
        *,
        sql: str = "",
        limit: int = _MAX_LOG_ENTRIES,
    ) -> List[Dict[str, Any]]:
        """Run a log query and return its rows.

        The query is passed through the SQL guard: log queries read, and a
        statement that tries to do anything else is refused even here.
        """
        query = sql or (
            "select timestamp, event_message, metadata from edge_logs "
            f"order by timestamp desc limit {int(limit)}"
        )
        verdict = check_sql(query, allow_writes=False)
        if not verdict:
            raise WriteGateClosedError(f"refused log query: {verdict.reason}")
        try:
            raw = self.client.get_json(
                f"/v1/projects/{self.project_ref}/analytics/endpoints/logs.all",
                params={"sql": query},
                default={},
            )
        except (httpx.HTTPError, CircuitOpenError, MissingTokenError) as exc:
            logger.warning("supabase: log query failed (%s)", exc)
            return []
        result = (raw or {}).get("result", [])
        return result[:limit] if isinstance(result, list) else []

    def auth_diagnostics(self, *, limit: int = _MAX_LOG_ENTRIES) -> Dict[str, Any]:
        """Summarize authentication failures from aggregate log data.

        Counts and error codes only — never user records.
        """
        rows = self.query_logs(limit=limit)
        failures: Dict[str, int] = {}
        for row in rows:
            message = str(row.get("event_message", ""))
            match = _AUTH_FAILURE_RE.search(message)
            if match:
                key = match.group(0).lower()
                failures[key] = failures.get(key, 0) + 1
        return {
            "sampled": len(rows),
            "failure_count": sum(failures.values()),
            "by_kind": dict(sorted(failures.items(), key=lambda kv: -kv[1])),
        }

    def rls_diagnostics(self, *, limit: int = _MAX_LOG_ENTRIES) -> List[Dict[str, Any]]:
        """Return log entries that look like row-level-security denials.

        Reports *that* a policy denied an operation, so a human can decide
        whether the policy or the query is wrong.  JARVIS never proposes
        loosening a policy — that path is refused by the SQL guard.
        """
        findings: List[Dict[str, Any]] = []
        for row in self.query_logs(limit=limit):
            message = str(row.get("event_message", ""))
            if _RLS_DENIAL_RE.search(message):
                findings.append(
                    {
                        "timestamp": row.get("timestamp", ""),
                        "message": message[:500],
                    }
                )
        return findings

    # -- guarded execution ------------------------------------------------

    def execute_sql(self, sql: str, *, force: bool = False) -> List[Dict[str, Any]]:
        """Run a statement against the project database, guard first.

        Parameters
        ----------
        force:
            Caller's assertion that every *other* gate is open (capability
            granted, human approved).  It does not bypass the guard's
            never-allowed rules and it does not bypass
            ``allow_production_writes``.

        Raises
        ------
        WriteGateClosedError
            When the guard refuses, with the reason.
        """
        verdict = check_sql(sql, allow_writes=self._allow_writes and force)
        if not verdict:
            logger.warning(
                "supabase: refused SQL (%s): %s", verdict.matched_rule, verdict.reason
            )
            raise WriteGateClosedError(verdict.reason)
        raw = self.client.request(
            "POST",
            f"/v1/projects/{self.project_ref}/database/query",
            json={"query": sql},
            expected=(200, 201),
        )
        try:
            payload = raw.json()
        except ValueError:
            return []
        return payload if isinstance(payload, list) else [payload]

    # -- signal source contract -------------------------------------------

    def poll(self, *, since: Optional[str] = None) -> List[Signal]:
        """Report project-level problems as signals."""
        signals: List[Signal] = []
        try:
            project = self.get_project()
        except (httpx.HTTPError, CircuitOpenError, MissingTokenError) as exc:
            logger.warning("supabase: poll failed (%s)", exc)
            return []

        status = (project.get("status") or "").upper()
        if status and status not in ("ACTIVE_HEALTHY", "ACTIVE", "COMING_UP"):
            signals.append(
                Signal(
                    source=self.source_id,
                    kind="project_unhealthy",
                    title=f"Supabase project status is {status}",
                    severity=Severity.CRITICAL
                    if status in ("INACTIVE", "PAUSED", "REMOVED")
                    else Severity.HIGH,
                    component="database",
                    external_id=self.project_ref,
                    occurred_at=now_iso(),
                    trust=TrustLevel.EXTERNAL,
                    metadata={"status": status},
                )
            )

        try:
            denials = self.rls_diagnostics(limit=50)
        except (WriteGateClosedError, httpx.HTTPError, CircuitOpenError):
            denials = []
        if denials:
            signals.append(
                Signal(
                    source=self.source_id,
                    kind="rls_denials",
                    title=f"{len(denials)} row-level-security denial(s) in recent logs",
                    detail=denials[0]["message"][:200],
                    severity=Severity.MEDIUM,
                    component="database",
                    occurred_at=denials[0].get("timestamp") or now_iso(),
                    trust=TrustLevel.EXTERNAL,
                    metadata={"count": len(denials)},
                )
            )
        return signals

    def health(self) -> SourceHealth:
        """Check that the project is reachable with the configured token."""
        try:
            project = self.get_project()
        except MissingTokenError as exc:
            return SourceHealth(
                source=self.source_id,
                reachable=False,
                detail=str(exc),
                checked_at=now_iso(),
            )
        except CircuitOpenError as exc:
            return SourceHealth(
                source=self.source_id,
                reachable=False,
                degraded=True,
                detail=str(exc),
                checked_at=now_iso(),
            )
        except httpx.HTTPError as exc:
            return SourceHealth(
                source=self.source_id,
                reachable=False,
                detail=f"{type(exc).__name__}: {exc}",
                checked_at=now_iso(),
            )
        status = (project.get("status") or "").upper()
        return SourceHealth(
            source=self.source_id,
            reachable=True,
            degraded=status not in ("ACTIVE_HEALTHY", "ACTIVE", ""),
            detail=status,
            checked_at=now_iso(),
        )
