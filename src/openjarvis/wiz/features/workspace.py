"""Isolated worktrees for feature builds.

Composed from :class:`openjarvis.reliability.workspace.RepairWorkspace` rather
than reimplemented. That class already resolves the base ref to an immutable SHA
before branching, sets the approved git identity *before* the agent gets a shell,
and cleans up on success while keeping failures for inspection. A second
implementation would be a second set of those guarantees, and the second one is
always the one that turns out to be missing a step.

What this adds is the part specific to product work: a branch name that reads
like a feature rather than an incident, and a refusal to hand a coding agent a
directory it must never be allowed to edit.

That refusal is the important half. §4 of the brief: never let Claude edit the
live Wize checkout, the live OpenJarvis checkout, or another task's worktree.
:meth:`FeatureWorkspace.create` checks the resolved path against those before it
returns, so an agent cannot be pointed at the running system by a misconfigured
root — which is the way it would actually happen, rather than by anybody
deciding to.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from openjarvis.reliability.workspace import RepairWorkspace, WorkspaceError, Worktree

logger = logging.getLogger(__name__)

__all__ = ["FeatureWorkspace", "UnsafeWorkspace", "branch_slug"]

#: Feature branches are named so that a human scanning `git branch` can tell
#: what they are without looking anything up.
FEATURE_BRANCH_PREFIX = "wiz/feature/"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class UnsafeWorkspace(WorkspaceError):
    """The requested workspace is somewhere a coding agent may not write."""


def branch_slug(title: str, *, max_words: int = 5) -> str:
    """A short, git-safe fragment derived from a feature's title."""
    words = [w for w in _SLUG_STRIP.sub("-", (title or "").lower()).split("-") if w]
    return "-".join(words[:max_words])


