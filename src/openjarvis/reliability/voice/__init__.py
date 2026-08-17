"""Sir Voice — a private, local, two-way voice interface to JARVIS.

Nothing here is a telephone. There is no carrier, no number and no paid
provider: the phone is a browser on the operator's own private network, the
audio never leaves the machine, and the whole system costs nothing to run.

Layers, from the microphone inwards:

``stt``
    Speech to text, locally, via whisper.cpp.
``intents``
    A transcript becomes one of a fixed set of intents, or nothing at all.
``commands``
    Intents are answered from recorded state, or executed from a narrow
    allowlist, or refused and parked for Control Center approval.
``answers``
    Spoken replies, assembled from stored facts only.
``tts``
    Text to speech, locally, via the operating system's own voice.
``confirmations``
    High-risk requests, awaiting a human at a screen.
``trigger``
    Whether an event is serious enough to ring the phone at all.

The seam that matters is between ``intents`` and ``commands``: what can be said
is a reviewable list, and what can be *done* is a smaller one.
"""

from __future__ import annotations

from openjarvis.reliability.voice.answers import VoiceFacts, answer_for
from openjarvis.reliability.voice.commands import CommandResult, VoiceCommands
from openjarvis.reliability.voice.confirmations import (
    ConfirmationStore,
    PendingConfirmation,
)
from openjarvis.reliability.voice.intents import (
    INTENTS,
    Intent,
    IntentMatch,
    Risk,
    match_intent,
)

__all__ = [
    "INTENTS",
    "CommandResult",
    "ConfirmationStore",
    "Intent",
    "IntentMatch",
    "PendingConfirmation",
    "Risk",
    "VoiceCommands",
    "VoiceFacts",
    "answer_for",
    "match_intent",
]
