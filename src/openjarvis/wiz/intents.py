"""Turning a sentence into the name of a verb — with no model involved.

The classifier here is rules over regular expressions. That is not a placeholder
for something cleverer later; it is the design. Classification decides which
capability runs, and therefore which authority is checked, so a wrong answer is
a security event rather than a usability annoyance. Rules are wrong in ways that
can be enumerated, tested and read in a diff.

A model may eventually help with the *hard* cases, and there is room for that:
a suggester can be layered in front, and whatever it returns is still only a
string that must name a registered capability whose authority and risk were
declared by a human. What must never happen is a model widening the action
surface, and keeping the default classifier deterministic means the surface has
a floor that does not move.

Ambiguity resolves to ``None``. Wiz asking "did you mean X?" costs a sentence;
Wiz guessing costs whatever the wrong verb did.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Pattern, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from openjarvis.wiz.brain import Request

__all__ = ["IntentRule", "RuleClassifier", "default_rules"]


@dataclass(frozen=True, slots=True)
class IntentRule:
    """One pattern that names one capability."""

    capability: str
    pattern: Pattern[str]

    #: Higher wins when several rules match. Used to let a specific phrase beat
    #: a general one ("what can you do" is a capability question, not a
    #: question about the word "do").
    weight: int = 0

    def matches(self, text: str) -> bool:
        return bool(self.pattern.search(text))


def _rule(capability: str, pattern: str, weight: int = 0) -> IntentRule:
    return IntentRule(
        capability=capability,
        pattern=re.compile(pattern, re.IGNORECASE),
        weight=weight,
    )


def default_rules() -> List[IntentRule]:
    """The read-and-explain verbs Wiz starts life knowing.

    Nothing here writes anything. Wiz's first vocabulary is deliberately made of
    questions, so that the dispatch path, the authority checks and the journal
    can all be exercised in production before any verb exists that can change a
    file.
    """
    return [
        _rule(
            "wiz.capabilities",
            r"\b(what can you do|what are you able|list (your )?capabilit\w*|"
            r"what can (you|wiz) handle)\b",
            weight=10,
        ),
        _rule(
            "wiz.authority",
            r"\b(what are you allowed|what authority|what permissions|"
            r"are you allowed to)\b",
            weight=10,
        ),
        _rule(
            "wiz.health",
            r"\b(how are you|are you (ok|healthy|working)|wiz status|"
            r"your (own )?(health|status))\b",
            weight=10,
        ),
        _rule(
            "reliability.status",
            r"\b(is (the )?(site|website|production|wize) (ok|up|down|healthy)|"
            r"production status|site status|how is (the )?(site|website|production))\b",
            weight=8,
        ),
        _rule(
            "reliability.incidents",
            r"\b(incidents?|outages?|what broke|anything broken|"
            r"what went wrong)\b",
            weight=5,
        ),
    ]


class RuleClassifier:
    """Picks a capability name from text, or declines to."""

    def __init__(self, rules: Optional[Sequence[IntentRule]] = None) -> None:
        self._rules = list(rules if rules is not None else default_rules())

    @property
    def rules(self) -> List[IntentRule]:
        return list(self._rules)

    def classify_text(self, text: str) -> Optional[str]:
        """The capability *text* names, if exactly one is the best match."""
        cleaned = (text or "").strip()
        if not cleaned:
            return None

        matched = [rule for rule in self._rules if rule.matches(cleaned)]
        if not matched:
            return None

        best = max(rule.weight for rule in matched)
        winners = {rule.capability for rule in matched if rule.weight == best}
        if len(winners) != 1:
            # Two different capabilities matched equally well. Refusing to
            # choose is the correct answer: the alternative is a coin flip
            # deciding which authority gets checked.
            return None
        return winners.pop()

    def __call__(self, request: "Request") -> Optional[str]:
        """Adapter for :class:`openjarvis.wiz.brain.Wiz`."""
        return self.classify_text(request.text)
