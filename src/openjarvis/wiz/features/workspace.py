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

    def push(self, worktree: Worktree, *, remote: str = "origin") -> None:
        self._inner.push(worktree, remote=remote)

    def remove(self, worktree: Worktree, *, succeeded: bool = True) -> None:
        self._inner.remove(worktree, succeeded=succeeded)

    def cleanup_all(self) -> None:
        self._inner.cleanup_all()
