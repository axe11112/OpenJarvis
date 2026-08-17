"""Target application configuration and credential presence.

Two jobs:

1. Resolve *which* application JARVIS monitors, from config with environment
   overrides, so the same checkout can point at staging or production without
   an edit.
2. Report whether the credentials each integration needs are present —
   **by name, never by value**. Nothing in this module reads a secret's
   content, and :func:`credential_report` is safe to print, log, send to
   Telegram, or paste into a support ticket.

Environment variables override config, so an operator can run a one-off
diagnostic against a different target without touching ``config.toml``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

__all__ = [
    "CredentialStatus",
    "TargetConfig",
    "credential_report",
    "accepted_names",
    "connectivity_report",
    "probe_host",
    "env_identifier",
    "resolve_target",
]

#: Environment overrides for the target's identity. These are identifiers, not
#: secrets — they are safe to print.
ENV_REPOSITORY = "TARGET_REPOSITORY"
ENV_BRANCH = "TARGET_BRANCH"
ENV_PRODUCTION_URL = "TARGET_PRODUCTION_URL"
ENV_VERCEL_PROJECT = "VERCEL_PROJECT"
ENV_VERCEL_TEAM = "VERCEL_TEAM"
ENV_SUPABASE_REF = "SUPABASE_PROJECT_REF"

#: Accepted spellings for each identifier, in precedence order.
#:
#: Both forms are read because both are natural to reach for, and the failure
#: mode of accepting only one is nasty: the operator exports ``TARGET_REPO``,
#: JARVIS ignores it, ``doctor`` still reports the identifier missing, and there
#: is nothing in the output to suggest the name was the problem. An alias costs
#: one dictionary entry; the alternative costs somebody an afternoon.
ENV_ALIASES: Dict[str, tuple] = {
    ENV_REPOSITORY: (ENV_REPOSITORY, "TARGET_REPO", "GITHUB_REPOSITORY"),
    ENV_BRANCH: (ENV_BRANCH, "TARGET_DEFAULT_BRANCH", "GITHUB_BASE_BRANCH"),
    ENV_PRODUCTION_URL: (ENV_PRODUCTION_URL, "PRODUCTION_URL", "TARGET_URL"),
    ENV_VERCEL_PROJECT: (ENV_VERCEL_PROJECT, "VERCEL_PROJECT_ID"),
    ENV_VERCEL_TEAM: (ENV_VERCEL_TEAM, "VERCEL_TEAM_ID"),
    ENV_SUPABASE_REF: (ENV_SUPABASE_REF, "SUPABASE_PROJECT_ID", "SUPABASE_REF"),
}


def env_identifier(canonical: str) -> str:
    """Return the first set value among *canonical* and its aliases."""
    for name in ENV_ALIASES.get(canonical, (canonical,)):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def accepted_names(canonical: str) -> str:
    """Render the accepted spellings, for diagnostics and documentation."""
    names = ENV_ALIASES.get(canonical, (canonical,))
    if len(names) == 1:
        return f"${names[0]}"
    return f"${names[0]} (or ${', $'.join(names[1:])})"


@dataclass(slots=True)
class TargetConfig:
    """Which application JARVIS is pointed at.

    Contains identifiers only. Credentials live in environment variables named
    by ``*_token_env`` fields on the config sections and are never copied here.
    """

    repository: str = ""
    branch: str = "main"
    production_url: str = ""
    vercel_project: str = ""
    vercel_team: str = ""
    supabase_ref: str = ""
    environment: str = "production"

    @property
    def is_configured(self) -> bool:
        """Whether enough is set to attempt anything at all."""
        return bool(
            self.repository
            or self.production_url
            or self.vercel_project
            or self.supabase_ref
        )

    def missing(self) -> List[str]:
        """Identifiers that are not set, as env-var names."""
        gaps: List[str] = []
        if not self.repository:
            gaps.append(ENV_REPOSITORY)
        if not self.production_url:
            gaps.append(ENV_PRODUCTION_URL)
        if not self.vercel_project:
            gaps.append(ENV_VERCEL_PROJECT)
        if not self.supabase_ref:
            gaps.append(ENV_SUPABASE_REF)
        return gaps

    @staticmethod
    def describe_missing(canonical: str) -> str:
        """Name every spelling JARVIS accepts for a missing identifier."""
        return accepted_names(canonical)

    def url_problem(self) -> str:
        """Return a complaint about ``production_url``, or '' when it is sane."""
        if not self.production_url:
            return ""
        parsed = urlparse(self.production_url)
        if parsed.scheme not in ("http", "https"):
            return (
                f"production_url must start with https:// (got {self.production_url!r})"
            )
        if not parsed.netloc:
            return f"production_url has no host (got {self.production_url!r})"
        if parsed.scheme == "http":
            return "production_url uses plain http; https is expected in production"
        return ""

    def repository_problem(self) -> str:
        """Return a complaint about ``repository``, or '' when it is sane."""
        if not self.repository:
            return ""
        if self.repository.count("/") != 1 or self.repository.startswith("http"):
            return f"repository must be in owner/name form (got {self.repository!r})"
        return ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize. Safe to print — identifiers only."""
        return {
            "repository": self.repository,
            "branch": self.branch,
            "production_url": self.production_url,
            "vercel_project": self.vercel_project,
            "vercel_team": self.vercel_team,
            "supabase_ref": self.supabase_ref,
            "environment": self.environment,
        }


