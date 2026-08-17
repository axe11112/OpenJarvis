"""One conversation, from answer to goodbye.

A session is deliberately turn-based rather than a continuous stream. The
browser decides when the operator has finished speaking — it is the thing
holding the microphone, and a short energy-based silence detector there is both
simpler and more responsive than shipping every frame to the Mac and deciding
here. Each turn is therefore one complete utterance in, one spoken answer out.

That shape buys three things worth more than streaming's lower latency for a
command interface: the audio for a turn either arrives whole or does not arrive,
so there is no half-heard sentence to act on; the whole exchange fits in a
plain HTTP POST, so the Control Center needs no WebSocket, no signalling server
and no new dependency; and every turn has an obvious place to be audited.

What is kept, and for how long, is the other half of the design. Audio is held
in memory for the length of one transcription and never written to disk by this
module. The transcript is kept as redacted text for the life of the call so the
operator can read what Sir heard, and is discarded when the call ends unless the
session was explicitly asked to retain it.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from openjarvis.reliability.types import now_iso
from openjarvis.reliability.voice.commands import CommandResult, VoiceCommands
from openjarvis.reliability.voice.intents import match_intent

logger = logging.getLogger(__name__)

__all__ = ["Turn", "VoiceSession", "VoiceSessionManager"]

#: A call nobody speaks into for this long is over.
DEFAULT_IDLE_TIMEOUT = 300.0

#: Hard ceiling on a single call, so a forgotten open tab cannot hold a session
#: — and a microphone — open indefinitely.
DEFAULT_MAX_DURATION = 1800.0

#: What Sir says first, before the operator has asked anything.
GREETING = "Sir, I'm here. What would you like to know?"


def _summarise(diagnosis: Dict[str, Any]) -> str:
    """One readable line describing an utterance, for the log."""
    if not diagnosis:
        return "(not inspected)"
    parts = [f"{diagnosis.get('bytes', 0)}B", str(diagnosis.get("format", "?"))]
    if diagnosis.get("sample_rate"):
        parts.append(
            f"{diagnosis['sample_rate']}Hz/{diagnosis.get('channels', '?')}ch/"
            f"{diagnosis.get('duration_seconds', 0)}s"
        )
    if "peak_dbfs" in diagnosis:
        parts.append(f"peak {diagnosis['peak_dbfs']}dBFS")
    if diagnosis.get("problem"):
        parts.append(f"PROBLEM: {diagnosis['problem']}")
    return " ".join(parts)


@dataclass
class Turn:
    """One exchange: what was heard, and what was said back."""

    at: str
    heard: str
    said: str
    intent: str = ""
    risk: str = ""
    executed: bool = False
    confirmation_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the live transcript and the audit record."""
        return {
            "at": self.at,
            "heard": self.heard,
            "said": self.said,
            "intent": self.intent,
            "risk": self.risk,
            "executed": self.executed,
            "confirmation_id": self.confirmation_id,
        }