@dataclass
class FeatureWorkspace:
    """Creates the worktree a feature is built in.

    Parameters
    ----------
    repo_path:
        The checkout feature branches are cut from.
    root:
        Directory feature worktrees are created under. Must be outside every
        protected checkout.
    protected_checkouts:
        Directories a coding agent must never be given. The live application
        checkout and OpenJarvis's own live checkout belong here.
    git_identity:
        ``(name, email)`` feature commits are authored as. The same reasoning as
        for repairs applies: hosting providers decide whether to build a branch
        based on whether the author maps to an authorised account, so a
        synthetic identity reads as "the feature did not work" rather than
        "nobody was allowed to build it".
    """

    repo_path: str
    root: str
    protected_checkouts: List[str] = field(default_factory=list)
    git_identity: Optional[Tuple[str, str]] = None
    keep_on_failure: bool = True

    def __post_init__(self) -> None:
        self._inner = RepairWorkspace(
            repo_path=self.repo_path,
            root=self.root,
            branch_prefix=FEATURE_BRANCH_PREFIX,
            keep_on_failure=self.keep_on_failure,
            git_identity=self.git_identity,
        )

    # -- naming ------------------------------------------------------------

    @staticmethod
    def workspace_name(feature_id: str, title: str = "") -> str:
        """``FEAT-00001-add-coach-dashboard``."""
        slug = branch_slug(title)
        return f"{feature_id}-{slug}" if slug else feature_id

    def branch_name_for(self, feature_id: str, title: str = "") -> str:
        return f"{FEATURE_BRANCH_PREFIX}{self.workspace_name(feature_id, title)}"

    # -- safety ------------------------------------------------------------

    def _protected(self) -> List[Path]:
        paths = [Path(p).expanduser().resolve() for p in self.protected_checkouts]
        # The source checkout is protected whether or not it was listed: cutting
        # a worktree from a repository is fine, editing that repository is not.
        paths.append(Path(self.repo_path).expanduser().resolve())
        return paths

    def _assert_safe(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        for protected in self._protected():
            if resolved == protected:
                raise UnsafeWorkspace(
                    f"{resolved} is a live checkout and may never be built in"
                )
            if protected in resolved.parents:
                raise UnsafeWorkspace(
                    f"{resolved} is inside the live checkout {protected}; "
                    "feature worktrees must live outside it"
                )

    def check_root(self) -> None:
        """Verify the configured root before anything is created in it."""
        self._assert_safe(Path(self.root))

    # -- lifecycle ---------------------------------------------------------

    def create(
        self, feature_id: str, *, title: str = "", base_ref: str = "HEAD"
    ) -> Worktree:
        """Cut an isolated worktree for *feature_id*."""
        if not feature_id:
            raise WorkspaceError("a feature id is required")
        self.check_root()

        name = self.workspace_name(feature_id, title)
        target = Path(self.root) / name
        self._assert_safe(target)

        self._forget_stale_worktrees()
        worktree = self._inner.create(name, base_ref=base_ref)

        # Re-checked after the fact: the value that matters is the path the
        # agent is actually handed, not the one that was requested.
        self._assert_safe(Path(worktree.path))
        logger.info(
            "prepared feature worktree for %s: %s @ %s",
            feature_id,
            worktree.branch,
            worktree.base_commit[:12],
        )
        return worktree

    def reuse(
        self, feature_id: str, *, path: str, branch: str, base_sha: str
    ) -> Optional[Worktree]:
        """Hand back *path* as-is if it is genuinely still that worktree.

        Never calls :meth:`create` — found on FEAT-00031's recovery, where a
        one-off process with a cold worktree cache called ``create()`` for a
        feature that already had a live worktree with an uncommitted diff on
        disk, and ``create()`` is documented and correct to remove whatever
        is already at that path first ("a previous attempt left one behind;
        reuse would mix two repairs" — true for a fresh attempt, false for
        resuming an existing one). A feature resuming after
        ``reopen_for_deploy`` is exactly the case that distinction misses:
        there was no previous, abandoned attempt to clean up, only this
        process's own memory of it being cold.

        ``None`` when *path* is not a real, matching git worktree, so the
        caller can safely fall back to :meth:`create` for the ordinary
        first-use case.
        """
        if not path or not branch:
            return None
        target = Path(path)
        if not (target / ".git").exists():
            return None
        try:
            self._assert_safe(target)
        except UnsafeWorkspace:
            return None
        from openjarvis.reliability.workspace import git_output

        try:
            actual_branch = git_output(
                ["rev-parse", "--abbrev-ref", "HEAD"], cwd=str(target)
            ).strip()
            actual_head = git_output(["rev-parse", "HEAD"], cwd=str(target)).strip()
        except Exception:  # noqa: BLE001 - not a usable worktree either way
            return None
        if actual_branch != branch:
            return None
        if base_sha and actual_head != base_sha:
            # Found re-verifying FEAT-00031 a second time: a branch that has
            # moved past its recorded base is not necessarily wrong — a
            # commit may genuinely have landed, which is exactly what a
            # successful build-and-push does. Refusing unconditionally here
            # sent every caller (see _worktree_for) to create(), which is
            # documented and correct to destroy whatever already exists at
            # the path — silently discarding a real, already-pushed commit
            # and leaving the local worktree at base_sha while origin sat
            # ahead of it. The next push then failed as a non-fast-forward,
            # which reads like a fresh problem rather than the actual cause.
            #
            # The distinction that matters is not "did HEAD move" but "did it
            # move *forward*": a worktree whose HEAD is a genuine descendant
            # of base_sha reflects real, intentional progress and is exactly
            # as safe to hand back as an untouched one. Only a HEAD that is
            # NOT reachable from base_sha at all — diverged, rewound, or
            # pointed somewhere unrelated — is the case this method was
            # actually built to refuse.
            from openjarvis.reliability.workspace import is_ancestor

            try:
                if not is_ancestor(base_sha, actual_head, cwd=target):
                    return None
            except Exception:  # noqa: BLE001 - not a usable worktree either way
                return None
        logger.info(
            "reusing the existing feature worktree for %s: %s @ %s",
            feature_id,
            branch,
            actual_head[:12],
        )
        return Worktree(
            incident_id=feature_id,
            path=str(target),
            branch=branch,
            base_commit=base_sha or actual_head,
            base_ref=self.repo_path,
        )

    def _forget_stale_worktrees(self) -> None:
        """Drop git's record of worktrees whose directories are gone.

        Git keeps a registration per worktree, and a directory removed out from
        under it — a killed process, a cleaned temp directory, an operator
        tidying up — leaves the registration behind. The branch then counts as
        checked out somewhere, so deleting it fails, so ``worktree add -b``
        fails, and the feature becomes unrecoverable with a git error the
        operator cannot act on.

        Pruning first costs a fast git call and turns "this feature can never be
        retried" into "this feature can be retried". Only stale entries are
        touched: a worktree that still exists is not pruned, so a build in
        progress elsewhere is unaffected.
        """
        from openjarvis.reliability.workspace import git_output

        try:
            git_output(["worktree", "prune"], cwd=self.repo_path, check=False)
        except Exception:  # pragma: no cover - defensive
            logger.debug("could not prune stale worktrees", exc_info=True)

    # -- delegation --------------------------------------------------------

    def changed_files(self, worktree: Worktree) -> List[str]:
        return self._inner.changed_files(worktree)

    def diff(self, worktree: Worktree, *, max_chars: int = 20000) -> str:
        return self._inner.diff(worktree, max_chars=max_chars)

    def diff_stat(self, worktree: Worktree) -> str:
        return self._inner.diff_stat(worktree)

    def line_counts(self, worktree: Worktree) -> Tuple[int, int]:
        return self._inner.line_counts(worktree)

    def has_changes(self, worktree: Worktree) -> bool:
        return self._inner.has_changes(worktree)

    def commit_all(self, worktree: Worktree, message: str) -> str:
        return self._inner.commit_all(worktree, message)

    def head_sha(self, worktree: Worktree) -> str:
        return self._inner.head_sha(worktree)

    def push(self, worktree: Worktree, *, remote: str = "origin") -> None:
        self._inner.push(worktree, remote=remote)

    def remove(self, worktree: Worktree, *, succeeded: bool = True) -> None:
        self._inner.remove(worktree, succeeded=succeeded)

    def cleanup_all(self) -> None:
        self._inner.cleanup_all()
