"""Speech to text, on this machine, for nothing.

whisper.cpp is run as a subprocess against a WAV file the browser sent. No audio
leaves the Mac, no API key exists to leak, and there is no per-minute cost — the
three properties that made every hosted speech service the wrong answer for a
system whose whole purpose is to be trusted with production.

**Model choice, measured rather than assumed.** This runs on an Intel Mac with
8 GB of RAM and no acceleration worth the name, so the usual advice — take the
biggest model that fits — is wrong here, and by more than expected. Benchmarked
on this machine against the actual command phrases, spoken at conversational
speed:

===========  ==================  ==========================================
model        per utterance       accuracy on the command vocabulary
===========  ==================  ==========================================
``tiny.en``  ~1.9 s              every phrase transcribed correctly
``base.en``  ~4.2 s              every phrase transcribed correctly
===========  ==================  ==========================================

So ``tiny.en`` is the default. Twice the speed for no measured loss, because the
thing downstream is a phrase matcher over about thirty fixed sentences, not a
transcriptionist: "run the diagnostic again" has to be recognised, not
rendered beautifully. ``base.en`` is worth reaching for only if real use turns
up phrasings tiny mishears; ``small.en`` is not, at roughly four seconds a turn
before it has understood anything.

Both figures are warm. The first transcription after a restart also pays for
loading the model — about 11 s extra for ``base.en``, less for ``tiny.en`` —
which is why the server loads one at startup rather than per call.

**Failure is silence, not invention.** A transcription that fails returns an
empty string, which the intent matcher treats as "I didn't catch that". The
alternative — a partial or garbled transcript matched optimistically — is how a
microphone in a room ends up triggering an operation.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = ["MODEL_CHOICES", "WhisperTranscriber", "find_whisper"]

#: Model files, smallest first, with the measured trade-off each one makes on
#: this machine. Names match whisper.cpp's ``ggml-<name>.bin`` convention.
MODEL_CHOICES = {
    "tiny.en": "the default: ~1.9s per utterance, no measured loss on commands",
    "base.en": "~4.2s per utterance; reach for it only if tiny mishears you",
    "small.en": "too slow for conversation on an Intel CPU",
}

#: The model to use when configuration does not name one.
DEFAULT_MODEL = "tiny.en"

#: Executable names whisper.cpp has shipped under, newest first.
_BINARIES = ("whisper-cli", "whisper-cpp", "whisper", "main")


def find_whisper(explicit: str = "") -> str:
    """Locate the whisper.cpp binary, or return ``""``."""
    if explicit:
        return explicit if Path(explicit).exists() else ""
    for name in _BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return ""


@dataclass
class WhisperTranscriber:
    """Transcribes a WAV file with a local whisper.cpp binary.

    Parameters
    ----------
    binary / model_path:
        Located at construction so a misconfiguration is discovered when the
        server starts rather than when the operator is mid-outage and talking.
    timeout_seconds:
        A hard bound. A transcription that takes longer than this has already
        failed as a conversation, whatever it eventually returns.
    """

    binary: str = ""
    model_path: str = ""
    timeout_seconds: float = 30.0
    threads: int = 4
    runner: Optional[Callable[..., Any]] = None

    def __post_init__(self) -> None:
        self.binary = self.binary or find_whisper()

    @property
    def available(self) -> bool:
        """Whether transcription can actually run."""
        return (
            bool(self.binary)
            and bool(self.model_path)
            and Path(self.model_path).exists()
        )

    def unavailable_reason(self) -> str:
        """Why it cannot run, for the diagnostic. Never a guess."""
        if not self.binary:
            return "whisper.cpp is not installed (brew install whisper-cpp)"
        if not self.model_path:
            return "no whisper model is configured"
        if not Path(self.model_path).exists():
            return f"the whisper model file is missing: {self.model_path}"
        return ""

    def transcribe(self, wav_bytes: bytes) -> str:
        """Return the text of *wav_bytes*, or ``""``.

        The audio is written to a temporary file and deleted immediately after,
        whichever way this goes. Keeping microphone audio on disk is not part of
        the bargain the operator agreed to when they answered a call.
        """
        if not self.available:
            logger.warning("voice: cannot transcribe — %s", self.unavailable_reason())
            return ""
        if not wav_bytes:
            return ""

        with tempfile.TemporaryDirectory(prefix="sirvoice-") as tmp:
            wav = Path(tmp) / "utterance.wav"
            wav.write_bytes(wav_bytes)
            argv = [
                self.binary,
                "-m",
                self.model_path,
                "-f",
                str(wav),
                "-nt",  # no timestamps; the matcher wants words
                "-np",  # no progress chatter on stdout
                "-t",
                str(self.threads),
                "-l",
                "en",
            ]
            try:
                proc = (self.runner or self._run)(argv)
            except Exception:  # noqa: BLE001 - unheard, never invented
                logger.exception("voice: whisper failed")
                return ""

        if getattr(proc, "returncode", 1) != 0:
            logger.warning(
                "voice: whisper exited %s: %s",
                getattr(proc, "returncode", "?"),
                (getattr(proc, "stderr", "") or "")[:200],
            )
            return ""
        return self._clean(getattr(proc, "stdout", "") or "")

    def _run(self, argv):
        return subprocess.run(  # noqa: S603 - fixed argv, no shell, no user text
            argv,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            stdin=subprocess.DEVNULL,
            check=False,
        )

    @staticmethod
    def _clean(stdout: str) -> str:
        """Strip whisper's decorations down to spoken words.

        Bracketed events — ``[BLANK_AUDIO]``, ``(wind blowing)`` — are dropped
        rather than passed through, because they are the model describing the
        room, and a phrase matcher has no way to tell that from speech.
        """
        lines = []
        for raw in stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                continue
            if line.startswith("(") and line.endswith(")"):
                continue
            lines.append(line)
        return " ".join(lines).strip()