@dataclass
class VoiceSession:
    """A single answered call."""

    id: str
    commands: VoiceCommands
    transcriber: Any = None
    speech: Any = None
    redact: Optional[Callable[[str], str]] = None
    clock: Callable[[], float] = time.monotonic
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT
    max_duration: float = DEFAULT_MAX_DURATION
    started_at_monotonic: float = 0.0
    last_activity: float = 0.0
    ended: bool = False
    end_reason: str = ""
    turns: List[Turn] = field(default_factory=list)
    #: What the last utterance actually contained: container, sample rate,
    #: duration, peak level, raw transcript. Metadata only — never the audio.
    last_audio: Dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        now = self.clock()
        self.started_at_monotonic = self.started_at_monotonic or now
        self.last_activity = self.last_activity or now

    # -- lifecycle --------------------------------------------------------

    @property
    def expired(self) -> bool:
        """Whether this call has gone quiet or run too long."""
        now = self.clock()
        return (
            now - self.last_activity > self.idle_timeout
            or now - self.started_at_monotonic > self.max_duration
        )

    def end(self, reason: str = "ended") -> None:
        """Close the call. Idempotent."""
        with self._lock:
            if not self.ended:
                self.ended = True
                self.end_reason = reason
                logger.info("voice: session %s ended (%s)", self.id, reason)

    def greeting(self) -> str:
        """What Sir says on answering."""
        return GREETING

    # -- one turn ---------------------------------------------------------

    def hear(self, wav_bytes: bytes) -> Turn:
        """Transcribe *wav_bytes*, act on it, and return the exchange.

        Never raises. A turn that goes wrong produces a spoken apology, because
        an exception here reaches an operator holding a phone to their ear
        during an outage.
        """
        if self.ended:
            return self._turn("", "Sir, the call has ended.")

        self.last_activity = self.clock()
        heard = ""
        # Measured before transcription, and logged whatever happens. "I didn't
        # catch that" has several very different causes that look identical from
        # the outside; this is what tells them apart without a debugger on a
        # phone that is not here.
        diagnosis: Dict[str, Any] = {}
        try:
            if self.transcriber is not None:
                # Optional: a transcriber that cannot describe audio is still a
                # perfectly good transcriber, and losing the diagnosis must
                # never cost us the transcript.
                inspect = getattr(self.transcriber, "inspect", None)
                if callable(inspect):
                    try:
                        diagnosis = inspect(wav_bytes) or {}
                    except Exception:  # noqa: BLE001
                        logger.exception("voice: could not inspect the audio")
                heard = self.transcriber.transcribe(wav_bytes) or ""
        except Exception:  # noqa: BLE001 - unheard, never invented
            logger.exception("voice: transcription failed")
            heard = ""

        diagnosis["raw_transcript"] = heard
        self.last_audio = diagnosis
        logger.warning(
            "voice: utterance %s -> transcript=%r", _summarise(diagnosis), heard
        )

        heard = self._clean(heard)
        if not heard:
            return self._turn("", "Sir, I didn't catch that.")

        return self.say(heard)

    def say(self, heard: str) -> Turn:
        """Act on already-transcribed text. Split out so the loop is testable
        without audio, and so a typed message in the UI takes the same path."""
        result: CommandResult = self.commands.handle(match_intent(heard))
        turn = self._turn(
            heard,
            result.speech,
            intent=result.intent,
            risk=result.risk,
            executed=result.executed,
            confirmation_id=result.confirmation_id,
        )
        if result.ends_call:
            self.end("goodbye")
        return turn

    def audio_for(self, text: str) -> bytes:
        """Synthesised audio for *text*, or empty when there is no voice."""
        if self.speech is None:
            return b""
        try:
            return self.speech.synthesize(text)
        except Exception:  # noqa: BLE001 - a silent reply beats a crashed call
            logger.exception("voice: synthesis failed")
            return b""

    # -- internals --------------------------------------------------------

    def _clean(self, text: str) -> str:
        """Redact before the transcript is stored, shown or acted on.

        Order matters: this runs before intent matching, so a credential read
        aloud near the microphone never reaches the transcript the UI renders or
        the audit record the command layer writes.
        """
        if not text:
            return ""
        if self.redact is None:
            return text.strip()
        try:
            return (self.redact(text) or "").strip()
        except Exception:  # noqa: BLE001 - redaction failing is not a reason to
            logger.exception("voice: redaction failed; dropping the utterance")
            return ""  # ...pass the raw text through instead.

    def _turn(self, heard: str, said: str, **extra: Any) -> Turn:
        turn = Turn(at=now_iso(), heard=heard, said=said, **extra)
        with self._lock:
            self.turns.append(turn)
        return turn

    def transcript(self) -> List[Dict[str, Any]]:
        """The redacted conversation so far."""
        with self._lock:
            return [t.to_dict() for t in self.turns]


@dataclass
class VoiceSessionManager:
    """The open calls. Usually zero, occasionally one, never many.

    A cap exists because sessions hold a microphone permission on the far side
    and a transcription slot on this one; an unbounded map of them is a way for
    a reload loop to exhaust the machine that is supposed to be watching
    production.
    """

    factory: Callable[[str], VoiceSession]
    max_sessions: int = 3
    clock: Callable[[], float] = time.monotonic
    _sessions: Dict[str, VoiceSession] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def start(self) -> Optional[VoiceSession]:
        """Answer a call, or ``None`` when too many are already open."""
        with self._lock:
            self._reap_locked()
            if len(self._sessions) >= self.max_sessions:
                logger.warning(
                    "voice: refusing a call; %d already open", len(self._sessions)
                )
                return None
            session = self.factory(uuid.uuid4().hex[:12])
            # Keyed by the session's *own* id, not the one just generated. The
            # factory is allowed to mint its own — and if the two disagree, the
            # session is the one every other caller will quote back.
            self._sessions[session.id] = session
            logger.info("voice: session %s answered", session.id)
            return session

    def get(self, session_id: str) -> Optional[VoiceSession]:
        """An open session by id, or ``None``."""
        with self._lock:
            self._reap_locked()
            session = self._sessions.get(session_id)
        return None if session is None or session.ended else session

    def end(self, session_id: str, reason: str = "hung up") -> bool:
        """Hang up."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        session.end(reason)
        return True

    def open_sessions(self) -> int:
        """How many calls are live."""
        with self._lock:
            self._reap_locked()
            return len(self._sessions)

    def _reap_locked(self) -> None:
        """Drop finished and abandoned calls. Caller holds the lock."""
        for session_id, session in list(self._sessions.items()):
            if session.ended or session.expired:
                session.end("timed out" if not session.ended else session.end_reason)
                self._sessions.pop(session_id, None)
