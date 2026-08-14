"""Vercel source — deployments, build failures, and preview lookup.

Two jobs:

1. **Detection** — a failed production deployment is an incident in its own
   right, and JARVIS should know which commit caused it before anyone asks.
2. **Verification substrate** — :meth:`VercelSource.find_preview_deployment`
   is what makes independent verification affordable. A preview deployment of
   the repair branch is a real, running copy of the fixed application, so the
   probe that detected the failure can be re-run against it for free.

**Environment variables are enumerated by name only.** The value-returning
endpoint is never called, so a leaked JARVIS token cannot be used to harvest
the application's secrets through this class. See ``docs/JARVIS_SECURITY.md``
§4 and §3.2 layer 1.
"""

from __future__ import annotations

import logging
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
from openjarvis.reliability.types import Severity, Signal, TrustLevel, now_iso

logger = logging.getLogger(__name__)

__all__ = ["VercelSource"]

_API_ROOT = "https://api.vercel.com"

#: Vercel deployment states that mean "this did not ship".
FAILED_STATES = frozenset({"ERROR", "CANCELED"})

#: Cap on retained build-log text.
_MAX_LOG_CHARS = 8000


class VercelSource(BaseSignalSource):
    """Reads deployment state from the Vercel REST API. Read-only.

    Parameters
    ----------
    project_id:
        Vercel project ID or name.
    team_id:
        Team scope, when the project belongs to a team.
    token_env:
        Name of the environment variable holding a read-only token.
    """

    source_id = "vercel"

    def __init__(
        self,
        *,
        project_id: str,
        team_id: str = "",
        token_env: str = "VERCEL_READONLY_TOKEN",
        client: Optional[ResilientClient] = None,
    ) -> None:
        self.project_id = project_id
        self.team_id = team_id
        self._token_env = token_env
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

    def _params(self, **extra: Any) -> Dict[str, Any]:
        params: Dict[str, Any] = {k: v for k, v in extra.items() if v not in ("", None)}
        if self.team_id:
            params["teamId"] = self.team_id
        return params

    # -- deployments ------------------------------------------------------

    def list_deployments(
        self,
        *,
        limit: int = 20,
        target: str = "",
        state: str = "",
    ) -> List[Dict[str, Any]]:
        """Return recent deployments, newest first.

        Parameters
        ----------
        target:
            ``"production"`` or ``"preview"``.
        state:
            Vercel state filter, e.g. ``"ERROR"``.
        """
        raw = self.client.get_json(
            "/v6/deployments",
            params=self._params(
                projectId=self.project_id,
                limit=min(limit, 100),
                target=target,
                state=state,
            ),
            default={},
        )
        return [self._summary(item) for item in (raw or {}).get("deployments", [])]

    def get_deployment(self, deployment_id: str) -> Dict[str, Any]:
        """Return one deployment's detail."""
        raw = self.client.get_json(
            f"/v13/deployments/{deployment_id}", params=self._params(), default={}
        )
        return self._summary(raw or {})

    def get_build_logs(
        self, deployment_id: str, *, max_chars: int = _MAX_LOG_CHARS
    ) -> str:
        """Return truncated build output for a deployment.

        Build logs are untrusted external content: anything in the repository's
        build scripts can print anything at all.
        """
        try:
            raw = self.client.get_json(
                f"/v2/deployments/{deployment_id}/events",
                params=self._params(limit=200),
                default=[],
            )
        except (httpx.HTTPError, CircuitOpenError) as exc:
            logger.warning("vercel: could not fetch build logs (%s)", exc)
            return ""

        lines: List[str] = []
        for event in raw or []:
            if not isinstance(event, dict):
                continue
            payload = event.get("payload") or {}
            text = payload.get("text") or event.get("text") or ""
            if text:
                lines.append(str(text).rstrip())
        joined = "\n".join(lines)
        if len(joined) > max_chars:
            # Build failures put the error at the END, so keep the tail.
            return "... (truncated)\n" + joined[-max_chars:]
        return joined

    def find_preview_deployment(
        self, branch: str, *, limit: int = 20
    ) -> Optional[Dict[str, Any]]:
        """Return the newest READY preview deployment for *branch*.

        This is the target independent verification runs against.  Returns
        ``None`` when no preview is ready yet — the caller must wait rather than
        verify against production.
        """
        for deployment in self.list_deployments(limit=limit, target="preview"):
            if deployment["branch"] != branch:
                continue
            if deployment["state"] != "READY":
                continue
            return deployment
        return None

    def list_environment_variable_names(self) -> List[str]:
        """Return the *names* of the project's environment variables.

        Deliberately no method returns a value.  Vercel's API can decrypt and
        return them; JARVIS never asks.  Knowing that ``STRIPE_SECRET_KEY``
        exists is useful for diagnosis; knowing its value is a liability.
        """
        raw = self.client.get_json(
            f"/v9/projects/{self.project_id}/env",
            params=self._params(),
            default={},
        )
        return sorted(
            item.get("key", "")
            for item in (raw or {}).get("envs", [])
            if item.get("key")
        )

    # -- signal source contract -------------------------------------------

    def poll(self, *, since: Optional[str] = None) -> List[Signal]:
        """Report failed deployments as signals."""
        try:
            deployments = self.list_deployments(limit=20)
        except (httpx.HTTPError, CircuitOpenError, MissingTokenError) as exc:
            logger.warning("vercel: poll failed (%s)", exc)
            return []

        signals: List[Signal] = []
        for deployment in deployments:
            if deployment["state"] not in FAILED_STATES:
                continue
            if since and deployment["created_at"] and deployment["created_at"] <= since:
                continue
            production = deployment["target"] == "production"
            signals.append(
                Signal(
                    source=self.source_id,
                    kind="deployment_failed",
                    title=(
                        f"{deployment['target'] or 'preview'} deployment "
                        f"{deployment['state'].lower()}"
                    ),
                    detail=deployment["url"],
                    # A failed production deploy means the fix everyone is
                    # waiting on is not live; a failed preview is one PR's
                    # problem.
                    severity=Severity.HIGH if production else Severity.MEDIUM,
                    component="deployment",
                    external_id=deployment["id"],
                    occurred_at=deployment["created_at"] or now_iso(),
                    trust=TrustLevel.EXTERNAL,
                    metadata={
                        "commit_sha": deployment["commit_sha"],
                        "branch": deployment["branch"],
                        "target": deployment["target"],
                        "state": deployment["state"],
                    },
                )
            )
        return signals

    def health(self) -> SourceHealth:
        """Check that the project is reachable with the configured token."""
        try:
            self.client.get_json(
                f"/v9/projects/{self.project_id}", params=self._params(), default={}
            )
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
        return SourceHealth(source=self.source_id, checked_at=now_iso())

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _summary(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a deployment payload across Vercel's API versions."""
        meta = raw.get("meta") or {}
        created = raw.get("createdAt") or raw.get("created") or 0
        created_iso = ""
        if created:
            from datetime import datetime, timezone

            try:
                created_iso = datetime.fromtimestamp(
                    int(created) / 1000, tz=timezone.utc
                ).isoformat()
            except (ValueError, OSError, TypeError):
                created_iso = ""
        url = raw.get("url", "")
        return {
            "id": raw.get("uid") or raw.get("id") or "",
            "state": (raw.get("readyState") or raw.get("state") or "").upper(),
            "target": raw.get("target") or "",
            "url": f"https://{url}" if url and not url.startswith("http") else url,
            "created_at": created_iso,
            "commit_sha": meta.get("githubCommitSha", "")
            or meta.get("gitlabCommitSha", "")
            or meta.get("bitbucketCommitSha", ""),
            "branch": meta.get("githubCommitRef", "")
            or meta.get("gitlabCommitRef", "")
            or meta.get("bitbucketCommitRef", ""),
            "commit_message": (meta.get("githubCommitMessage", "") or "").split("\n")[
                0
            ][:200],
        }
