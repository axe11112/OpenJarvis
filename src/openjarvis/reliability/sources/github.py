"""GitHub source — commits, pull requests, Actions, and repair branches.

Read operations are used for correlation: given a failure at time T, which
commits landed just before it and what did they touch?

Write operations exist only for the repair loop and are deliberately narrow.
There is no method here that can push to the default branch, force-push, merge a
pull request, or modify a workflow file — those are refused structurally rather
than by policy, so a bug or an injected instruction cannot reach them.
See ``docs/JARVIS_SECURITY.md`` §4.
"""

from __future__ import annotations

import fnmatch
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
from openjarvis.reliability.types import Severity, Signal, TrustLevel, now_iso

logger = logging.getLogger(__name__)

__all__ = ["GitHubSource", "ProtectedPathError", "UnsafeBranchError"]

_API_ROOT = "https://api.github.com"

#: Paths a repair may never touch, whatever the policy says.  A self-modifying
#: CI configuration would let a compromised repair loop disable its own checks.
ALWAYS_PROTECTED_PATHS = (
    ".github/workflows/*",
    ".github/actions/*",
)


class UnsafeBranchError(RuntimeError):
    """Raised when an operation would write to a protected branch."""


class ProtectedPathError(RuntimeError):
    """Raised when a change touches a path repairs may never modify."""


def normalize_path(path: str) -> str:
    """Reduce *path* to the form the guard matches against.

    Separators are unified, ``.`` and ``..`` segments are resolved, and the
    result is lower-cased. Resolution is textual rather than filesystem-based on
    purpose: the guard must give the same answer for a path that does not exist
    yet (a file the agent is about to create) as for one that does, and it must
    not be steerable by a symlink planted in the worktree.

    A path that climbs above the repository root keeps its leading ``../``
    segments, so callers can reject it outright rather than having it silently
    collapse into a harmless-looking relative path.
    """
    unified = path.replace("\\", "/")
    segments: List[str] = []
    escaped = 0
    for segment in unified.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments:
                segments.pop()
            else:
                escaped += 1
            continue
        segments.append(segment)
    prefix = "../" * escaped
    return (prefix + "/".join(segments)).lower()


def escapes_repository(path: str) -> bool:
    """Whether *path* points outside the repository root.

    An absolute path or one that climbs past the root is never a legitimate
    target for a repair, whatever it names.
    """
    unified = path.replace("\\", "/")
    if unified.startswith("/") or re.match(r"^[a-zA-Z]:/", unified):
        return True
    return normalize_path(path).startswith("../")


def matches_path_pattern(path: str, pattern: str) -> bool:
    """Match *path* against a glob *pattern*, generously.

    Shared by the protected-path guard and the policy's security-sensitive
    check so the two cannot drift apart.  "Generously" is deliberate: a path
    guard that under-matches is a security hole, while one that over-matches
    merely sends a human a pull request.

    Three fnmatch sharp edges are handled explicitly:

    * ``lstrip("./")`` would strip leading ``.`` and ``/`` *characters*, turning
      ``.github/workflows/ci.yml`` into ``github/workflows/ci.yml``; use
      ``removeprefix``.
    * ``**/middleware.*`` does not match a bare ``middleware.ts`` at the
      repository root, because fnmatch wants the literal separator. Patterns are
      therefore also tried without the ``**/`` prefix and against the basename.
    * ``a/../.github/workflows/ci.yml`` and ``.github\\workflows\\ci.yml`` name a
      protected file without matching the pattern textually, so the path is
      normalized before any comparison.
    """
    normalized = normalize_path(path)
    lowered = pattern.replace("\\", "/").lower()
    basename = normalized.rsplit("/", 1)[-1]
    bare = lowered.removeprefix("**/")
    candidates = (
        lowered,
        bare,
        lowered.rstrip("/") + "/*",
        bare.rstrip("/") + "/*",
    )
    if any(fnmatch.fnmatch(normalized, candidate) for candidate in candidates):
        return True
    if fnmatch.fnmatch(basename, bare):
        return True
    # Bare-directory patterns like ".github/workflows/"
    return lowered.endswith("/") and normalized.startswith(lowered)


def is_protected_path(path: str, extra_patterns: Optional[List[str]] = None) -> bool:
    """Return ``True`` when *path* must never be modified by an automated repair.

    A path that leaves the repository is protected unconditionally: there is no
    pattern list under which writing to ``/etc/passwd`` or ``../../.ssh/config``
    is a legitimate repair.
    """
    if escapes_repository(path):
        return True
    candidates = list(ALWAYS_PROTECTED_PATHS) + list(extra_patterns or [])
    return any(matches_path_pattern(path, pattern) for pattern in candidates)


