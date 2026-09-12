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
import os
import shutil
import socket
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
    "MergeOutcome",
    "RepairWorkspace",
    "WorkspaceError",
    "Worktree",
    "git_output",
    "is_ancestor",
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


def is_ancestor(candidate: str, descendant: str, *, cwd: str | Path) -> bool:
    """Whether *candidate* is reachable from *descendant* by walking parents.

    ``git merge-base --is-ancestor`` communicates its answer purely through
    the exit code (0 = yes, 1 = no), which :func:`git_output` — built to
    raise on a non-zero exit — cannot expose. A separate, small function
    rather than a ``raise_on_nonzero`` flag on that one: "is X an ancestor
    of Y" is a question with a real false answer, not a failure.
    """
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", candidate, descendant],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.returncode == 0


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


@dataclass(slots=True)
class MergeOutcome:
    """What happened trying to reconcile a worktree with a moved base."""

    merged: bool
    new_sha: str = ""
    conflicting_files: List[str] = field(default_factory=list)
    error: str = ""


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

    def checkout_existing(self, incident_id: str, *, branch: str) -> Worktree:
        """Check out an *existing*, already-pushed branch fresh into a
        worktree — fetched from origin first, never created new.

        Unlike :meth:`create`, which always cuts a brand new branch from a
        base ref: this reconnects to real work that already has a remote
        history of its own, for a case ``create`` was never meant to
        handle -- re-verifying a feature whose own worktree was already
        torn down (routine cleanup once it reached READY, see
        :meth:`~openjarvis.wiz.features.pipeline.FeaturePipeline.
        _cleanup_worktree`) against a base branch that has since moved.
        Reusing ``create`` here would silently start a *different* branch
        or destroy the remote history this call exists to reconnect to.

        ``-B`` (not ``-b``): the local branch of this name is very likely
        already gone (:meth:`remove` deletes it on teardown), but if a
        stale one somehow remains this resets it to match origin rather
        than refusing — the worktree is always freshly checked out from
        ``origin/<branch>`` regardless of anything a prior local branch
        pointed at.
        """
        if not branch:
            raise WorkspaceError("a branch name is required")
        repo = Path(self.repo_path)
        if not (repo / ".git").exists() and not (repo / "HEAD").exists():
            raise WorkspaceError(f"{self.repo_path} is not a git repository")

        path = Path(self.root) / incident_id
        if path.exists():
            self._remove_path(str(path))
        path.parent.mkdir(parents=True, exist_ok=True)

        git_output(["fetch", "origin", branch], cwd=self.repo_path)
        git_output(
            ["worktree", "add", "-B", branch, str(path), f"origin/{branch}"],
            cwd=self.repo_path,
        )
        git_output(
            ["branch", f"--set-upstream-to=origin/{branch}", branch],
            cwd=str(path),
        )
        self._apply_git_identity(str(path))
        base_commit = self.resolve_commit(f"origin/{branch}")
        logger.info(
            "checked out existing branch for %s: %s @ %s",
            incident_id,
            branch,
            base_commit[:12],
        )
        return Worktree(
            incident_id=incident_id,
            path=str(path),
            branch=branch,
            base_commit=base_commit,
            base_ref=f"origin/{branch}",
        )

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
        # Drop registrations whose directories are already gone, before
        # anything below tries to reuse their branch. A directory removed out
        # from under its registration -- a killed process, a cleaned temp
        # directory, an operator tidying up -- leaves the branch counting as
        # checked out somewhere, so `branch -D` fails and `worktree add -b`
        # fails, and the incident can never be repaired again. FeatureWorkspace
        # has pruned first for exactly this reason since aa778c09; this side
        # never did, and failed the same way.
        self.prune_stale_worktrees()
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
        # Claimed for this process, so a second watcher or a hand-run
        # `jarvis reliability repair` that reaches the same incident cannot
        # delete the tree this one is writing in. _remove_path reads this and
        # refuses; git itself refuses a plain `worktree remove`.
        self._lock_worktree(str(path), incident_id)
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

    def merge_base(self, worktree: Worktree, *, base_branch: str) -> MergeOutcome:
        """Merge *base_branch* (freshly fetched from origin) into
        *worktree*'s own branch, in place.

        A real ``git merge --no-edit``, never a rebase and never
        ``--force`` anything: the branch's existing commit is preserved
        exactly as it was, and success produces one new merge commit whose
        ancestry includes both. A conflict is detected and the merge is
        aborted immediately, leaving the worktree exactly as clean as it
        was before this was called -- never left half-resolved, and never
        handed to a coding session to resolve automatically. The caller
        decides what a feature that cannot be mechanically reconciled
        means; this only ever reports the fact.
        """
        git_output(["fetch", "origin", base_branch], cwd=worktree.path)
        proc = subprocess.run(
            ["git", "merge", "--no-edit", f"origin/{base_branch}"],
            cwd=worktree.path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode == 0:
            new_sha = git_output(["rev-parse", "HEAD"], cwd=worktree.path).strip()
            return MergeOutcome(merged=True, new_sha=new_sha)

        conflicting = [
            line
            for line in git_output(
                ["diff", "--name-only", "--diff-filter=U"],
                cwd=worktree.path,
                check=False,
            ).splitlines()
            if line.strip()
        ]
        git_output(["merge", "--abort"], cwd=worktree.path, check=False)
        return MergeOutcome(
            merged=False,
            conflicting_files=conflicting,
            error=(proc.stdout + proc.stderr).strip()[:2000],
        )

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

    def head_sha(self, worktree: Worktree) -> str:
        """*worktree*'s current commit, without committing anything.

        For a caller that needs to know what would be re-deployed when
        :meth:`has_changes` already says there is nothing new to commit —
        re-verifying an attempt whose diff a previous run already committed
        and pushed, for instance, rather than trying (and failing) to commit
        it again.
        """
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

    #: Marks a worktree as belonging to one live repair, in git's own lock
    #: reason so any process can read it. Worktree ownership was previously not
    #: established across processes at all: the path is derived from the
    #: incident id, so a second watcher (or a hand-run
    #: `jarvis reliability repair`) picking up the same incident went straight
    #: into ``create()``, found the directory, and force-deleted the tree the
    #: first one was actively writing in -- a live Claude session's work, gone,
    #: with nothing recorded anywhere.
    _LOCK_PREFIX = "openjarvis-repair"

    def _lock_reason(self, incident_id: str) -> str:
        return (
            f"{self._LOCK_PREFIX} pid={os.getpid()} host={socket.gethostname()} "
            f"incident={incident_id}"
        )

    def _lock_worktree(self, path: str, incident_id: str) -> None:
        """Claim this worktree for this process, for as long as it is in use."""
        git_output(
            ["worktree", "lock", path, "--reason", self._lock_reason(incident_id)],
            cwd=self.repo_path,
            check=False,
        )

    def _registered_worktrees(self) -> List[tuple]:
        """Every worktree git has a record of, as ``(path, lock_reason_or_None)``."""
        try:
            listing = git_output(
                ["worktree", "list", "--porcelain"], cwd=self.repo_path, check=False
            )
        except WorkspaceError:  # pragma: no cover - defensive
            return []
        entries: List[tuple] = []
        current: Optional[str] = None
        locked: Optional[str] = None
        for line in listing.splitlines():
            if line.startswith("worktree "):
                if current is not None:
                    entries.append((current, locked))
                current = line[len("worktree ") :].strip()
                locked = None
            elif line.startswith("locked") and current is not None:
                locked = line[len("locked") :].strip()
        if current is not None:
            entries.append((current, locked))
        return entries

    def prune_stale_worktrees(self) -> None:
        """Drop git's record of worktrees whose directories are gone.

        ``git worktree prune`` silently skips a *locked* worktree -- it exits 0
        and removes nothing. Since :meth:`create` locks every worktree it makes
        (see :attr:`_LOCK_PREFIX`), a plain prune stopped working the moment
        ownership was introduced: a directory removed out from under a
        registration left the lock behind, the branch went on counting as
        checked out somewhere, ``branch -D`` failed, ``worktree add -b`` failed,
        and the feature became unretryable with a git error an operator cannot
        act on. That is precisely the bug pruning was added to prevent.

        So the lock is released first, but only for a registration whose
        directory no longer exists. That is the one case where the owner's
        identity does not matter at all: a worktree with no directory cannot be
        protecting anybody's work, whoever claimed it. A registration whose
        directory is still there is left completely alone, lock and all --
        that may be a live repair in another process.
        """
        for path, locked in self._registered_worktrees():
            if locked is None:
                continue
            if Path(path).exists():
                continue  # someone may be working in it; never touch the lock
            git_output(
                ["worktree", "unlock", path], cwd=self.repo_path, check=False
            )
        git_output(["worktree", "prune"], cwd=self.repo_path, check=False)

    def _lock_holder(self, path: str) -> Optional[str]:
        """The lock reason on *path*, or ``None`` when it is not locked.

        Read from ``git worktree list --porcelain``, which reports a ``locked``
        line carrying the reason verbatim.
        """
        try:
            listing = git_output(
                ["worktree", "list", "--porcelain"], cwd=self.repo_path, check=False
            )
        except WorkspaceError:  # pragma: no cover - defensive
            return None
        target = str(Path(path))
        current: Optional[str] = None
        for line in listing.splitlines():
            if line.startswith("worktree "):
                current = str(Path(line[len("worktree ") :].strip()))
            elif line.startswith("locked") and current == target:
                reason = line[len("locked") :].strip()
                return reason or ""
        return None

    def _may_break_lock(self, reason: str) -> bool:
        """Whether a lock this process did not take is safe to break.

        Only one case is: a lock this module took, naming a process on *this*
        host that is no longer alive. That is a crashed repair, and refusing to
        clean up after it forever would leave the incident permanently
        unrepairable.

        Anything else is left alone. A lock from another host cannot be checked
        for liveness from here, and a lock nobody here wrote is not this
        module's to break.
        """
        if not reason.startswith(self._LOCK_PREFIX):
            return False
        fields = dict(
            part.split("=", 1)
            for part in reason.split()
            if "=" in part
        )
        if fields.get("host") != socket.gethostname():
            return False
        try:
            pid = int(fields.get("pid", ""))
        except ValueError:
            return False
        if pid == os.getpid():
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True  # the owner is gone; its lock is not protecting anything
        except PermissionError:
            return False  # alive, just not ours to signal
        except OSError:  # pragma: no cover - defensive
            return False
        return False

    #: The one git refusal that means "there is nothing here I am tracking",
    #: and therefore the one that makes deleting the directory outright safe.
    #: Matched on git's own wording for `worktree remove` against a path it
    #: does not know about: ``fatal: '<path>' is not a working tree``.
    _NOT_A_WORKTREE = "is not a working tree"

    def _remove_path(self, path: str, *, branch: str = "") -> None:
        """Remove a worktree directory and git's record of it, or refuse to.

        Fails closed. It used to run ``worktree remove --force`` with
        ``check=False`` -- discarding whether git had agreed -- and then delete
        the directory with ``shutil.rmtree(ignore_errors=True)`` regardless. So
        every reason git can have for refusing was overridden by a recursive
        delete, including ``git worktree lock``, which exists for the sole
        purpose of saying "do not remove this" and which ``--force`` alone is
        specifically documented not to override.

        The rmtree is still needed for the case its comment named: a directory
        left by a killed process that git never registered, which
        ``worktree remove`` declines to touch. That case is now identified from
        git's own answer instead of assumed, so a worktree git is protecting is
        left alone and reported rather than deleted. This subsystem has
        destroyed real work twice before; unexplained is not the same as
        unwanted.
        """
        holder = self._lock_holder(path)
        if holder is not None:
            if not self._may_break_lock(holder):
                logger.error(
                    "refusing to remove worktree %s: it is locked by another "
                    "live repair (%s). Left in place.",
                    path,
                    holder or "no reason recorded",
                )
                return
            # Ours, or a crashed owner on this host. Release our own claim so
            # the removal below can proceed.
            git_output(
                ["worktree", "unlock", path], cwd=self.repo_path, check=False
            )

        removed = False
        refusal = ""
        try:
            git_output(["worktree", "remove", "--force", path], cwd=self.repo_path)
            removed = True
        except WorkspaceError as exc:
            refusal = str(exc)

        if Path(path).exists():
            if removed or self._NOT_A_WORKTREE in refusal:
                # Either git removed its record and left the directory, or git
                # never had a record of it. Nothing is protecting this.
                shutil.rmtree(path, ignore_errors=True)
            else:
                # git refused for a reason of its own -- a lock, most likely.
                # Leaving a directory behind costs disk. Deleting one git is
                # protecting costs whatever was in it.
                logger.error(
                    "refusing to delete worktree %s: git would not remove it "
                    "(%s). Left in place; unlock it or remove it by hand.",
                    path,
                    refusal or "no reason given",
                )
                git_output(["worktree", "prune"], cwd=self.repo_path, check=False)
                return

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
        # And the registrations whose directories are already gone, which no
        # loop over existing directories can reach.
        self.prune_stale_worktrees()


def find_repository_root(path: str) -> Optional[str]:
    """Return the git repository root containing *path*, if any."""
    try:
        out = git_output(["rev-parse", "--show-toplevel"], cwd=path, check=False)
    except WorkspaceError:
        return None
    return out.strip() or None
