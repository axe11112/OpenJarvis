"""Good morning, Sir.

§29. A short, factual account of where things stand: whether the site is
healthy, what was built, and what is waiting on the operator. Not a report — a
sentence or two per thing, in the order somebody actually cares about them.

Three rules, and each one is about the same failure.

**Say something only when there is something to say.** A summary that arrives
every morning saying "everything is fine, nothing happened" is a summary nobody
reads by the end of the second week, and by then it is the one carrying the
sentence that mattered. :meth:`Briefing.worth_sending` answers whether there is
anything in it; a caller that ignores that answer is building the habit of not
reading.

**Approvals first.** The one thing in a morning summary that has a deadline is
the feature that has been sitting waiting for a person since yesterday. It goes
above the good news.

**Never claim what was not checked.** "Wize is healthy" is a claim about probes
that ran. When the reliability subsystem is not configured or has no data, the
summary says it does not know, which is a shorter and more useful sentence than
a confident one that is guessing.

Nothing here schedules anything or sends anything. It composes text and returns
it; who delivers it, and whether they are allowed to, is decided elsewhere —
§29's "do not enable noisy scheduled delivery without safe existing authority"
is enforced by this module having no way to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from openjarvis.reliability.briefing import redact_secrets
from openjarvis.wiz.features.model import FeatureState

logger = logging.getLogger(__name__)

__all__ = ["Briefing", "compose"]


@dataclass(slots=True)
class Briefing:
    """What Wiz would say this morning, in pieces so a caller can pick."""

    greeting: str = "Good morning, Sir."

    #: One line on the site. Empty when Wiz genuinely cannot tell.
    health: str = ""

    #: Things needing the operator, most urgent first.
    needs_you: List[str] = field(default_factory=list)

    #: What happened since yesterday.
    built: List[str] = field(default_factory=list)
    fixed: List[str] = field(default_factory=list)

    #: What is in progress right now.
    in_progress: List[str] = field(default_factory=list)

    @property
    def worth_sending(self) -> bool:
        """Whether there is anything here a person needs.

        A summary that arrives saying "nothing happened" every day is one nobody
        reads by the end of the second week, and by then it is the one carrying
        the sentence that mattered.
        """
        return bool(self.needs_you or self.built or self.fixed or self.in_progress)

    def render(self) -> str:
        """The whole thing, as the operator hears it."""
        lines = [self.greeting]

        if self.health:
            lines.append("")
            lines.append(self.health)

        if self.needs_you:
            lines.append("")
            lines.append(
                "You need to look at something:"
                if len(self.needs_you) > 1
                else "One thing needs you:"
            )
            lines.extend(f"- {item}" for item in self.needs_you)

        if self.built or self.fixed:
            lines.append("")
            lines.append("Yesterday:")
            lines.extend(f"- I built {item}." for item in self.built)
            lines.extend(f"- I fixed {item}." for item in self.fixed)

        if self.in_progress:
            lines.append("")
            lines.append("Working on:")
            lines.extend(f"- {item}" for item in self.in_progress)

        if not self.worth_sending:
            lines.append("")
            lines.append("Nothing needs you.")

        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "greeting": self.greeting,
            "health": self.health,
            "needs_you": list(self.needs_you),
            "built": list(self.built),
            "fixed": list(self.fixed),
            "in_progress": list(self.in_progress),
            "worth_sending": self.worth_sending,
        }


def compose(
    *,
    store: Any = None,
    memory: Any = None,
    reliability: Any = None,
    now: Optional[str] = None,
    site_name: str = "The site",
) -> Briefing:
    """Build this morning's summary from what is actually recorded.

    Every source is optional and every one of them may be absent, broken or
    empty. A summary that raises because the incident database was moved is a
    summary the operator stops receiving without noticing.
    """
    briefing = Briefing()
    reference = _reference(now)
    yesterday = (reference - timedelta(days=1)).date().isoformat()

    briefing.health = _health(reliability, site_name)

    if store is not None:
        try:
            briefing.needs_you = _needs_you(store)
            briefing.in_progress = _in_progress(store)
            briefing.built = _built_on(store, yesterday)
        except Exception:
            logger.exception("could not read the feature store for the briefing")

    if memory is not None and not briefing.built:
        # The store is the better source — it knows *states*. Memory is the
        # fallback for a machine where features have been pruned but the record
        # of what was built survives.
        try:
            briefing.built = [
                entry.title for entry in memory.on_day(yesterday, kinds=["feature"])
            ][:5]
        except Exception:
            logger.exception("could not read product memory for the briefing")

    return briefing


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------


def _reference(now: Optional[str]) -> datetime:
    if now:
        try:
            return datetime.fromisoformat(now[:10]).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _health(reliability: Any, site_name: str) -> str:
    """One line about the site, or nothing at all.

    "I do not know" is a shorter and more useful sentence than a confident one
    that is guessing, so an unavailable reliability subsystem produces no health
    line rather than a reassuring one.
    """
    if reliability is None:
        return ""
    try:
        status = reliability() if callable(reliability) else reliability
    except Exception:
        logger.exception("could not read reliability status for the briefing")
        return ""

    if not isinstance(status, dict) or not status.get("available"):
        return f"I cannot see {site_name.lower()} at the moment, so I cannot say."

    open_count = int(status.get("open", 0) or 0)
    if open_count == 0:
        return f"{site_name} is healthy."
    if open_count == 1:
        incident = (status.get("incidents") or [{}])[0]
        title = str(incident.get("title", "")).strip() or "something"
        return f"{site_name} has one open problem: {title}."
    return f"{site_name} has {open_count} open problems."


def _needs_you(store: Any) -> List[str]:
    """Features that stopped and are waiting for a person.

    Ordered so the reason comes with the feature: "Z needs your approval
    because it changes permissions" is actionable; "Z needs you" is a
    to-do item somebody has to go and investigate.
    """
    lines: List[str] = []
    blocked = store.list(states=[FeatureState.HUMAN_REQUIRED], limit=10)
    for feature in blocked:
        reason = ""
        if feature.history:
            # Redacted before truncation: some history reasons carry a raw
            # exception string (a GitHub or Vercel client failure), and this
            # is an owner notification — the same surface redact_secrets()'s
            # own docstring names explicitly.
            reason = redact_secrets(str(feature.history[-1].get("reason", ""))).strip()
        if feature.risk == "HIGH":
            reasons = feature.metadata.get("risk_reasons") or []
            why = reasons[0] if reasons else "it is a high-risk change"
            lines.append(f"{feature.title} needs your approval because {why}")
        elif reason:
            lines.append(f"{feature.title}: {_first_sentence(reason)}")
        else:
            lines.append(f"{feature.title} stopped and needs you")

    ready = store.list(states=[FeatureState.READY], limit=10)
    for feature in ready:
        where = f" — {feature.pr_url}" if feature.pr_url else ""
        lines.append(f"{feature.title} is ready and waiting for you{where}")

    return lines[:8]


def _in_progress(store: Any) -> List[str]:
    working = [
        f
        for f in store.active(limit=20)
        if f.state
        not in (FeatureState.READY, FeatureState.HUMAN_REQUIRED, FeatureState.RECEIVED)
    ]
    return [f"{f.title} ({_plain(f.state)})" for f in working][:5]


def _built_on(store: Any, day: str) -> List[str]:
    """Features that reached READY or COMPLETE on *day*."""
    finished = store.list(states=[FeatureState.READY, FeatureState.COMPLETE], limit=50)
    return [f.title for f in finished if (f.updated_at or "").startswith(day)][:5]


#: State names as a person would say them. The internal vocabulary is precise
#: and the operator did not agree to learn it — §34.
_PLAIN = {
    FeatureState.UNDERSTANDING: "reading the request",
    FeatureState.PLANNING: "working out how",
    FeatureState.APPROVED_FOR_BUILD: "about to start",
    FeatureState.BUILDING: "writing the code",
    FeatureState.TESTING: "running the checks",
    FeatureState.PREVIEWING: "waiting for a preview",
    FeatureState.VERIFYING: "checking it works",
    FeatureState.MERGING: "merging",
    FeatureState.DEPLOYING: "deploying",
    FeatureState.PRODUCTION_VERIFYING: "checking it live",
}


def _plain(state: FeatureState) -> str:
    return _PLAIN.get(state, state.value.lower().replace("_", " "))


def _first_sentence(text: str, *, limit: int = 140) -> str:
    cleaned = " ".join((text or "").split())
    for stop in (". ", "; "):
        if stop in cleaned:
            cleaned = cleaned.split(stop, 1)[0]
            break
    return cleaned[:limit]
