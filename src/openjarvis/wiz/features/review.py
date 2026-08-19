"""A second Claude session that reads the change and says what it thinks.

§17 of the brief. The value is not that the review is right — it often will not
be — but that it is *independent*: a separate session, with no memory of having
written the code, reading the diff cold. An agent asked to review its own work
in the same context has already decided the work is good; that is what finishing
it meant.

So the review session gets the diff and the requirement and nothing else. Not
the plan it wrote, not its own account of what it did, not the previous
attempts. Those are exactly the things that would talk it into agreeing with
itself.

**The review is advisory and stays advisory.** It cannot fail a feature and it
cannot pass one. The deterministic gates and the acceptance contract decide;
this produces something for the operator and the pull request to read.
:class:`ReviewReport` deliberately has no boolean anyone could mistake for a
verdict — its ``concerns`` are text, and the one flag it does carry
(``blocking_suggested``) is named as a suggestion and consumed by nothing in the
pipeline. A test holds that line, because "advisory" is the sort of property
that quietly stops being true the first time a review catches something real.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from openjarvis.wiz.features.engineer import (
    ClaudeCodeEngineeringAgent,
    ContextPack,
)

logger = logging.getLogger(__name__)

__all__ = ["IndependentReviewer", "ReviewReport", "REVIEW_DIMENSIONS"]

#: What the reviewer is asked to look at. Named explicitly so a review is
#: comparable between features rather than being about whatever the model found
#: interesting that day.
REVIEW_DIMENSIONS: Sequence[str] = (
    "correctness — does it actually do what was asked?",
    "requirement coverage — is any part of the request unimplemented?",
    "regression risk — what existing behaviour could this break?",
    "complexity — is there a materially simpler version of this change?",
    "security — anything touching auth, data access, secrets or user input?",
    "missing tests — what could break without a test noticing?",
    "architecture — does this fit how the rest of the codebase works?",
)

#: Wording that means the reviewer thinks somebody should look before this
#: ships. Matched loosely on purpose: this only ever sets a flag on a report.
_BLOCKING = re.compile(
    r"\b(must not|should not ship|do not merge|blocking|serious|critical|"
    r"data loss|security (issue|risk|hole|vulnerability)|leaks?)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ReviewReport:
    """What the review session said. Read by people, not by the pipeline."""

    ran: bool = False
    text: str = ""
    error: str = ""

    #: The reviewer's own view that a person should look first. A suggestion,
    #: consumed by nothing: the gates decide, and a model that can block a
    #: release by using the word "critical" is a model that decides releases.
    blocking_suggested: bool = False

    dimensions: List[str] = field(default_factory=list)

    def summary(self, *, limit: int = 300) -> str:
        if not self.ran:
            return self.error or "no independent review was run"
        first = self.text.strip().split("\n\n", 1)[0]
        return first[:limit]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ran": self.ran,
            "text": self.text[:20000],
            "error": self.error,
            "blocking_suggested": self.blocking_suggested,
            "dimensions": list(self.dimensions),
        }


@dataclass
class IndependentReviewer:
    """Runs the read-only review session.

    Uses the same :class:`ClaudeCodeEngineeringAgent` — and therefore the same
    read-only tool list — as planning. A reviewer that could edit the code would
    be a second implementer.
    """

    engineer: ClaudeCodeEngineeringAgent

    #: Read from git rather than from the agent. Anything with
    #: ``diff(worktree)``; in production the feature workspace.
    workspace: Optional[Any] = None

    #: Changes smaller than this are not worth a second session on a laptop
    #: that has one Claude slot. A one-line copy change does not need a
    #: correctness review; it needs the tests that already ran.
    min_lines: int = 20

    def review(
        self, feature: Any, *, workspace: str, worktree: Any = None
    ) -> Dict[str, Any]:
        """Review *feature*'s change. Never raises; a failed review is not a
        failed feature."""
        report = ReviewReport(dimensions=list(REVIEW_DIMENSIONS))

        attempt = feature.attempts[-1] if getattr(feature, "attempts", None) else None
        if attempt is None:
            report.error = "there is no change to review"
            return report.to_dict()

        if attempt.lines_changed < self.min_lines:
            report.error = (
                f"the change is {attempt.lines_changed} lines; too small to be "
                "worth a separate review session"
            )
            return report.to_dict()

        diff = ""
        if self.workspace is not None and worktree is not None:
            try:
                diff = self.workspace.diff(worktree)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("could not read the diff for review: %s", exc)

        pack = ContextPack(
            goal=self._goal_for(feature, diff),
            repository=getattr(feature, "repository", ""),
            branch=getattr(feature, "branch", ""),
            base_sha=getattr(feature, "base_sha", ""),
            relevant_paths=list(attempt.changed_files)[:40],
            acceptance=list(getattr(feature, "acceptance", []) or []),
        )

        try:
            session = self.engineer.plan(pack, workspace=workspace)
        except Exception as exc:
            report.error = str(exc)
            return report.to_dict()

        if not session.succeeded:
            report.error = session.error or "the review session did not finish"
            return report.to_dict()

        report.ran = True
        report.text = session.claim
        report.blocking_suggested = bool(_BLOCKING.search(session.claim))
        return report.to_dict()

    @staticmethod
    def _goal_for(feature: Any, diff: str) -> str:
        """What the reviewer is told.

        Note what is absent: the plan the implementing session wrote, its
        account of what it did, and the previous attempts. Those are what would
        talk a reviewer into agreeing with the implementer, and the whole point
        of a second session is that it has not already decided.
        """
        lines = [
            "Review a change somebody else wrote. You did not write it and you "
            "have not seen it before.",
            "",
            "The request it is meant to satisfy, in the operator's words:",
            f"    {getattr(feature, 'operator_request', '')}",
            "",
            "Read the change on this branch and say what you think. Cover:",
        ]
        lines.extend(f"- {dimension}" for dimension in REVIEW_DIMENSIONS)
        lines.extend(
            [
                "",
                "Be specific and be brief. Name files and lines. If it is fine, "
                "say so in a sentence rather than finding something to say.",
                "",
                "You are not deciding whether this ships. The tests and the "
                "acceptance checks decide that, and they have already passed. "
                "You are telling a person what to look at.",
            ]
        )
        if diff:
            lines.extend(["", "The diff:", diff[:20000]])
        return "\n".join(lines)
