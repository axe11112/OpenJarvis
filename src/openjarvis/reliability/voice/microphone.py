"""Whether a real phone has ever actually been heard.

This module exists because of a specific embarrassment. The Control Center
reported voice as ONLINE — whisper installed, model present, `say` working,
tailnet reachable, phone registered — while every single word spoken into the
real iPhone came back as "Sir, I didn't catch that." Every component was
healthy. The product did not work.

The mistake was treating *installed* as *working*. A transcriber that can load
a model is a transcriber that might work; the only evidence that the microphone
path works is a real device having sent real audio that produced a real
transcript. Nothing else is evidence, and a green light backed by anything else
is a guess printed in the colour of a fact.

So the microphone gets its own state, kept apart from the speech engine's:

``UNKNOWN``
    No phone has spoken yet, or the only attempts so far contained no sound.
    Deliberately not FAILED — the owner may simply not have tried, and calling
    that a failure is the same overclaim in the other direction.
``WORKING``
    A real device sent audio and a transcript came back. This is the only state
    that permits an ONLINE verdict.
``FAILED``
    A real device sent audio that *had sound in it* and no transcript came back,
    or the audio could not be decoded at all. The owner spoke and Sir did not
    hear them, which is the release-blocking condition.

The distinction between UNKNOWN and FAILED is drawn from the audio itself, not
from the outcome: a silent recording that yields nothing is a person who did
not speak, and a two-second recording at a healthy level that yields nothing is
a broken pipeline. The diagnosis that draws that line is already computed for
every turn by :meth:`~openjarvis.reliability.voice.stt.Transcriber.inspect`.

Only turns from a real device count. A synthetic WAV from the test suite proves
the library works, which was never the thing in doubt.

The record is persisted, because the question "has this ever actually worked?"
must survive the restart that follows every deploy — otherwise every update
resets the answer to UNKNOWN and the panel becomes useless exactly when someone
is checking whether the fix landed.

No audio is stored, and no transcript text: only its length in words. The
recording is the most private thing this system touches and it never reaches
disk.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from openjarvis.reliability.statefile import write_json_atomic
from openjarvis.reliability.types import now_iso

logger = logging.getLogger(__name__)

__all__ = ["MicrophoneRecord", "UNKNOWN", "WORKING", "FAILED", "microphone_path"]

UNKNOWN = "UNKNOWN"
WORKING = "WORKING"
FAILED = "FAILED"

#: Consecutive real-device failures before a previously working microphone is
#: called broken. One is a cough, a bump, a hand over the phone. Three in a row
#: is the pipeline.
FAILURES_BEFORE_BROKEN = 3

#: Below this peak sample a recording is silence, whatever its length. Matches
#: the threshold :meth:`Transcriber.inspect` uses for its own ``silent`` flag,
#: deliberately — two different definitions of silence in one pipeline is a bug
#: waiting for an evening.
SILENCE_PEAK = 200

#: Shorter than this and there was nothing to transcribe.
MINIMUM_SECONDS = 0.3

#: Fewer bytes than this is a tapped button, not a recording. Used only for the
#: case where the audio could not be decoded at all and there is no duration to
#: look at.
MINIMUM_BYTES = 2000


def microphone_path(config: Any) -> Path:
    """Where the record lives — beside the rest of the voice state."""
    return Path.home() / ".openjarvis" / "voice" / "microphone.json"


@dataclass
class MicrophoneRecord:
    """What real devices have actually managed to say.

    Parameters
    ----------
    path:
        Where to persist. ``None`` keeps it in memory, for tests.
    """

    path: Optional[Path] = None
    _data: Dict[str, Any] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self._load()

    # -- recording ---------------------------------------------------------

    def observe(
        self,
        *,
        transcript: str,
        diagnosis: Optional[Dict[str, Any]] = None,
        device: str = "",
        source: str = "device",
    ) -> str:
        """Record one turn and return the resulting state.

        ``source`` is ``"device"`` for audio that arrived over the network from
        a real phone and anything else for audio this process made up. Only the
        former is evidence.
        """
        if source != "device":
            return self.state
        diagnosis = dict(diagnosis or {})
        heard = (transcript or "").strip()
        with self._lock:
            self._data.setdefault("attempts", 0)
            self._data["attempts"] += 1
            self._data["last_attempt_at"] = now_iso()
            self._data["last_device"] = _short_device(device)
            self._data["last_container"] = str(diagnosis.get("container") or "unknown")

            if heard:
                self._data["last_success_at"] = now_iso()
                self._data["last_success_container"] = self._data["last_container"]
                self._data["last_success_words"] = len(heard.split())
                self._data["last_success_device"] = self._data["last_device"]
                self._data["consecutive_failures"] = 0
                self._data["successes"] = self._data.get("successes", 0) + 1
                self._data.pop("last_failure_reason", None)
            elif _had_sound(diagnosis):
                # The owner spoke and nothing came back. That is the failure.
                self._data["consecutive_failures"] = (
                    self._data.get("consecutive_failures", 0) + 1
                )
                self._data["last_failure_at"] = now_iso()
                self._data["last_failure_reason"] = _why(diagnosis)
            else:
                # Silence. Says nothing about the microphone either way, so it
                # must not be allowed to count as a failure — an operator who
                # opens the page and does not speak would otherwise turn the
                # panel red.
                self._data["silent_attempts"] = self._data.get("silent_attempts", 0) + 1
                self._data["last_silent_reason"] = _why(diagnosis)
        self._save()
        return self.state

    # -- reporting ---------------------------------------------------------

    @property
    def state(self) -> str:
        """UNKNOWN, WORKING or FAILED — never a guess."""
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        failures = self._data.get("consecutive_failures", 0)
        succeeded = bool(self._data.get("last_success_at"))
        if failures >= FAILURES_BEFORE_BROKEN:
            return FAILED
        if succeeded:
            return WORKING
        if failures:
            # Sound went in and nothing came out, more than zero times, and it
            # has never worked. Not yet conclusive, and certainly not healthy.
            return UNKNOWN
        return UNKNOWN

    def snapshot(self) -> Dict[str, Any]:
        """The panel's view: a state and a sentence explaining it."""
        with self._lock:
            state = self._state_locked()
            data = dict(self._data)
        return {
            "state": state,
            "detail": _detail(state, data),
            "attempts": data.get("attempts", 0),
            "successes": data.get("successes", 0),
            "consecutive_failures": data.get("consecutive_failures", 0),
            "last_success_at": data.get("last_success_at", ""),
            "last_attempt_at": data.get("last_attempt_at", ""),
            "last_device": data.get("last_device", ""),
            "last_container": data.get("last_container", ""),
        }

    def reset(self) -> None:
        """Forget everything. For a fresh verification after a change."""
        with self._lock:
            self._data = {}
        self._save()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._data = loaded
        except Exception:  # noqa: BLE001 - an unreadable record is UNKNOWN,
            logger.warning("voice: could not read %s", self.path)  # ...not a crash.

    def _save(self) -> None:
        if self.path is None:
            return
        write_json_atomic(self.path, self._data)


