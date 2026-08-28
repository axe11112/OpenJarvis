"""GitHub source — commits, pull requests, Actions, and repair branches.

Read operations are used for correlation: given a failure at time T, which
commits landed just before it and what did they touch?

Write operations exist only for the repair loop and are deliberately narrow.
There is no method here that can push to the default branch, force-push, or
modify a workflow file — those are refused structurally rather than by policy,
so a bug or an injected instruction cannot reach them.
See ``docs/JARVIS_SECURITY.md`` §4.

:meth:`GitHubSource.merge_pull_request` is the one exception and the only way
code here can reach the default branch. It is still structurally narrow — it
cannot create a pull request to merge, cannot push, and refuses any commit other
than the exact SHA the caller names — but it is real authority, and it is off by
default. The gates that decide whether to call it live in
:mod:`openjarvis.reliability.merge`, not here: this class knows how to merge one
named commit and nothing about whether it should.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

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

__all__ = [
    "MERGE_METHODS",
    "GitHubSource",
    "ProtectedPathError",
    "UnsafeBranchError",
    "UnsafeMergeError",
]

_API_ROOT = "https://api.github.com"

#: Merge verbs this class will send. An allowlist rather than a passthrough:
#: the merge method is read from configuration, and configuration is not a
#: trust boundary.
MERGE_METHODS = frozenset({"squash", "merge", "rebase"})

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


class UnsafeMergeError(RuntimeError):
    """Raised when a merge is attempted without the guarantees that make it safe."""


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
        actions_token_env: str = "",
        monitor_actions: bool = True,
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
        self._actions_token_env = actions_token_env or token_env
        self._monitor_actions = monitor_actions
        self._client = client
        self._actions_client: Optional[ResilientClient] = None
        self._last_error = ""

    @property
    def monitor_actions(self) -> bool:
        """Whether Actions is part of this target's health picture.

        Read by the diagnostic so it can say "not monitored" rather than
        guessing from an empty result — an empty run list means the same thing
        whether the repository has no workflows or the token cannot see them,
        and those two need very different answers.
        """
        return self._monitor_actions

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

    @property
    def actions_client(self) -> ResilientClient:
        """Client for Actions reads, which may use a different token.

        Falls back to the main client when no separate variable is configured,
        so the common case costs nothing.
        """
        if self._actions_token_env == self._token_env:
            return self.client
        if self._actions_client is None:
            token = resolve_token(self._actions_token_env, source=self.source_id)
            self._actions_client = ResilientClient(
                base_url=_API_ROOT,
                source=self.source_id,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        return self._actions_client

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

    def permissions(self) -> Dict[str, bool]:
        """Return what this token may do to the repository.

        Answered by reading the repository object, which reports the
        authenticated token's effective permissions. Deliberately not answered
        by attempting a write: a capability probe that creates a branch to find
        out whether it can create branches is not a probe, it is a write.

        Keys are GitHub's own: ``pull`` (read), ``push`` (write), ``admin``.
        An empty dict means the API did not say — reported as UNKNOWN upstream,
        never as "no permission".
        """
        raw = self.client.get_json(f"/repos/{self.repo}", default={}) or {}
        permissions = raw.get("permissions")
        if not isinstance(permissions, dict):
            return {}
        return {k: bool(v) for k, v in permissions.items()}

    def can_write(self) -> bool:
        """Whether the token could create a branch and open a pull request.

        JARVIS needs this only once automated repair is enabled; every earlier
        phase is strictly read-only, and the diagnostic reports write access as
        NOT_CONFIGURED rather than missing when repair is off.
        """
        return bool(self.permissions().get("push"))

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
        """Return recent Actions runs, optionally filtered by status/branch.

        Returns an empty list *without contacting the API* when Actions is not
        monitored for this target.  The guard lives here rather than only at
        the call sites so that a new caller cannot reintroduce the API traffic
        by forgetting to check — the operator said this repository has no CI,
        and that has to hold everywhere.
        """
        if not self._monitor_actions:
            return []
        params: Dict[str, Any] = {"per_page": min(limit, 100)}
        if status:
            params["status"] = status
        if branch:
            params["branch"] = branch
        raw = self.actions_client.get_json(
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

        Empty, with no API call, when Actions is not monitored: there is no run
        whose logs could be wanted.
        """
        if not self._monitor_actions:
            return ""
        try:
            response = self.actions_client.request(
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

    def branch_head_sha(self, branch: str) -> str:
        """The commit *branch* currently points to on the remote, or ``""``.

        Read-only counterpart to the ref lookup :meth:`create_branch` already
        does before branching — pulled out so a caller can ask "has this
        branch moved?" without creating anything.
        """
        ref = self.client.get_json(
            f"/repos/{self.repo}/git/ref/heads/{branch}", default={}
        )
        return str(((ref or {}).get("object") or {}).get("sha", ""))

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

    def get_pull_request(self, number: int) -> Dict[str, Any]:
        """Read one pull request's current facts.

        Returns the fields a merge decision needs and nothing else. ``head_sha``
        is read fresh on every call by design: it is the value the whole
        time-of-check/time-of-use argument turns on, and a cached one is worse
        than none.

        ``mergeable`` is GitHub's tri-state and is passed through as-is —
        ``None`` means "still computing", which is not the same as "yes" and
        must not be rounded up to one.
        """
        raw = self.client.get_json(f"/repos/{self.repo}/pulls/{number}", default={})
        if not raw:
            raise RuntimeError(f"github: pull request #{number} could not be read")
        head = raw.get("head") or {}
        base = raw.get("base") or {}
        return {
            "number": raw.get("number", 0),
            "title": raw.get("title", ""),
            "state": raw.get("state", ""),
            "draft": bool(raw.get("draft", False)),
            "head_ref": head.get("ref", ""),
            "head_sha": head.get("sha", ""),
            "base_ref": base.get("ref", ""),
            "base_sha": base.get("sha", ""),
            "mergeable": raw.get("mergeable"),
            "mergeable_state": raw.get("mergeable_state", ""),
            "merged": bool(raw.get("merged", False)),
            "author": (raw.get("user") or {}).get("login", ""),
            "url": raw.get("html_url", ""),
        }

    def combined_status(
        self,
        sha: str,
        *,
        required_contexts: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Report CI's verdict on *sha* from both status APIs.

        GitHub has two, and a repository can use either: the legacy combined
        *status* API that third-party CI posts to, and the *check-runs* API that
        Actions uses. Reading only one reports "no CI" for half the world, so
        both are read and the pessimistic answer wins.

        ``state`` is ``"success"``, ``"failure"``, ``"pending"``, ``"none"`` or
        ``"unreadable"``. The last two are distinct from each other and from
        ``"success"``, and keeping them apart is the whole point:

        ``"none"``
            Nothing was reported. "No CI ran" is not "CI passed".
        ``"unreadable"``
            The credential is not permitted to look. A fine-grained token
            without *Commit statuses: Read* and *Checks: Read* gets 403 on both
            endpoints below.

        Collapsing ``unreadable`` into ``none`` — or letting the 403 escape as
        an exception the caller reports as "could not read the pull request" —
        turns a blind spot into an observation and sends whoever is debugging it
        looking for absent CI instead of an absent permission. Found against a
        real repository whose Vercel status was green the entire time and simply
        could not be seen. ``missing_permissions`` names what to grant.

        Naming *required_contexts* changes the question being asked. Without
        them the verdict is "is anything, anywhere, not green", which no
        credential can answer while half the evidence is behind a 403. With
        them it is "did *these named* contexts report success on this commit" —
        a question the Commit Statuses API answers by itself, so a Checks API
        that refuses to open is reported as unavailable rather than treated as
        a blind spot that poisons the verdict. It is never treated as consent:
        the named contexts must be found and green on their own evidence, and a
        Commit Statuses API that cannot be read is still ``"unreadable"``,
        because that is the API the answer now rests on.

        This is what makes the gate usable by a GitHub fine-grained token, which
        has no Checks permission to grant at all.

        Extra keys when *required_contexts* is given: ``required`` maps each
        named context to what was observed (its state, ``"missing"`` or
        ``"unreadable"``), ``missing_required`` lists those not found. ``sha``
        always echoes the commit the verdict describes, so a caller can prove
        the answer is about the commit it asked about.
        """
        required = [str(c) for c in (required_contexts or []) if str(c).strip()]
        states: List[str] = []
        contexts: List[str] = []
        denied: List[str] = []
        #: context name -> worst state seen for it, so a context reported twice
        #: cannot be rescued by its better half.
        observed: Dict[str, str] = {}
        readable = {"statuses": True, "checks": True}

        def _worst(current: str, incoming: str) -> str:
            rank = {"success": 0, "pending": 1, "error": 2, "failure": 2}
            return (
                incoming if rank.get(incoming, 2) >= rank.get(current, 2) else current
            )

        def _observe(context: str, state: str) -> None:
            contexts.append(context)
            states.append(state)
            observed[context] = (
                _worst(observed[context], state) if context in observed else state
            )

        def _read(
            path: str, permission: str, which: str, **params: Any
        ) -> Dict[str, Any]:
            """Read *path*, recording a permission denial rather than raising."""
            try:
                return (
                    self.client.get_json(path, params=params or None, default={}) or {}
                )
            except (httpx.HTTPStatusError, CircuitOpenError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", 0)
                # 404 is included on purpose: GitHub returns it instead of 403
                # for resources a token may not even know exist.
                if status in (401, 403, 404):
                    denied.append(permission)
                    readable[which] = False
                    logger.warning(
                        "github: cannot read %s (HTTP %s) — the token is missing "
                        "the '%s' permission",
                        path,
                        status,
                        permission,
                    )
                    return {}
                raise

        combined = _read(
            f"/repos/{self.repo}/commits/{sha}/status",
            "Commit statuses: Read",
            "statuses",
        )
        # GitHub echoes the commit it answered about. If that is not the commit
        # asked about, the answer is evidence about someone else's code — treat
        # it as no answer at all rather than as a verdict.
        echoed = str(combined.get("sha") or "")
        if echoed and echoed != sha:
            readable["statuses"] = False
            denied.append(f"commit status answered for {echoed[:12]}, not {sha[:12]}")
            combined = {}
        for item in combined.get("statuses") or []:
            _observe(str(item.get("context", "")), str(item.get("state", "")).lower())

        runs = _read(
            f"/repos/{self.repo}/commits/{sha}/check-runs",
            "Checks: Read",
            "checks",
            per_page=100,
        )
        for run in runs.get("check_runs") or []:
            if str(run.get("status", "")).lower() != "completed":
                _observe(str(run.get("name", "")), "pending")
                continue
            conclusion = str(run.get("conclusion", "")).lower()
            if conclusion in ("success", "neutral", "skipped"):
                _observe(str(run.get("name", "")), "success")
            elif conclusion in ("cancelled", "timed_out", "action_required"):
                _observe(str(run.get("name", "")), "pending")
            else:
                _observe(str(run.get("name", "")), "failure")

        red = any(s in ("failure", "error") for s in states)
        result: Dict[str, Any] = {
            "state": "",
            "contexts": contexts,
            "count": len(states),
            "missing_permissions": sorted(set(denied)),
            "sha": sha,
            "required_configured": bool(required),
            "statuses_api": "readable" if readable["statuses"] else "unavailable",
            "checks_api": "readable" if readable["checks"] else "unavailable",
        }

        if not required:
            if red:
                # A denial does not soften an observed failure: something red was
                # seen, and that is enough to refuse whatever else was invisible.
                state = "failure"
            elif denied:
                # Checked before "none" on purpose. With one endpoint forbidden
                # and the other empty, "nothing reported" would be a guess
                # dressed as an observation — the forbidden half is exactly
                # where the evidence would have been.
                state = "unreadable"
            elif not states:
                state = "none"
            elif any(s == "pending" for s in states):
                state = "pending"
            else:
                state = "success"
            result["state"] = state
            return result

        # -- the named-contexts contract --------------------------------------
        per_context: Dict[str, str] = {}
        for name in required:
            if name in observed:
                per_context[name] = observed[name]
            elif not readable["statuses"] or not readable["checks"]:
                # Absent from what could be read, with something unread. Absence
                # is only evidence when everything was legible; here the context
                # may be sitting in the half that refused to open.
                per_context[name] = "unreadable"
            else:
                per_context[name] = "missing"

        values = list(per_context.values())
        if red:
            # Still pessimistic about anything observed red, required or not.
            # A named contract narrows what must be green; it does not license
            # ignoring a failure that was seen.
            state = "failure"
        elif any(v == "unreadable" for v in values):
            state = "unreadable"
        elif any(v == "missing" for v in values):
            state = "none"
        elif any(v == "pending" for v in values):
            state = "pending"
        elif all(v == "success" for v in values):
            state = "success"
        else:
            # An unrecognised state word is not a green light.
            state = "failure"

        result["state"] = state
        result["required"] = per_context
        result["missing_required"] = sorted(
            name for name, v in per_context.items() if v in ("missing", "unreadable")
        )
        return result

    def merge_pull_request(
        self,
        *,
        number: int,
        expected_head_sha: str,
        method: str = "squash",
        title: str = "",
        message: str = "",
    ) -> Dict[str, Any]:
        """Merge a pull request, and only at *expected_head_sha*.

        The narrowest write in this class, and the only one that can put code on
        the default branch. Three properties make it narrow:

        * ``expected_head_sha`` is mandatory and is sent to GitHub as ``sha``.
          GitHub then refuses the merge with 409 if the head has moved since the
          caller read it. That is what closes the last time-of-check/
          time-of-use window: the check and the merge are the same call, decided
          by the server, so no amount of racing between JARVIS's read and its
          write can land a different commit than the one that was verified.
        * ``method`` is validated against a three-name allowlist rather than
          passed through, so a corrupted config cannot invent a merge verb.
        * There is still no way to reach the default branch except through an
          existing reviewed pull request. This method cannot create one, cannot
          push, and cannot force anything.

        Callers must not use this directly for automatic merges — go through
        :class:`~openjarvis.reliability.merge.AutoMerger`, which owns the gates.
        """
        if method not in MERGE_METHODS:
            raise UnsafeMergeError(
                f"refusing merge method '{method}'; "
                f"permitted: {', '.join(sorted(MERGE_METHODS))}"
            )
        if not expected_head_sha:
            raise UnsafeMergeError(
                "refusing to merge without an expected head SHA: the merge would "
                "land whatever happens to be on the branch at the time"
            )
        if not number:
            raise UnsafeMergeError("refusing to merge without a pull request number")

        payload: Dict[str, Any] = {
            "merge_method": method,
            "sha": expected_head_sha,
        }
        if title:
            payload["commit_title"] = title
        if message:
            payload["commit_message"] = message

        response = self.client.request(
            "PUT",
            f"/repos/{self.repo}/pulls/{number}/merge",
            json=payload,
            expected=(200,),
        )
        body = response.json()
        merged = bool(body.get("merged", False))
        logger.info(
            "github: merge of PR #%s at %s -> merged=%s",
            number,
            expected_head_sha[:12],
            merged,
        )
        return {
            "merged": merged,
            "sha": body.get("sha", ""),
            "message": body.get("message", ""),
        }

    # -- signal source contract -------------------------------------------

    def poll(self, *, since: Optional[str] = None) -> List[Signal]:
        """Report failed Actions runs as signals.

        Yields nothing when Actions is not monitored — ``list_workflow_runs``
        returns empty without an API call, so a repository whose last workflow
        failed months before Actions was switched off cannot manufacture a
        fresh incident today.
        """
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
