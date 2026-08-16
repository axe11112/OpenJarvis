"""What a spoken sentence is allowed to mean.

Speech recognition turns audio into a string. This module turns that string into
one of a fixed set of intents, or into nothing. There is no path from an
utterance to an arbitrary action: an intent that is not in :data:`INTENTS` does
not exist, and a sentence that matches none of them is answered with "I can't do
that", not guessed at.

That is a deliberate refusal of the obvious design. Handing the transcript to a
language model and letting it choose a function would be less code and would
handle phrasings this module misses. It would also mean the set of things a
voice can trigger is decided at runtime, by a model, from audio — where the
audio is whatever a microphone in a room picked up. Every safety property of
this system rests on the action set being *enumerable and reviewable*, and a
model that can be argued into a tool call is neither.

So matching is deterministic: lowercase, strip, and look for phrases. The cost
is that unusual phrasings fall through to "I don't understand", which is a bad
minute for the operator and a safe one for production.

Risk is a property of the intent, not of how it was asked for. ``READ`` and
``SAFE`` intents run immediately. ``CONFIRM`` intents never run from voice at
all — they raise a pending confirmation in the Control Center, where a human
with a screen and a mouse decides. Saying "merge it" more insistently does not
promote it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

__all__ = [
    "INTENTS",
    "Intent",
    "IntentMatch",
    "Risk",
    "match_intent",
]


class Risk(str, Enum):
    """How much authority carrying out an intent needs."""

    #: Answers a question from recorded state. Changes nothing.
    READ = "READ"
    #: Changes something, but nothing that can reach production or relax a
    #: control. Stopping repairs and engaging the emergency stop live here:
    #: both make the system *less* powerful, which is why a voice may do them.
    SAFE = "SAFE"
    #: Would grant or use production authority. Voice can ask; only the Control
    #: Center can approve.
    CONFIRM = "CONFIRM"


@dataclass(frozen=True)
class Intent:
    """One thing a voice may ask for."""

    name: str
    risk: Risk
    #: Phrases that select this intent. Matched as substrings against the
    #: normalised transcript, so "could you run the diagnostic please" hits
    #: "run the diagnostic".
    phrases: Tuple[str, ...]
    #: What Sir says when refusing a CONFIRM intent, or "" for the default.
    description: str = ""


#: The complete set. Adding an entry here is the only way to make a new thing
#: sayable, and doing so is a reviewable diff rather than a prompt change.
INTENTS: Tuple[Intent, ...] = (
    # -- READ -------------------------------------------------------------
    Intent(
        "status",
        Risk.READ,
        (
            "what's the status",
            "whats the status",
            "current status",
            "how are things",
            "how is everything",
            "everything ok",
            "everything okay",
            "status report",
        ),
        "the overall state",
    ),
    Intent(
        "production_status",
        Risk.READ,
        (
            "is production down",
            "is the site down",
            "is production up",
            "production status",
            "is the website down",
            "is it down",
        ),
        "whether production is healthy",
    ),
    Intent(
        "incidents",
        Risk.READ,
        (
            "current incidents",
            "open incidents",
            "any incidents",
            "what incidents",
            "list incidents",
        ),
        "the open incidents",
    ),
    Intent(
        "what_happened",
        Risk.READ,
        (
            "what happened",
            "explain what happened",
            "what went wrong",
            "tell me what happened",
            "what's wrong",
            "whats wrong",
        ),
        "what happened",
    ),
    Intent(
        "what_did_you_try",
        Risk.READ,
        (
            "what did you try",
            "what have you tried",
            "what did you do",
            "how did you try to fix",
        ),
        "what was attempted",
    ),
    Intent(
        "did_you_change_production",
        Risk.READ,
        (
            "did you change production",
            "did you touch production",
            "did you deploy",
            "did anything go live",
            "is anything live",
        ),
        "whether production changed",
    ),
    Intent(
        "deployment_failure",
        Risk.READ,
        (
            "what failed in the deployment",
            "why did the deployment fail",
            "what failed in the deploy",
            "deployment failure",
        ),
        "the deployment failure",
    ),
    Intent(
        "probe_status",
        Risk.READ,
        (
            "probe status",
            "how are the checks",
            "what checks are failing",
            "which checks are failing",
            "are the checks passing",
        ),
        "the checks",
    ),
    Intent(
        "diagnostic_result",
        Risk.READ,
        ("latest diagnostic", "last diagnostic", "diagnostic result"),
        "the last diagnostic",
    ),
    # -- SAFE -------------------------------------------------------------
    Intent(
        "run_diagnostic",
        Risk.SAFE,
        (
            "run the diagnostic",
            "run a diagnostic",
            "run diagnostics",
            "check everything again",
            "run the check again",
        ),
        "run a read-only diagnostic",
    ),
    Intent(
        "rerun_probe",
        Risk.SAFE,
        (
            "rerun the probe",
            "re-run the probe",
            "run the probe again",
            "check it again",
            "test it again",
        ),
        "re-run a check",
    ),
    Intent(
        "stop_repairs",
        Risk.SAFE,
        (
            "stop automatic repairs",
            "stop repairing",
            "stop the repairs",
            "pause repairs",
            "pause automatic repair",
            "stop fixing things",
        ),
        "stop automatic repairs",
    ),
    Intent(
        "emergency_stop",
        Risk.SAFE,
        ("emergency stop", "stop everything", "shut it down", "halt everything"),
        "engage the emergency stop",
    ),
    Intent(
        "restart_watcher",
        Risk.SAFE,
        (
            "restart the watcher",
            "restart yourself",
            "restart monitoring",
            "start the watcher",
        ),
        "restart the watcher",
    ),
    Intent(
        "hand_over",
        Risk.SAFE,
        (
            "leave this incident for me",
            "leave it for me",
            "i'll take it",
            "ill take it",
            "hand it to me",
            "leave it to me",
            "don't touch it",
            "dont touch it",
        ),
        "leave the incident for you",
    ),
    # -- CONFIRM ----------------------------------------------------------
    Intent(
        "merge",
        Risk.CONFIRM,
        (
            "merge the pr",
            "merge it",
            "merge the pull request",
            "ship it",
            "approve the pr",
        ),
        "merge a pull request",
    ),
    Intent(
        "override_verification",
        Risk.CONFIRM,
        (
            "override the verification",
            "ignore the checks",
            "override the checks",
            "force it through",
            "skip verification",
        ),
        "override a failed verification",
    ),
    Intent(
        "push_to_main",
        Risk.CONFIRM,
        ("push to main", "push it to main", "commit to main"),
        "push to the main branch",
    ),
    Intent(
        "enable_deploy",
        Risk.CONFIRM,
        (
            "enable deployment",
            "enable production deployment",
            "deploy to production",
            "turn on deployment",
            "deploy it",
        ),
        "enable production deployment",
    ),
    Intent(
        "enable_database_writes",
        Risk.CONFIRM,
        (
            "enable database writes",
            "enable supabase writes",
            "allow database writes",
            "write to the database",
        ),
        "enable database writes",
    ),
    Intent(
        "change_secrets",
        Risk.CONFIRM,
        (
            "change the token",
            "rotate the token",
            "change the secret",
            "update the credentials",
            "change the api key",
        ),
        "change a credential",
    ),
    Intent(
        "disable_security",
        Risk.CONFIRM,
        (
            "disable boundary guard",
            "disable boundaryguard",
            "turn off redaction",
            "disable the security",
            "turn off the safety",
            "disable the gates",
            "turn off the checks permanently",
        ),
        "turn off a security control",
    ),
    Intent(
        "branch_protection",
        Risk.CONFIRM,
        (
            "change branch protection",
            "disable branch protection",
            "remove branch protection",
        ),
        "change branch protection",
    ),
    # -- conversation -----------------------------------------------------
    Intent(
        "goodbye",
        Risk.READ,
        (
            "goodbye",
            "good bye",
            "that's all",
            "thats all",
            "hang up",
            "bye",
            "nothing else",
            "we're done",
            "were done",
        ),
        "end the call",
    ),
)

#: Phrases that end the call, hoisted so the session loop need not re-derive it.
GOODBYE = "goodbye"

_INTENTS_BY_NAME: Dict[str, Intent] = {intent.name: intent for intent in INTENTS}

#: Collapse anything that is not a letter, digit or apostrophe. Whisper output
#: carries punctuation and capitalisation that would otherwise defeat a
#: substring match.
_NOISE = re.compile(r"[^a-z0-9' ]+")


@dataclass
class IntentMatch:
    """The result of interpreting one utterance."""

    intent: Optional[Intent] = None
    transcript: str = ""
    normalised: str = ""
    #: Every intent whose phrases matched, longest phrase first. Kept for the
    #: audit record: "what else could this have meant" is a fair question after
    #: a voice command runs.
    candidates: List[str] = field(default_factory=list)

    @property
    def understood(self) -> bool:
        """Whether anything at all was recognised."""
        return self.intent is not None

    @property
    def name(self) -> str:
        """The matched intent's name, or ``""``."""
        return self.intent.name if self.intent is not None else ""

    @property
    def risk(self) -> Optional[Risk]:
        """The matched intent's risk, or ``None``."""
        return self.intent.risk if self.intent is not None else None


