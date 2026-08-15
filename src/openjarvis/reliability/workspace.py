"""Isolated repair workspaces.

The coding agent must never be pointed at the checkout a human is using, or at
anything that could be mistaken for production. Phase 12 gives every repair
attempt its own git *worktree*: a separate directory, on its own branch, forked
from a recorded base commit.

    <repo>                     the operator's checkout — never modified
      └── .git/worktrees/…
    <root>/INC-00042/          the agent's sandbox, branch jarvis/incident-INC-00042

Why a worktree rather than a clone: it shares the object database, so creating
one is close to free and needs no network round trip, which matters when the
loop may make three attempts. Why not a bare directory: the agent needs real git
history to diagnose a regression, and JARVIS needs a real diff to audit what
changed.

The base commit is resolved and recorded *before* the agent runs, so the audit
log can say exactly what the repair was based on rather than inferring it later.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from openjarvis.reliability.types import now_iso

logger = logging.getLogger(__name__)

#: Last-resort commit identity, used only when git can resolve none at all.
#: Deliberately not a default: a synthetic author gets deployments refused by
#: hosts that map commit authors to authorized accounts, so it exists to stop a
#: missing setting from losing a repair, not as a normal operating mode.
_FALLBACK_AUTHOR_NAME = "JARVIS"
_FALLBACK_AUTHOR_EMAIL = "jarvis@localhost"

__all__ = [
    "RepairWorkspace",
    "WorkspaceError",
    "Worktree",
    "git_output",
]


class WorkspaceError(RuntimeError):
    """Raised when an isolated workspace cannot be prepared or cleaned up."""


def git_output(
    args: Sequence[str],
    *,
    cwd: str | Path,
    timeout: int = 120,
    check: bool = True,
) -> str:
    """Run a git command and return its stdout.

    Unlike :func:`openjarvis.reliability.code_agent._git`, a failure here is
    raised rather than swallowed: this module's callers are setting up the
    sandbox, and a half-created worktree is worse than none.
    """
    if shutil.which("git") is None:
        raise WorkspaceError("git is not on PATH")
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(f"git {' '.join(args)} timed out") from exc
    except OSError as exc:
        raise WorkspaceError(f"could not run git: {exc}") from exc
    if check and proc.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:500]}"
        )
    return proc.stdout


@dataclass(slots=True)
class Worktree:
    """One isolated checkout, and the facts the audit log needs about it."""

    incident_id: str
    path: str
    branch: str
    base_commit: str
    base_ref: str = ""
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        """Serialize for the audit record."""
        return {
            "incident_id": self.incident_id,
            "path": self.path,
            "branch": self.branch,
            "base_commit": self.base_commit,
            "base_ref": self.base_ref,
            "created_at": self.created_at,
        }

    @property
    def summary(self) -> str:
        """One-line description for evidence and notifications."""
        return (
            f"branch {self.branch} from {self.base_commit[:12]} "
            f"({self.base_ref or 'detached'}) at {self.path}"
        )


@dataclass
class RepairWorkspace:
    """Creates and destroys isolated worktrees for repair attempts.

    Parameters
    ----------
    repo_path:
        The checkout worktrees are cut from. Read-only from JARVIS's point of
        view: nothing here writes to it beyond git's own worktree bookkeeping.
    root:
        Directory the worktrees are created under.
    branch_prefix:
        Matches the GitHub adapter's prefix so the local branch and the remote
        branch have the same name.
    keep_on_failure:
        Leave the directory behind when a repair fails, so a human can inspect
        what the agent actually did. Successful repairs clean up.
    git_identity:
        ``(name, email)`` to author repair commits as, set inside the worktree
        before any commit is made.

        A repair commit is a commit in somebody else's repository, and the
        identity on it is load-bearing well beyond attribution: hosting
        providers decide whether to build a branch based on whether the commit
        author maps to an authorized account.  A synthetic identity gets the
        deployment silently refused, which reads as "the fix did not work"
        rather than "nobody was allowed to build it".

        Left unset, JARVIS does not touch the worktree's identity and git
        resolves it normally from the repository and user configuration.
    """

    repo_path: str
    root: str
    branch_prefix: str = "jarvis/incident-"
    keep_on_failure: bool = True
    git_identity: Optional[tuple[str, str]] = None

    # -- creation ---------------------------------------------------------

    def branch_name_for(self, incident_id: str) -> str:
        """Return the isolated branch name for an incident."""
        return f"{self.branch_prefix}{incident_id}"

    def resolve_commit(self, ref: str) -> str:
        """Return the full SHA *ref* points at."""
        sha = git_output(["rev-parse", ref], cwd=self.repo_path).strip()
        if not sha:
            raise WorkspaceError(f"could not resolve ref {ref!r}")
        return sha

    def create(self, incident_id: str, *, base_ref: str = "HEAD") -> Worktree:
        """Cut a fresh worktree for *incident_id* from *base_ref*.

        The base ref is resolved to an immutable SHA first: branching from
        ``main`` twenty minutes apart can otherwise mean two different trees,
        and the audit log would not be able to say which one a repair was based
        on.
        """
        if not incident_id:
            raise WorkspaceError("an incident id is required")
        repo = Path(self.repo_path)
        if not (repo / ".git").exists() and not (repo / "HEAD").exists():
            raise WorkspaceError(f"{self.repo_path} is not a git repository")

        base_commit = self.resolve_commit(base_ref)
        branch = self.branch_name_for(incident_id)
        path = Path(self.root) / incident_id
        if path.exists():
            # A previous attempt left one behind; reuse would mix two repairs.
            self._remove_path(str(path), branch=branch)
        path.parent.mkdir(parents=True, exist_ok=True)

        # An existing branch of the same name would make `worktree add -b` fail.
        self._delete_branch_if_present(branch)
        git_output(
            ["worktree", "add", "-b", branch, str(path), base_commit],
            cwd=self.repo_path,
        )
        # Before anything can commit — the coding agent has a shell and may
        # commit on its own, so setting this only at commit_all would leave a
        # hole the agent walks straight through.
        self._apply_git_identity(str(path))
        logger.info(
            "prepared repair worktree for %s: %s @ %s",
            incident_id,
            branch,
            base_commit[:12],
        )
        return Worktree(
            incident_id=incident_id,
            path=str(path),
            branch=branch,
            base_commit=base_commit,
            base_ref=base_ref,
        )

    def _apply_git_identity(self, worktree_path: str) -> None:
        """Set the configured author identity inside *worktree_path*.

        Scoped to the repository the worktree belongs to.  Global git
        configuration is never written: JARVIS repairing one target must not
        change how the operator's other commits are authored.
        """
        if not self.git_identity:
            return
        name, email = self.git_identity
        if not name or not email:
            logger.warning(
                "repair git identity is incomplete (name=%r email=%r); "
                "leaving the worktree identity to git",
                name,
                email,
            )
            return
        git_output(["config", "user.name", name], cwd=worktree_path)
        git_output(["config", "user.email", email], cwd=worktree_path)
        logger.info("repair worktree will author commits as %s <%s>", name, email)

    def committer_identity(self, worktree_path: str) -> tuple[str, str]:
        """Return the ``(name, email)`` git would use in *worktree_path*.

        Exposed so the repair loop can assert, before it pushes, that the
        commit it is about to create carries an identity the target will
        accept — rather than discovering it from a refused deployment.
        """
        # check=False: `git config <key>` exits 1 when the key is unset, which
        # is an answer ("git has no identity here"), not a failure.
        name = git_output(
            ["config", "user.name"], cwd=worktree_path, check=False
        ).strip()
        email = git_output(
            ["config", "user.email"], cwd=worktree_path, check=False
        ).strip()
        return name, email

    # -- inspection -------------------------------------------------------

    def changed_files(self, worktree: Worktree) -> List[str]:
        """Return paths modified in *worktree* relative to its base commit.

        Read from git, not from the agent's own account — the account is
        precisely the thing JARVIS does not trust. Tracked modifications and
        untracked new files are both included, because a bug fixed by adding an
        unreferenced file is still a change a human must review.
        """
        out = git_output(
            ["status", "--porcelain=v1", "--untracked-files=all"],
            cwd=worktree.path,
        )
        paths: List[str] = []
        for line in out.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:  # rename: the new path is what exists now
                path = path.split(" -> ", 1)[1]
            paths.append(path.strip('"'))
        return sorted(set(paths))

    def diff_stat(self, worktree: Worktree) -> str:
        """Return a compact diffstat against the base commit."""
        return git_output(
            ["diff", "--stat", worktree.base_commit],
            cwd=worktree.path,
            check=False,
        ).strip()

    def diff(self, worktree: Worktree, *, max_chars: int = 20000) -> str:
        """Return the unified diff against the base commit, truncated."""
        out = git_output(["diff", worktree.base_commit], cwd=worktree.path, check=False)
        if len(out) > max_chars:
            return out[:max_chars] + "\n... (diff truncated)"
        return out

    def line_counts(self, worktree: Worktree) -> tuple[int, int]:
        """Return ``(insertions, deletions)`` against the base commit."""
        out = git_output(
            ["diff", "--numstat", worktree.base_commit],
            cwd=worktree.path,
            check=False,
        )
        added = removed = 0
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            # Binary files report "-", which is not a count.
            if parts[0].isdigit():
                added += int(parts[0])
            if parts[1].isdigit():
                removed += int(parts[1])
        return added, removed

    def has_changes(self, worktree: Worktree) -> bool:
        """Whether anything at all was modified."""
        return bool(self.changed_files(worktree))

    # -- commit -----------------------------------------------------------

    def commit_all(self, worktree: Worktree, message: str) -> str:
        """Stage everything in *worktree* and commit it. Returns the new SHA.

        Committing happens on the incident branch inside the isolated worktree,
        never on the operator's checkout and never on the default branch.
        """
        git_output(["add", "--all"], cwd=worktree.path)
        status = git_output(["status", "--porcelain"], cwd=worktree.path)
        if not status.strip():
            raise WorkspaceError("nothing to commit")

        # The identity comes from the worktree, which create() configured.
        #
        # This used to pass `-c user.name=JARVIS -c user.email=jarvis@localhost`
        # here, which does not merely fail to inherit the repository's identity
        # — `-c` outranks every config level, so it actively overrode an
        # identity the operator had deliberately set. The visible consequence
        # was a Vercel preview silently refused because the commit author
        # mapped to no authorized account, which presents as "the repair did
        # not work" rather than "nobody was allowed to build it".
        #
        # A last-resort identity is still applied when git has none at all,
        # because failing the commit outright would turn a missing setting into
        # a lost repair.
        command = ["commit", "--no-verify", "-m", message]
        name, email = self.committer_identity(worktree.path)
        if not name or not email:
            logger.warning(
                "no git identity in %s; committing with the JARVIS fallback. "
                "Set [reliability.repair] git_author_name/git_author_email so "
                "repair commits carry an identity your host will accept.",
                worktree.path,
            )
            command = [
                "-c",
                f"user.name={_FALLBACK_AUTHOR_NAME}",
                "-c",
                f"user.email={_FALLBACK_AUTHOR_EMAIL}",
                *command,
            ]
        git_output(command, cwd=worktree.path)
        return git_output(["rev-parse", "HEAD"], cwd=worktree.path).strip()

    def push(self, worktree: Worktree, *, remote: str = "origin") -> None:
        """Push the incident branch to *remote*.

        Two guards, both structural rather than configurable:

        * the branch must carry the incident prefix, so this method cannot be
          used to push anything else, whatever the caller passes;
        * ``--force`` is never used, so an existing remote branch cannot be
          overwritten.

        The policy gate on the default branch lives in
        :meth:`SafetyPolicy.may_push_to` and runs before this is reached; the
        prefix check here means a bug that skipped it still cannot push to
        ``main``, because ``main`` does not start with ``jarvis/incident-``.
        """
        if not worktree.branch.startswith(self.branch_prefix):
            raise WorkspaceError(
                f"refusing to push '{worktree.branch}': not an incident branch "
                f"(expected the prefix '{self.branch_prefix}')"
            )
        git_output(
            ["push", "--set-upstream", remote, worktree.branch],
            cwd=worktree.path,
        )
        logger.info("pushed %s to %s", worktree.branch, remote)

    # -- teardown ---------------------------------------------------------

    def remove(self, worktree: Worktree, *, succeeded: bool = True) -> None:
        """Tear down *worktree*, unless a failure is worth preserving."""
        if not succeeded and self.keep_on_failure:
            logger.info(
                "keeping worktree %s for inspection (repair did not succeed)",
                worktree.path,
            )
            return
        self._remove_path(worktree.path, branch=worktree.branch)

    def _remove_path(self, path: str, *, branch: str = "") -> None:
        """Remove a worktree directory and git's record of it."""
        try:
            git_output(
                ["worktree", "remove", "--force", path],
                cwd=self.repo_path,
                check=False,
            )
        except WorkspaceError:  # pragma: no cover - defensive
            logger.exception("could not remove worktree %s", path)
        # `worktree remove` leaves the directory behind if it was never
        # registered (e.g. a stale directory from a killed process).
        if Path(path).exists():
            shutil.rmtree(path, ignore_errors=True)
        git_output(["worktree", "prune"], cwd=self.repo_path, check=False)
        if branch:
            self._delete_branch_if_present(branch)

    def _delete_branch_if_present(self, branch: str) -> None:
        """Delete a local branch, ignoring the case where it does not exist."""
        git_output(["branch", "-D", branch], cwd=self.repo_path, check=False)

    # -- convenience ------------------------------------------------------

    def cleanup_all(self) -> None:
        """Prune every worktree under :attr:`root`.

        Used by the CLI and by tests; a crashed process can otherwise leave
        directories behind that make the next `worktree add` fail.
        """
        root = Path(self.root)
        if not root.is_dir():
            return
        for child in sorted(root.iterdir()):
            if child.is_dir():
                self._remove_path(str(child), branch=self.branch_name_for(child.name))


def find_repository_root(path: str) -> Optional[str]:
    """Return the git repository root containing *path*, if any."""
    try:
        out = git_output(["rev-parse", "--show-toplevel"], cwd=path, check=False)
    except WorkspaceError:
        return None
    return out.strip() or None
