"""Correlation — from "something broke" to "this is probably why".

The output is a ranked *guess* with an explicit confidence, never a claim.
Getting this wrong sends the coding agent to the wrong file, so the honest
signal matters more than the answer: a low-confidence correlation tells the
agent to investigate broadly, a high-confidence one tells it where to look.

Deliberately heuristic and deliberately transparent — every score contribution
is recorded in ``Correlation.notes`` so a human can see why JARVIS pointed
where it did.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from openjarvis.reliability.types import Correlation

logger = logging.getLogger(__name__)

__all__ = ["correlate", "parse_iso", "score_commit"]

#: Commits older than this before the failure are unlikely to be the cause.
DEFAULT_WINDOW = timedelta(hours=6)

#: Weights for the individual signals.  Tuned to be explainable rather than
#: optimal: recency dominates, component overlap confirms.
_W_RECENCY = 0.45
_W_COMPONENT = 0.30
_W_DEPLOYMENT = 0.25


def parse_iso(value: str) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp, tolerating a trailing ``Z``.

    Returns ``None`` rather than raising: a malformed timestamp from an external
    API must degrade the correlation, not crash the pipeline.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _recency_score(
    commit_time: Optional[datetime],
    failure_time: Optional[datetime],
    window: timedelta,
) -> float:
    """1.0 for a commit right before the failure, decaying to 0.0 at the window edge."""
    if commit_time is None or failure_time is None:
        return 0.0
    delta = (failure_time - commit_time).total_seconds()
    if delta < 0:
        return 0.0  # landed after the failure: cannot be the cause
    span = window.total_seconds()
    if delta > span:
        return 0.0
    return 1.0 - (delta / span)


def _component_score(files: Sequence[str], component: str) -> float:
    """How strongly a commit's changed files relate to the failing component."""
    if not files or not component:
        return 0.0
    needle = component.lower().replace("-", "").replace("_", "")
    if not needle:
        return 0.0
    hits = 0
    for path in files:
        haystack = path.lower().replace("-", "").replace("_", "")
        if needle in haystack:
            hits += 1
    if not hits:
        return 0.0
    # Saturating: one clearly-related file is most of the signal.
    return min(1.0, 0.6 + 0.4 * (hits - 1))


def score_commit(
    commit: Dict[str, Any],
    *,
    failure_time: Optional[datetime],
    component: str = "",
    deployment_sha: str = "",
    window: timedelta = DEFAULT_WINDOW,
) -> tuple[float, List[str]]:
    """Score one commit as a candidate cause.

    Returns ``(score, reasons)`` where *score* is 0.0-1.0 and *reasons* explains
    every contribution in human-readable terms.
    """
    reasons: List[str] = []
    commit_time = parse_iso(commit.get("date", ""))

    recency = _recency_score(commit_time, failure_time, window)
    if recency > 0:
        minutes = (
            int((failure_time - commit_time).total_seconds() // 60)
            if failure_time and commit_time
            else 0
        )
        reasons.append(f"landed {minutes} min before the failure")

    files = [f.get("filename", "") for f in commit.get("files", [])]
    component_hit = _component_score(files, component)
    if component_hit > 0:
        related = [f for f in files if component.lower().split("-")[0] in f.lower()]
        reasons.append(f"touches {len(related) or 1} file(s) related to '{component}'")

    deployment_hit = 0.0
    if deployment_sha and commit.get("sha", "").startswith(deployment_sha[:7]):
        deployment_hit = 1.0
        reasons.append("is the commit in the failing deployment")

    score = (
        _W_RECENCY * recency
        + _W_COMPONENT * component_hit
        + _W_DEPLOYMENT * deployment_hit
    )
    return round(min(score, 1.0), 3), reasons


def correlate(
    *,
    failure_time: str,
    commits: Sequence[Dict[str, Any]],
    component: str = "",
    deployment_id: str = "",
    deployment_sha: str = "",
    pull_requests: Optional[Sequence[Dict[str, Any]]] = None,
    window: timedelta = DEFAULT_WINDOW,
) -> Correlation:
    """Pick the most likely cause of a failure.

    Parameters
    ----------
    failure_time:
        When the failure was observed (ISO 8601).
    commits:
        Candidate commits, each optionally carrying a ``files`` list.
    component:
        The failing component, used to weight file-path overlap.
    deployment_id, deployment_sha:
        The deployment in play, when one is known.
    pull_requests:
        Open/recent PRs, used to attach a PR number to the winning commit.

    Returns
    -------
    Correlation
        With ``confidence`` 0.0 when nothing plausible was found — an empty
        correlation is a valid and honest answer.
    """
    failure_at = parse_iso(failure_time)
    scored: List[tuple[float, List[str], Dict[str, Any]]] = []

    for commit in commits:
        score, reasons = score_commit(
            commit,
            failure_time=failure_at,
            component=component,
            deployment_sha=deployment_sha,
            window=window,
        )
        if score > 0:
            scored.append((score, reasons, commit))

    if not scored:
        return Correlation(
            deployment_id=deployment_id,
            confidence=0.0,
            notes=(
                "No commit landed in the correlation window before the failure; "
                "the cause is likely environmental or in an unversioned change."
            ),
        )

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_reasons, best_commit = scored[0]

    # A close runner-up means we should not sound confident.
    if len(scored) > 1 and scored[1][0] > best_score * 0.8:
        best_score *= 0.8
        best_reasons.append(
            f"another commit ({scored[1][2].get('sha', '')[:7]}) scores nearly "
            "as highly, so this attribution is uncertain"
        )

    files = [f.get("filename", "") for f in best_commit.get("files", [])]
    pr_number, branch = _find_pull_request(best_commit, pull_requests or [])

    return Correlation(
        deployment_id=deployment_id,
        commit_sha=best_commit.get("sha", ""),
        pr_number=pr_number,
        branch=branch,
        changed_files=files,
        confidence=round(min(best_score, 1.0), 3),
        notes="; ".join(best_reasons),
    )


def _find_pull_request(
    commit: Dict[str, Any], pull_requests: Sequence[Dict[str, Any]]
) -> tuple[int, str]:
    """Match a commit to a pull request by merge commit or title."""
    message = (commit.get("message") or "").lower()
    for pull_request in pull_requests:
        number = pull_request.get("number", 0)
        if number and f"#{number}" in message:
            return number, pull_request.get("head", "")
        title = (pull_request.get("title") or "").lower()
        if title and title in message:
            return number, pull_request.get("head", "")
    return 0, ""