def normalise(transcript: str) -> str:
    """Reduce an utterance to the form phrases are matched against."""
    lowered = (transcript or "").strip().lower()
    return " ".join(_NOISE.sub(" ", lowered).split())


def match_intent(transcript: str) -> IntentMatch:
    """Interpret *transcript*, or return an unmatched result.

    The longest matching phrase wins. Without that rule "stop everything" would
    be ambiguous the moment a shorter phrase like "stop" existed, and the
    ambiguity would resolve by dictionary order — which is to say, by accident.
    """
    normalised = normalise(transcript)
    if not normalised:
        return IntentMatch(transcript=transcript, normalised="")

    hits: List[Tuple[int, Intent]] = []
    for intent in INTENTS:
        for phrase in intent.phrases:
            if phrase in normalised:
                hits.append((len(phrase), intent))
                break

    if not hits:
        return IntentMatch(transcript=transcript, normalised=normalised)

    hits.sort(key=lambda pair: pair[0], reverse=True)
    return IntentMatch(
        intent=hits[0][1],
        transcript=transcript,
        normalised=normalised,
        candidates=[intent.name for _, intent in hits],
    )


def intent_named(name: str) -> Optional[Intent]:
    """Look one up by name, for replaying an audited decision."""
    return _INTENTS_BY_NAME.get(name)