def resolve_target(config: Any) -> TargetConfig:
    """Build a :class:`TargetConfig` from *config*, with env overrides."""
    rc = config.reliability
    return TargetConfig(
        repository=env_identifier(ENV_REPOSITORY) or rc.github.repo,
        branch=env_identifier(ENV_BRANCH) or rc.github.base_branch or "main",
        production_url=(env_identifier(ENV_PRODUCTION_URL) or rc.site.base_url).rstrip(
            "/"
        ),
        vercel_project=env_identifier(ENV_VERCEL_PROJECT) or rc.vercel.project_id,
        vercel_team=env_identifier(ENV_VERCEL_TEAM) or rc.vercel.team_id,
        supabase_ref=env_identifier(ENV_SUPABASE_REF) or rc.supabase.project_ref,
        environment=rc.site.environment or "production",
    )


@dataclass(slots=True)
class CredentialStatus:
    """Whether one credential is available. Never holds the value."""

    label: str
    env_name: str
    present: bool
    required_for: str = ""
    optional: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize. Safe to print."""
        return {
            "label": self.label,
            "env_name": self.env_name,
            "present": self.present,
            "required_for": self.required_for,
            "optional": self.optional,
        }


def _present(env_name: str) -> bool:
    """Whether an environment variable holds a non-empty value.

    Reads only the truthiness. The value is never returned, logged or retained.
    """
    return bool(env_name) and bool(os.environ.get(env_name, "").strip())


def credential_report(config: Any) -> List[CredentialStatus]:
    """Report which credentials are present, by variable name only.

    The returned objects carry booleans and names. There is deliberately no
    field capable of holding a secret, so the whole report is safe to render
    anywhere.
    """
    rc = config.reliability
    statuses = [
        CredentialStatus(
            label="GitHub",
            env_name=rc.github.token_env,
            present=_present(rc.github.token_env),
            required_for="reading commits, PRs and Actions",
        ),
        CredentialStatus(
            label="Vercel",
            env_name=rc.vercel.token_env,
            present=_present(rc.vercel.token_env),
            required_for="reading deployments and build logs",
        ),
        CredentialStatus(
            label="Supabase",
            env_name=rc.supabase.token_env,
            present=_present(rc.supabase.token_env),
            required_for="reading project health and logs",
        ),
        CredentialStatus(
            label="Telegram",
            env_name="TELEGRAM_BOT_TOKEN",
            present=_present("TELEGRAM_BOT_TOKEN"),
            required_for="sending notifications",
            optional=not rc.notify.enabled,
        ),
        CredentialStatus(
            label="Test account e-mail",
            env_name="JARVIS_TEST_EMAIL",
            present=_present("JARVIS_TEST_EMAIL") or _present("JARVIS_TEST_USER_EMAIL"),
            required_for="authenticated probes",
            optional=True,
        ),
        CredentialStatus(
            label="Test account password",
            env_name="JARVIS_TEST_PASSWORD",
            present=_present("JARVIS_TEST_PASSWORD")
            or _present("JARVIS_TEST_USER_PASSWORD"),
            required_for="authenticated probes",
            optional=True,
        ),
    ]
    return statuses


# ---------------------------------------------------------------------------
# Connectivity preflight
# ---------------------------------------------------------------------------

#: Hosts each integration needs, keyed by the name shown to the operator.
#: Deliberately hosts rather than full URLs: this answers "can this network get
#: there at all", before any credential is involved.
INTEGRATION_HOSTS: Dict[str, str] = {
    "GitHub": "api.github.com",
    "Vercel": "api.vercel.com",
    "Supabase": "api.supabase.com",
    "Telegram": "api.telegram.org",
}


def probe_host(host: str, *, timeout: float = 8.0) -> tuple[str, str]:
    """Return ``(state, detail)`` for a reachability check.

    States are ``REACHABLE``, ``BLOCKED`` or ``UNKNOWN``. No credential is sent:
    an unauthenticated request is enough to answer "can this network reach the
    host", which is the question an operator has *before* they start debugging
    tokens. A 401 or 403 from the service itself still means REACHABLE — we got
    an answer, which is the thing being tested.

    Crucially this goes through **the same HTTP stack, and the same proxy
    configuration, that JARVIS itself will use**. An earlier version opened a
    raw TLS socket instead, and reported every host REACHABLE on a network whose
    egress proxy was refusing all of them — the socket simply bypassed the
    proxy. A preflight that is green where the real client fails is worse than
    no preflight, because it sends the operator looking in the wrong place.
    """
    import httpx

    try:
        # trust_env=True is the whole point: honour HTTPS_PROXY, NO_PROXY and
        # the system CA exactly as the real clients do.
        with httpx.Client(timeout=timeout, trust_env=True, follow_redirects=False) as c:
            response = c.get(f"https://{host}/")
        return "REACHABLE", f"HTTP {response.status_code}"
    except httpx.ProxyError as exc:
        return "BLOCKED", f"egress proxy refused the connection: {exc}"
    except httpx.ConnectTimeout:
        return "BLOCKED", f"timed out after {timeout:.0f}s"
    except httpx.ConnectError as exc:
        return "BLOCKED", str(exc)
    except httpx.TransportError as exc:
        return "BLOCKED", f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return "UNKNOWN", f"{type(exc).__name__}: {exc}"


def connectivity_report(target: "TargetConfig") -> List[Dict[str, str]]:
    """Reachability for every integration host, plus the production site.

    Ordered so the operator reads the most fundamental problem first.
    """
    rows: List[Dict[str, str]] = []
    for label, host in INTEGRATION_HOSTS.items():
        state, detail = probe_host(host)
        rows.append({"name": label, "host": host, "state": state, "detail": detail})

    if target.production_url:
        host = urlparse(target.production_url).hostname or ""
        if host:
            state, detail = probe_host(host)
            rows.append(
                {"name": "Website", "host": host, "state": state, "detail": detail}
            )
    else:
        rows.append(
            {
                "name": "Website",
                "host": "",
                "state": "NOT_CONFIGURED",
                "detail": f"set {accepted_names(ENV_PRODUCTION_URL)}",
            }
        )
    return rows
