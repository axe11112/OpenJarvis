"""Text to speech, on this machine, for nothing.

macOS ships a speech synthesiser that is already installed, already offline and
already free. For a system whose entire value proposition is that it can be
trusted with production, "already on the machine" beats "better sounding" —
there is no account, no key, no quota and no third party receiving a stream of
sentences about the operator's outages.

Two rules shape what gets spoken:

**Speak sentences, not records.** Anything reaching this module has already been
shortened by :mod:`~openjarvis.reliability.voice.answers`. The cap here is a
backstop: a voice reading a log for ninety seconds cannot be interrupted by
someone who just wants to know if the site is up.

**Never speak an identifier.** SHAs, deployment ids and tokens are unreadable
aloud and dangerous to recite. Redaction happens before synthesis, not after.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = ["MacSpeech", "speakable"]

#: Longest utterance that will be synthesised, in characters.
MAX_SPOKEN = 400

#: Things that must never be read aloud even if something upstream let them
#: through: long hex strings (commit SHAs), token-shaped words, and URLs.
_UNSPEAKABLE = (
    re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE),
    re.compile(r"\b(?:gh[pousr]|github_pat|sk|xox[baprs])_[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bdpl_[A-Za-z0-9]+\b"),
    re.compile(r"https?://\S+"),
)


def speakable(text: str) -> str:
    """Reduce *text* to something worth hearing.

    Not a security control — :class:`BoundaryGuard` is, and it runs first. This
    is the belt to that pair of braces, and it also handles the merely
    unlistenable: an answer that is technically safe and still forty seconds of
    hexadecimal.
    """
    cleaned = (text or "").strip()
    for pattern in _UNSPEAKABLE:
        cleaned = pattern.sub("", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > MAX_SPOKEN:
        cut = cleaned[:MAX_SPOKEN].rsplit(". ", 1)[0]
        cleaned = (cut or cleaned[:MAX_SPOKEN]).rstrip(".") + ". It's on the dashboard."
    return cleaned


@dataclass
class MacSpeech:
    """Synthesises speech with the operating system's own voice.

    ``say`` writes AIFF; the browser is happy with it, and converting would mean
    depending on ffmpeg for no gain the operator can hear.
    """

    voice: str = "Daniel"
    #: Words per minute. Slower than the default on purpose: this voice is heard
    #: by someone who has just been woken up.
    rate: int = 175
    binary: str = ""
    timeout_seconds: float = 30.0
    runner: Optional[Callable[..., Any]] = None

    def __post_init__(self) -> None:
        self.binary = self.binary or shutil.which("say") or ""

    @property
    def available(self) -> bool:
        """Whether speech can be synthesised here."""
        return bool(self.binary)

    def unavailable_reason(self) -> str:
        """Why not, for the diagnostic."""
        return "" if self.binary else "the macOS `say` command is not available"

    def synthesize(self, text: str) -> bytes:
        """Return AIFF audio for *text*, or ``b""``.

        Failure is empty audio rather than an exception: a call where the
        operator can hear nothing is bad, and a call that crashes the watcher
        is worse.
        """
        spoken = speakable(text)
        if not spoken or not self.available:
            return b""
        with tempfile.TemporaryDirectory(prefix="sirvoice-tts-") as tmp:
            out = Path(tmp) / "reply.aiff"
            argv = [
                self.binary,
                "-v",
                self.voice,
                "-r",
                str(self.rate),
                "-o",
                str(out),
                # The text is passed as one argv element, never through a shell,
                # so nothing in an answer can be read as a command.
                spoken,
            ]
            try:
                proc = (self.runner or self._run)(argv)
                if getattr(proc, "returncode", 1) != 0:
                    logger.warning(
                        "voice: say exited %s", getattr(proc, "returncode", "?")
                    )
                    return b""
                return out.read_bytes() if out.exists() else b""
            except Exception:  # noqa: BLE001
                logger.exception("voice: speech synthesis failed")
                return b""

    def _run(self, argv):
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            stdin=subprocess.DEVNULL,
            check=False,
        )
