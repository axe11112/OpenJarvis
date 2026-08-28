"""Real counts of what actually happened — not what the architecture could do.

Every number here is derived from the FeatureStore, the same source of truth
:mod:`openjarvis.wiz.briefing` already reads. Nothing is inferred, nothing is
projected forward, and nothing is recorded by a separate instrumentation call
that a real code path might forget to make — nine states have exactly nine
places a feature can be, and this counts which one every stored feature is in
right now, plus what its own history says happened along the way.

**A problem fixing itself is not a Wiz repair.** This module has no opinion
about incidents at all — it counts features. Incident autonomy belongs to
:mod:`openjarvis.reliability`, which already has its own store and its own
counters; duplicating that here would be a second, drifting copy of the same
facts.

**Do not invent a rate from a small sample.** Every rate this module computes
is returned alongside the count it was computed from, and a caller rendering
this for a person should show the count, not just the percentage — five
features is not a statistic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from openjarvis.wiz.features.model import FeatureState

__all__ = ["FeatureMetrics", "summarize_features"]


@dataclass(slots=True)
class FeatureMetrics:
    """One snapshot of the FeatureStore, counted by outcome."""

    #: Every feature ever recorded, minus none — the honest denominator.
    sample_size: int = 0

    received: int = 0
    completed: int = 0
    human_required: int = 0
    cancelled: int = 0
    #: Currently mid-pipeline: not yet READY, not yet stopped.
    in_progress: int = 0

    #: COMPLETE and LOW risk — the only features that could possibly have
    #: shipped with no operator decision anywhere in their history.
    low_autonomous_completions: int = 0
    #: COMPLETE and NOT LOW risk — reached production only because an
    #: operator approved something along the way (MEDIUM/HIGH auto-merge is
    #: policy-disabled, so a MEDIUM or HIGH feature that completed did so
    #: through an operator action, not on its own).
    operator_approved_completions: int = 0

    total_claude_attempts: int = 0
    average_attempts: float = 0.0

    #: Risk classification rose from what a request was first read as to
    #: something higher during planning or after the real diff existed —
    #: see FeaturePipeline's own docstring on why the agent may only raise
    #: the level, never lower it.
    risk_escalations: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sample_size": self.sample_size,
            "received": self.received,
            "completed": self.completed,
            "human_required": self.human_required,
            "cancelled": self.cancelled,
            "in_progress": self.in_progress,
            "low_autonomous_completions": self.low_autonomous_completions,
            "operator_approved_completions": self.operator_approved_completions,
            "total_claude_attempts": self.total_claude_attempts,
            "average_attempts": self.average_attempts,
            "risk_escalations": self.risk_escalations,
        }

    def render(self) -> str:
        """A short, honest summary — the count first, the rate only beside it."""
        if self.sample_size == 0:
            return "No features have been recorded yet."
        lines = [
            f"{self.sample_size} feature(s) recorded: "
            f"{self.completed} completed, {self.human_required} needed a person, "
            f"{self.cancelled} cancelled, {self.in_progress} in progress.",
        ]
        if self.completed:
            lines.append(
                f"Of {self.completed} completed: {self.low_autonomous_completions} "
                f"shipped with no operator decision, "
                f"{self.operator_approved_completions} required one."
            )
        if self.risk_escalations:
            lines.append(
                f"{self.risk_escalations} feature(s) had their risk raised after "
                "the initial read."
            )
        return " ".join(lines)


def summarize_features(store: Any) -> FeatureMetrics:
    """Count every feature in *store* by outcome. Never raises."""
    if store is None:
        return FeatureMetrics()
    try:
        features: List[Any] = store.list(limit=100_000)
    except Exception:
        return FeatureMetrics()

    metrics = FeatureMetrics(sample_size=len(features))
    if not features:
        return metrics

    for feature in features:
        metrics.received += 1
        state = getattr(feature, "state", None)
        risk = str(getattr(feature, "risk", "") or "").strip().upper()
        attempts = int(getattr(feature, "attempts_used", 0) or 0)
        metrics.total_claude_attempts += attempts

        if state is FeatureState.COMPLETE:
            metrics.completed += 1
            if risk == "LOW":
                metrics.low_autonomous_completions += 1
            else:
                metrics.operator_approved_completions += 1
        elif state is FeatureState.HUMAN_REQUIRED:
            metrics.human_required += 1
        elif state is FeatureState.CANCELLED:
            metrics.cancelled += 1
        else:
            metrics.in_progress += 1

        if _risk_was_escalated(feature):
            metrics.risk_escalations += 1

    metrics.average_attempts = metrics.total_claude_attempts / metrics.sample_size
    return metrics


#: The exact stop message FeaturePipeline._build writes when the real diff
#: reads as riskier than the request did — see its "feature.risk_raised_by_diff"
#: stop. Matched literally rather than guessed at from metadata, which is
#: populated on every classification, escalated or not, and would over-count.
_ESCALATION_PHRASE = "turned out to change something sensitive"


def _risk_was_escalated(feature: Any) -> bool:
    """Whether history shows the diff-triggered risk-escalation stop.

    Conservative and specific on purpose: this only recognises the one case
    that leaves an unambiguous trace in ``feature.history`` (a real state
    transition). The HIGH-risk-needs-approval case that can fire earlier, at
    PLANNING, never transitions state and so never appears here — it is
    visible in the journal, not the store, and this function only reads the
    store. Undercounting a real signal is preferred to guessing one from
    fields that do not actually distinguish "escalated" from "always was".
    """
    history = getattr(feature, "history", None) or []
    return any(_ESCALATION_PHRASE in str(entry.get("reason", "")) for entry in history)