class GitHubSource(BaseSignalSource):
    """Reads repository state and, for repairs, creates branches and PRs.

    Parameters
    ----------
    repo:
        ``"owner/name"``.
    token_env:
        Name of the environment variable holding the token.
    base_branch:
        The branch JARVIS branches *from* and never writes *to*.
    allow_push_to_default_branch:
        Safety interlock.  When ``False`` (the default), any operation naming
        the base branch as a write target raises :class:`UnsafeBranchError`.
    """

    source_id = "github"

    def __init__(
        self,
        *,
        repo: str,
        token_env: str = "GITHUB_READONLY_TOKEN",
        base_branch: str = "main",
        branch_prefix: str = "jarvis/incident-",
        allow_push_to_default_branch: bool = False,
        protected_paths: Optional[List[str]] = None,
        client: Optional[ResilientClient] = None,
    ) -> None:
        if "/" not in repo:
            raise ValueError(f"repo must be 'owner/name', got {repo!r}")
        self.repo = repo
        self.base_branch = base_branch
        self.branch_prefix = branch_prefix
        self._allow_default_branch_push = allow_push_to_default_branch
        self._protected_paths = list(protected_paths or [])
        self._token_env = token_env
        self._client = client
        self._last_error = ""

    # -- client -----------------------------------------------------------

    @property
    def client(self) -> ResilientClient:
        """Lazily built HTTP client, so constructing a source needs no token."""
        if self._client is None:
            token = resolve_token(self._token_env, source=self.source_id)
            self._client = ResilientClient(
                base_url=_API_ROOT,
                source=self.source_id,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        return self._client

    # -- reads ------------------------------------------------------------

    def list_commits(
        self,
        *,
        since: Optional[str] = None,
        until: Optional[str] = None,
        branch: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Return commits in a time window, newest first."""
        params: Dict[str, Any] = {"per_page": min(limit, 100)}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        params["sha"] = branch or self.base_branch
        raw = self.client.get_json(
            f"/repos/{self.repo}/commits", params=params, default=[]
        )
        return [self._commit_summary(item) for item in (raw or [])][:limit]

    def get_commit(self, sha: str) -> Dict[str, Any]:
        """Return one commit including the files it changed."""
        raw = self.client.get_json(f"/repos/{self.repo}/commits/{sha}", default={})
        summary = self._commit_summary(raw or {})
        summary["files"] = [
            {
                "filename": f.get("filename", ""),
                "status": f.get("status", ""),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
            }
            for f in (raw or {}).get("files", [])
        ]
        return summary

    def list_branches(self, *, limit: int = 50) -> List[str]:
        """Return branch names."""
        raw = self.client.get_json(
            f"/repos/{self.repo}/branches",
            params={"per_page": min(limit, 100)},
            default=[],
        )
        return [item.get("name", "") for item in (raw or [])]

    def list_pull_requests(
        self, *, state: str = "open", limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Return pull requests, newest first."""
        raw = self.client.get_json(
            f"/repos/{self.repo}/pulls",
            params={
                "state": state,
                "per_page": min(limit, 100),
                "sort": "updated",
                "direction": "desc",
            },
            default=[],
        )
        return [
            {
                "number": item.get("number", 0),
                "title": item.get("title", ""),
                "state": item.get("state", ""),
                "head": (item.get("head") or {}).get("ref", ""),
                "base": (item.get("base") or {}).get("ref", ""),
                "url": item.get("html_url", ""),
                "merged_at": item.get("merged_at"),
                "author": (item.get("user") or {}).get("login", ""),
            }
            for item in (raw or [])
        ]

    def list_workflow_runs(
        self, *, status: str = "", limit: int = 20, branch: str = ""
    ) -> List[Dict[str, Any]]:
        """Return recent Actions runs, optionally filtered by status/branch."""
        params: Dict[str, Any] = {"per_page": min(limit, 100)}
        if status:
            params["status"] = status
        if branch:
            params["branch"] = branch
        raw = self.client.get_json(
            f"/repos/{self.repo}/actions/runs", params=params, default={}
        )
        return [
            {
                "id": run.get("id", 0),
                "name": run.get("name", ""),
                "status": run.get("status", ""),
                "conclusion": run.get("conclusion", ""),
                "branch": run.get("head_branch", ""),
                "commit_sha": run.get("head_sha", ""),
                "url": run.get("html_url", ""),
                "created_at": run.get("created_at", ""),
            }
            for run in (raw or {}).get("workflow_runs", [])
        ]

    def get_job_logs(self, run_id: int, *, max_chars: int = 8000) -> str:
        """Return truncated logs for a failed run.

        Logs are attacker-influenceable (a test can print anything), so the
        caller must treat the result as untrusted external content.
        """
        try:
            response = self.client.request(
                "GET",
                f"/repos/{self.repo}/actions/runs/{run_id}/logs",
                expected=(200, 302),
            )
        except (httpx.HTTPError, CircuitOpenError) as exc:
            logger.warning("github: could not fetch logs for run %s (%s)", run_id, exc)
            return ""
        text = response.text
        if len(text) > max_chars:
            return text[:max_chars] + "\n... (truncated)"
        return text

    # -- writes (repair loop only) ----------------------------------------

    def _assert_writable_branch(self, branch: str) -> None:
        """Refuse to write to the base branch unless explicitly unlocked.

        A guard in code, not merely a config value: the check runs on every
        write path regardless of how the caller got here.
        """
        if branch == self.base_branch and not self._allow_default_branch_push:
            raise UnsafeBranchError(
                f"refusing to write to the default branch '{branch}'; "
                "set [reliability.policy] allow_push_to_default_branch = true "
                "to override (not recommended)"
            )

    def branch_name_for(self, incident_id: str) -> str:
        """Return the isolated branch name for an incident."""
        return f"{self.branch_prefix}{incident_id}"

    def create_branch(self, branch: str, *, from_ref: str = "") -> str:
        """Create *branch* from *from_ref* (default: the base branch)."""
        self._assert_writable_branch(branch)
        source_ref = from_ref or self.base_branch
        head = self.client.get_json(
            f"/repos/{self.repo}/git/ref/heads/{source_ref}", default={}
        )
        sha = ((head or {}).get("object") or {}).get("sha", "")
        if not sha:
            raise RuntimeError(f"github: could not resolve '{source_ref}'")
        self.client.request(
            "POST",
            f"/repos/{self.repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": sha},
            expected=(201,),
        )
        logger.info("github: created branch %s from %s", branch, source_ref)
        return sha

    def assert_paths_allowed(self, paths: List[str]) -> None:
        """Raise when any of *paths* is off-limits to automated repair."""
        blocked = [p for p in paths if is_protected_path(p, self._protected_paths)]
        if blocked:
            raise ProtectedPathError(
                "refusing a change touching protected path(s): "
                + ", ".join(sorted(blocked))
            )

    def create_pull_request(
        self,
        *,
        head: str,
        title: str,
        body: str,
        labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Open a pull request from *head* into the base branch.

        JARVIS opens pull requests; it never merges them.  There is
        deliberately no merge method on this class.
        """
        if head == self.base_branch:
            raise UnsafeBranchError(
                "refusing to open a pull request from the base branch"
            )
        response = self.client.request(
            "POST",
            f"/repos/{self.repo}/pulls",
            json={
                "title": title,
                "body": body,
                "head": head,
                "base": self.base_branch,
            },
            expected=(201,),
        )
        payload = response.json()
        number = payload.get("number", 0)
        if labels and number:
            try:
                self.client.request(
                    "POST",
                    f"/repos/{self.repo}/issues/{number}/labels",
                    json={"labels": labels},
                    expected=(200, 201),
                )
            except (httpx.HTTPError, CircuitOpenError):
                logger.warning("github: could not label PR #%s", number)
        return {"number": number, "url": payload.get("html_url", "")}

    # -- signal source contract -------------------------------------------

    def poll(self, *, since: Optional[str] = None) -> List[Signal]:
        """Report failed Actions runs as signals."""
        try:
            runs = self.list_workflow_runs(status="completed", limit=20)
        except (httpx.HTTPError, CircuitOpenError, MissingTokenError) as exc:
            self._last_error = str(exc)
            logger.warning("github: poll failed (%s)", exc)
            return []

        self._last_error = ""
        signals: List[Signal] = []
        for run in runs:
            if run["conclusion"] != "failure":
                continue
            if since and run["created_at"] and run["created_at"] <= since:
                continue
            on_default = run["branch"] == self.base_branch
            signals.append(
                Signal(
                    source=self.source_id,
                    kind="workflow_failed",
                    title=f"Workflow '{run['name']}' failed on {run['branch']}",
                    detail=run["url"],
                    # A red default branch blocks everyone; a red feature branch
                    # is the author's problem.
                    severity=Severity.HIGH if on_default else Severity.MEDIUM,
                    component="ci",
                    external_id=str(run["id"]),
                    occurred_at=run["created_at"] or now_iso(),
                    trust=TrustLevel.EXTERNAL,
                    metadata={
                        "commit_sha": run["commit_sha"],
                        "branch": run["branch"],
                        "workflow": run["name"],
                    },
                )
            )
        return signals

    def health(self) -> SourceHealth:
        """Check that the repository is reachable with the configured token."""
        try:
            self.client.get_json(f"/repos/{self.repo}", default={})
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
    def _commit_summary(raw: Dict[str, Any]) -> Dict[str, Any]:
        commit = raw.get("commit") or {}
        author = commit.get("author") or {}
        return {
            "sha": raw.get("sha", ""),
            "message": (commit.get("message") or "").split("\n")[0][:200],
            "author": author.get("name", ""),
            "date": author.get("date", ""),
            "url": raw.get("html_url", ""),
        }
