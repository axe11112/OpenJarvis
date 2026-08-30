"""Deciding how dangerous a change is — without asking the thing making it.

§11 of the brief: *"Do not let an LLM self-classify its own authority without
deterministic policy checks."* The failure this prevents is specific and worth
naming. A coding agent asked "is this risky?" is being asked to authorise itself,
and the answer that gets it unblocked is "no". It need not be lying; a model that
genuinely believes a change to a session helper is presentational will say so
just as confidently as one that has been talked into saying it.

So classification here runs on things a model cannot assert its way past: the
paths a change actually touches, read from git, and the words in the request.
Both are combined by taking the *highest* risk found, never the average and
never the most recent. A model's own opinion may be supplied, and it can only
ever raise the result — :func:`classify` takes the maximum, so an agent can warn
that something is dangerous but cannot certify that it is safe.

The path patterns are conservative on purpose. A false HIGH costs one approval
click. A false LOW costs an unreviewed change to authentication.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Pattern, Sequence, Tuple

from openjarvis.wiz.capabilities import Risk

__all__ = ["RiskAssessment", "classify", "classify_paths", "classify_text"]


def _patterns(*sources: str) -> List[Pattern[str]]:
    return [re.compile(source, re.IGNORECASE) for source in sources]


#: Paths whose modification is HIGH risk regardless of what the change does.
#:
#: Matched against the whole path, so ``src/lib/auth/session.ts`` and
#: ``supabase/migrations/0007_add_rls.sql`` both hit.
HIGH_RISK_PATHS: List[Pattern[str]] = _patterns(
    r"(^|/)(auth|authentication|authorization|authorisation)(/|\.|$)",
    r"(^|/)middleware\.(ts|js|tsx|jsx|py)$",
    r"(^|/)(login|signup|signin|session|password|token|jwt|oauth)",
    r"(^|/)(payment|payments|billing|stripe|checkout|subscription)",
    r"(^|/)migrations?(/|$)",
    r"\.sql$",
    r"(^|/)(rls|policies|policy)\.(sql|ts|js)$",
    r"(^|/)schema\.(sql|prisma|ts)$",
    r"(^|/)(health|biometric|medical|hipaa|gdpr)",
    r"(^|/)\.env",
    r"(^|/)(secrets?|credentials?|keys?)(/|\.|$)",
    r"(^|/)(security|permissions?|roles?|rbac)(/|\.|$)",
    r"(^|/)\.github/workflows(/|$)",
    r"(^|/)(Dockerfile|docker-compose\.ya?ml|vercel\.json|next\.config\.[a-z]+)$",
    r"(^|/)supabase(/|$)",
)

#: Paths that mean real application behaviour is changing.
MEDIUM_RISK_PATHS: List[Pattern[str]] = _patterns(
    r"(^|/)api(/|$)",
    r"(^|/)(routes?|server|actions?|handlers?)(/|$)",
    r"(^|/)(lib|utils|hooks|services|store)(/|$)",
    r"(^|/)package\.json$",
    r"(^|/)(components?|app|pages)(/|$).*\.(ts|tsx|js|jsx)$",
)

#: Requests whose wording alone makes them HIGH.
HIGH_RISK_WORDS: List[Pattern[str]] = _patterns(
    r"\b(auth|authentication|authoris|authoriz|permission|role|rbac)\w*",
    r"\b(login|log in|sign in|signup|sign up|password|token)\w*",
    # "Session" only when it is an *auth* session. Bare ``session`` was in the
    # line above, and in a swim-training product every other request mentions
    # training sessions — which meant almost everything needed an approval, and
    # an approval that appears on everything is one the operator learns to click
    # through without reading. The path patterns still catch
    # ``src/lib/auth/session.ts`` whatever the request called it, and paths are
    # the stronger signal of the two.
    r"\b(user|auth\w*|login|browser|http|cookie|admin)\s+sessions?\b",
    r"\bsessions?\s+(token|cookie|id|store|storage|management|expir\w+|"
    r"timeout|hijack\w*)\b",
    r"\bsession[_-]?(id|token|cookie)\b",
    r"\b(payment|billing|stripe|checkout|subscription|invoice)\w*",
    r"\b(migration|schema|database table|rls|row level security)\w*",
    r"\b(secret|credential|api key|private key)\w*",
    r"\b(delete|drop|purge|wipe|destroy|truncate)\b",
    r"\b(health|biometric|medical|patient)\s+(data|record)",
    r"\b(security|firewall|cors|csrf)\b",
    # Authorisation as an operator actually phrases it. Nobody asks to "modify
    # the RBAC policy"; they ask to change who can see something. Without these
    # the classifier reads the most consequential request Wiz can receive as an
    # ordinary UI change, because none of the words above appear in it.
    r"\bwho\s+(can|may|is\s+allowed|are\s+allowed|should\s+(?:be\s+able|see))\b",
    r"\ballowed\s+to\s+(see|view|read|edit|change|access|download|export)\b",
    r"\baccess\s+(to|control|level)\b",
    r"\b(visible|visibility)\s+(to|for)\b",
    r"\b(share|shares|sharing|shared)\b.{0,30}\b(with|between|across)\b",
    r"\b(private|public)\s+(data|profile|record|page|link)\b",
    r"\b(other|another|others'?|other people'?s?)\s+\w*\s*(data|record|profile|"
    r"result|swimmer|athlete|user|account)",
)

#: Requests whose wording alone makes them at least MEDIUM.
MEDIUM_RISK_WORDS: List[Pattern[str]] = _patterns(
    r"\b(endpoint|api|route|handler|query|fetch|mutation)\w*",
    # The noun is allowed to be a little way from the verb: "add a new *coach*
    # dashboard" is the shape real requests arrive in, and a pattern demanding
    # adjacency silently classifies it LOW.
    r"\b(add|build|create|implement)\b.{0,40}?\b"
    r"(feature|page|dashboard|chart|graph|report|export|view|screen|component|table)\b",
    r"\b(refactor|rewrite|restructure|migrate)\b",
    r"\b(upload|download|import|export)\b",
)

#: Cues that mean the risky word right after them is being ruled *out*, not
#: asked for: "do not change authentication" versus "change authentication".
_NEGATION_CUES: Pattern[str] = re.compile(
    r"\b(do\s+not|don'?t|never|without|avoid(?:ing)?|must\s+not|"
    r"should\s*n'?t|won'?t|shall\s+not|cannot|can'?t|excluding|"
    r"except\s+for|nor)\b",
    re.IGNORECASE,
)

#: How far back a negation cue can sit and still be read as governing the
#: matched word. Long enough for a list like "do not change functionality,
#: authentication, data, APIs, or styling" — the cue is at the front and the
#: risky word can be several items later — short enough that a negation in an
#: earlier, unrelated sentence cannot reach forward and silence a real
#: request later in the same text.
_NEGATION_WINDOW = 80


def _is_prohibited(text: str, match: "re.Match[str]") -> bool:
    """Whether *match* sits inside an explicit prohibition, not a request.

    A model cannot self-classify its own change (see the module docstring),
    but an operator explicitly ruling something out is not the same signal as
    an operator asking for it, and treating them alike means every request
    that lists what must stay untouched — the safest possible request — reads
    as the most dangerous one. Handled as a bounded window immediately before
    the match, stopped at the nearest sentence boundary so a prohibition in
    one sentence cannot reach into the next.
    """
    start = max(0, match.start() - _NEGATION_WINDOW)
    window = text[start : match.start()]
    for boundary in (".", "!", "?"):
        idx = window.rfind(boundary)
        if idx != -1:
            window = window[idx + 1 :]
    return bool(_NEGATION_CUES.search(window))


_ORDER = {Risk.LOW: 0, Risk.MEDIUM: 1, Risk.HIGH: 2}


def _max(*risks: Optional[Risk]) -> Risk:
    present = [r for r in risks if r is not None]
    if not present:
        return Risk.LOW
    return max(present, key=lambda r: _ORDER[r])


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """A risk level, and the evidence for it.

    The reasons matter as much as the level: an operator asked to approve a
    HIGH-risk feature deserves to be told it was classified that way because it
    touches ``src/lib/auth/session.ts``, not merely that a classifier said so.
    """

    risk: Risk
    reasons: Tuple[str, ...] = ()

    @property
    def requires_approval(self) -> bool:
        return self.risk is Risk.HIGH

    def to_dict(self) -> dict:
        return {"risk": self.risk.value, "reasons": list(self.reasons)}


def classify_paths(paths: Iterable[str]) -> RiskAssessment:
    """Risk implied by the files a change touches.

    This is the classification that matters most, because it is derived from
    the diff rather than from anybody's description of the diff.
    """
    reasons: List[str] = []
    risk = Risk.LOW
    for path in paths:
        normalised = str(path).replace("\\", "/").strip()
        if not normalised:
            continue
        for pattern in HIGH_RISK_PATHS:
            if pattern.search(normalised):
                reasons.append(f"{normalised} is a sensitive path")
                risk = Risk.HIGH
                break
        else:
            for pattern in MEDIUM_RISK_PATHS:
                if pattern.search(normalised):
                    reasons.append(f"{normalised} changes application behaviour")
                    risk = _max(risk, Risk.MEDIUM)
                    break
    return RiskAssessment(risk=risk, reasons=tuple(dict.fromkeys(reasons)))


def classify_text(text: str) -> RiskAssessment:
    """Risk implied by what was asked for, before anything has been built.

    Used to decide whether a request can even be planned autonomously. It is
    the weaker signal of the two — wording is easy to get wrong in both
    directions — so it never lowers a path-based result.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return RiskAssessment(risk=Risk.LOW)

    reasons: List[str] = []
    risk = Risk.LOW
    for pattern in HIGH_RISK_WORDS:
        for match in pattern.finditer(cleaned):
            if _is_prohibited(cleaned, match):
                continue
            reasons.append(f"the request mentions '{match.group(0).strip()}'")
            risk = Risk.HIGH
            break
    if risk is not Risk.HIGH:
        for pattern in MEDIUM_RISK_WORDS:
            for match in pattern.finditer(cleaned):
                if _is_prohibited(cleaned, match):
                    continue
                reasons.append(f"the request mentions '{match.group(0).strip()}'")
                risk = _max(risk, Risk.MEDIUM)
                break
    return RiskAssessment(risk=risk, reasons=tuple(dict.fromkeys(reasons)))


def classify(
    *,
    text: str = "",
    paths: Sequence[str] = (),
    agent_opinion: Optional[Risk] = None,
) -> RiskAssessment:
    """The classification Wiz acts on.

    *agent_opinion* is whatever the coding agent said about its own change. It
    participates only through :func:`_max`, so it can raise the level and can
    never lower it. An agent that says "this is low risk" about a change to
    ``auth/session.ts`` is overruled by the path; an agent that says "careful,
    this is high risk" about a copy edit is believed, because being too careful
    is not a failure mode worth defending against.
    """
    from_text = classify_text(text)
    from_paths = classify_paths(paths)

    reasons = list(from_paths.reasons) + list(from_text.reasons)
    risk = _max(from_text.risk, from_paths.risk, agent_opinion)

    if (
        agent_opinion is not None
        and _ORDER[agent_opinion] > _ORDER[_max(from_text.risk, from_paths.risk)]
    ):
        reasons.append("the coding agent raised the risk itself")

    return RiskAssessment(risk=risk, reasons=tuple(dict.fromkeys(reasons)))
