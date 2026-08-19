"""Finding the preview deployment that is actually this feature.

§14 of the brief: *never mistake an unrelated deployment for the feature.* The
existing reliability code finds a preview by branch, which is right for its
purpose and wrong for this one. A branch has many deployments — the first
attempt, the second attempt, and whatever a parallel session pushed a minute
ago — and the newest READY one on the branch is very often not the commit that
is about to be verified. Verifying the wrong build and calling the feature done
is the failure mode that this module exists to make impossible: matching is on
the **commit SHA**, and a deployment whose SHA does not match is not a
candidate, however recent, however green.

The second rule is that ``READY`` means ready. Vercel reports ``BUILDING``,
``QUEUED``, ``ERROR``, ``CANCELED`` and ``READY``; only the last one is a
deployment that can be opened in a browser. A build that errored is evidence for
the next Claude attempt — it is returned as an observation with the state on it,
not as an absence — because "the preview never appeared" and "the preview failed
to build" lead to completely different next steps.

Nothing here waits forever. A deadline expires and the feature stops with an
honest account of what it saw, which is the outcome the operator can act on.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "PreviewObservation",
    "PreviewObserver",
    "PreviewUnavailable",
    "TERMINAL_FAILURE_STATES",
]

#: Deployment states from which no amount of waiting produces a preview.
TERMINAL_FAILURE_STATES = frozenset({"ERROR", "CANCELED", "CANCELLED", "DELETED"})

#: States that mean "still working".
PENDING_STATES = frozenset({"QUEUED", "BUILDING", "INITIALIZING", "PENDING"})


class PreviewUnavailable(RuntimeError):
    """No preview deployment can be produced for this commit."""


@dataclass(slots=True)
class PreviewObservation:
    """What was seen while waiting for one commit's preview."""

    #: Whether a deployment for the exact commit was found at all.
    matched: bool = False

    #: Whether that deployment is ``READY``. Only then may it be verified.
    ready: bool = False

    commit_sha: str = ""
    branch: str = ""
    deployment_id: str = ""
    state: str = ""
    url: str = ""
    created_at: str = ""

    waited_seconds: float = 0.0
    polls: int = 0

    #: Plain English, for the operator and for the next Claude attempt.
    reason: str = ""

    #: Build output, fetched only when the build failed. Untrusted external
    #: content: anything in the repository's build scripts can print anything.
    build_log: str = ""

    #: Deployments seen on the branch that were *not* this commit. Recorded so
    #: that "there is a green preview for this branch, just not for your commit"
    #: is a sentence the operator can be told rather than a silent near-miss.
    other_deployments: List[Dict[str, str]] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether this observation names a preview that can be verified."""
        return self.matched and self.ready and bool(self.url)

    @property
    def failed_to_build(self) -> bool:
        return self.matched and self.state.upper() in TERMINAL_FAILURE_STATES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "matched": self.matched,
            "ready": self.ready,
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "deployment_id": self.deployment_id,
            "state": self.state,
            "url": self.url,
            "created_at": self.created_at,
            "waited_seconds": round(self.waited_seconds, 1),
            "polls": self.polls,
            "reason": self.reason,
            "build_log": self.build_log[-4000:],
            "other_deployments": list(self.other_deployments),
        }

    def evidence(self) -> str:
        """What the next Claude attempt is told, when the build failed."""
        if self.usable:
            return ""
        if self.failed_to_build:
            head = (
                f"The preview build for commit {self.commit_sha[:12]} finished in "
                f"state {self.state}."
            )
            if self.build_log:
                return f"{head}\n\nBuild output (tail):\n{self.build_log[-4000:]}"
            return head
        if not self.matched:
            return (
                f"No preview deployment appeared for commit {self.commit_sha[:12]} "
                f"on {self.branch} within {self.waited_seconds:.0f}s."
            )
        return (
            f"The preview for commit {self.commit_sha[:12]} was still in state "
            f"{self.state} after {self.waited_seconds:.0f}s."
        )


@dataclass
class PreviewObserver:
    """Waits for the preview deployment of one exact commit.

    Parameters
    ----------
    vercel:
        Anything with ``list_deployments(limit=..., target=...)`` returning the
        normalised summaries
        :class:`~openjarvis.reliability.sources.vercel.VercelSource` produces.
        Typed structurally rather than by import so that a test double is the
        same shape as the real thing and no test needs a network.
    timeout_seconds:
        How long to wait before giving up. A preview that has not appeared in
        this long is a problem to report, not to keep waiting on.
    poll_seconds:
        Gap between polls.
    fetch_build_logs:
        Whether to pull build output when a build fails. On by default: the
        build log is the single most useful piece of evidence the next attempt
        can be given, and without it "the preview failed" tells Claude nothing
        it can act on.
    """

    vercel: Any
    timeout_seconds: float = 600.0
    poll_seconds: float = 15.0
    fetch_build_logs: bool = True
    deployment_page_size: int = 40

    #: Injected so tests do not sleep and do not depend on a clock.
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic

    def observe(self, *, commit_sha: str, branch: str = "") -> PreviewObservation:
        """Poll until the preview for *commit_sha* is READY, fails, or times out."""
        if not commit_sha:
            raise PreviewUnavailable(
                "a commit SHA is required; a preview cannot be matched by branch "
                "alone without risking verifying somebody else's push"
            )

        started = self.monotonic()
        observation = PreviewObservation(commit_sha=commit_sha, branch=branch)

        while True:
            observation.polls += 1
            deployments = self._list()
            match = self._match(deployments, commit_sha)
            observation.other_deployments = self._others(
                deployments, commit_sha, branch
            )

            if match is not None:
                observation.matched = True
                observation.deployment_id = str(match.get("id", ""))
                observation.state = str(match.get("state", "")).upper()
                observation.url = str(match.get("url", ""))
                observation.created_at = str(match.get("created_at", ""))
                observation.branch = str(match.get("branch", "")) or branch

                if observation.state == "READY":
                    observation.ready = True
                    observation.reason = "the preview for this commit is ready"
                    break

                if observation.state in TERMINAL_FAILURE_STATES:
                    observation.reason = (
                        f"the preview build finished in state {observation.state}"
                    )
                    if self.fetch_build_logs:
                        observation.build_log = self._build_log(
                            observation.deployment_id
                        )
                    break

            observation.waited_seconds = self.monotonic() - started
            if observation.waited_seconds >= self.timeout_seconds:
                elapsed = f"{observation.waited_seconds:.0f}s"
                if observation.matched:
                    observation.reason = (
                        "the preview for this commit was still "
                        f"{observation.state or 'unfinished'} after {elapsed}"
                    )
                else:
                    observation.reason = (
                        "no preview deployment appeared for this commit after "
                        f"{elapsed}"
                    )
                break

            self.sleep(self.poll_seconds)

        observation.waited_seconds = self.monotonic() - started
        logger.info(
            "preview for %s: matched=%s ready=%s state=%s after %.0fs",
            commit_sha[:12],
            observation.matched,
            observation.ready,
            observation.state or "-",
            observation.waited_seconds,
        )
        return observation

    # -- plumbing ----------------------------------------------------------

    def _list(self) -> List[Dict[str, Any]]:
        try:
            return list(
                self.vercel.list_deployments(
                    limit=self.deployment_page_size, target="preview"
                )
                or []
            )
        except Exception as exc:
            # A provider outage is not a feature failure. Reported as "nothing
            # seen this poll" so the loop keeps its own deadline rather than
            # ending the feature on one bad response.
            logger.warning("could not list preview deployments: %s", exc)
            return []

    @staticmethod
    def _same_commit(found: str, wanted: str) -> bool:
        """Whether two commit identifiers name the same commit.

        Compared on the common prefix, because providers abbreviate
        inconsistently. Anything shorter than seven characters on either side is
        refused rather than matched loosely: at that length a collision is a
        plausible accident rather than a remote one, and the cost of a wrong
        match here is verifying somebody else's code and calling it the feature.
        """
        found = found.strip().lower()
        wanted = wanted.strip().lower()
        if len(found) < 7 or len(wanted) < 7:
            return False
        shortest = min(len(found), len(wanted))
        return found[:shortest] == wanted[:shortest]

    @classmethod
    def _match(
        cls, deployments: Sequence[Dict[str, Any]], commit_sha: str
    ) -> Optional[Dict[str, Any]]:
        """The deployment for exactly this commit, or nothing."""
        for deployment in deployments:
            if cls._same_commit(str(deployment.get("commit_sha", "")), commit_sha):
                return deployment
        return None

    @classmethod
    def _others(
        cls, deployments: Sequence[Dict[str, Any]], commit_sha: str, branch: str
    ) -> List[Dict[str, str]]:
        """Deployments on this branch that are *not* this commit.

        Recorded so "there is a green preview for your branch, just not for your
        commit" can be said out loud rather than being a silent near-miss.
        """
        others: List[Dict[str, str]] = []
        for deployment in deployments:
            found = str(deployment.get("commit_sha", "")).strip().lower()
            if cls._same_commit(found, commit_sha):
                continue
            if branch and str(deployment.get("branch", "")) != branch:
                continue
            others.append(
                {
                    "commit_sha": found[:12],
                    "state": str(deployment.get("state", "")),
                    "created_at": str(deployment.get("created_at", "")),
                }
            )
        return others[:5]

    def _build_log(self, deployment_id: str) -> str:
        if not deployment_id:
            return ""
        getter = getattr(self.vercel, "get_build_logs", None)
        if not callable(getter):
            return ""
        try:
            return str(getter(deployment_id) or "")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("could not fetch build logs: %s", exc)
            return ""