# ---------------------------------------------------------------------------
# Reading the diagnosis
# ---------------------------------------------------------------------------


def _had_sound(diagnosis: Dict[str, Any]) -> bool:
    """Whether this recording contained something a person could have said.

    Conservative in the direction that matters: when the measurements are
    missing or unreadable the answer is *no*, so an unmeasurable turn can never
    mark a working microphone broken. The one thing that does count without a
    measurement is audio that failed to decode at all — bytes arrived from a
    real phone and could not be turned into sound, which is a failure of the
    path and not of the speaker.
    """
    if not diagnosis:
        return False
    problem = str(diagnosis.get("problem") or "")
    if diagnosis.get("silent"):
        return False  # a live path in a quiet room proves nothing either way
    if "shorter than 0.3s" in problem:
        return False  # a tapped button, not a sentence
    if "duration_seconds" not in diagnosis:
        # Never decoded far enough to measure.
        return int(diagnosis.get("bytes") or 0) >= MINIMUM_BYTES
    try:
        seconds = float(diagnosis.get("duration_seconds") or 0.0)
        peak = int(diagnosis.get("peak_amplitude") or 0)
    except (TypeError, ValueError):
        return False
    return seconds >= MINIMUM_SECONDS and peak >= SILENCE_PEAK


def _why(diagnosis: Dict[str, Any]) -> str:
    """One line naming what was wrong with the audio, from measurements."""
    problem = str(diagnosis.get("problem") or "")
    if problem:
        return problem[:200]
    container = str(diagnosis.get("container") or diagnosis.get("format") or "unknown")
    if "duration_seconds" in diagnosis:
        return (
            f"{container}, {float(diagnosis['duration_seconds']):.1f}s at peak "
            f"{int(diagnosis.get('peak_amplitude') or 0)}, no words came back"
        )
    return f"{container}, {int(diagnosis.get('bytes') or 0)} bytes, no words came back"


def _short_device(device: str) -> str:
    """A user agent trimmed to the part worth showing, and length-capped."""
    agent = (device or "").strip()
    if not agent:
        return "unknown device"
    for marker in ("iPhone", "iPad", "Macintosh", "Android"):
        if marker in agent:
            return marker
    return agent[:60]


def _detail(state: str, data: Dict[str, Any]) -> str:
    """The sentence under the light. Always says what the evidence was."""
    if state == WORKING:
        when = str(data.get("last_success_at", ""))[:19]
        device = data.get("last_success_device") or "a phone"
        words = data.get("last_success_words", 0)
        return f"{device} was heard at {when} ({words} word(s))"
    if state == FAILED:
        reason = data.get("last_failure_reason") or "no transcript came back"
        failures = data.get("consecutive_failures", 0)
        return f"{failures} spoken attempt(s) produced nothing — {reason}"
    if data.get("consecutive_failures"):
        reason = data.get("last_failure_reason") or ""
        return f"spoken to, nothing transcribed yet — {reason}"
    if data.get("silent_attempts"):
        return (
            "a phone has connected but no sound has arrived yet "
            f"({data.get('last_silent_reason', '')})"
        )
    return "no phone has spoken to Sir yet"
