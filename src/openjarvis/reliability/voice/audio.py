"""Turning whatever a browser recorded into what whisper.cpp can read.

The first version of Sir Voice asked the browser to hand over 16 kHz mono WAV
directly, encoded in JavaScript from raw Web Audio samples. That avoided
needing any decoder on the Mac, and on a real iPhone it produced silence: iOS
Safari's ``ScriptProcessor`` frequently delivers empty buffers inside an
installed PWA, so the page uploaded nothing at all and the operator heard "I
didn't catch that" forever.

The lesson is not "fix ScriptProcessor". It is that the browser should be
allowed to record the way it wants to. Every browser has one recording path it
genuinely supports — ``MediaRecorder`` — and each produces a different
container:

===============  =========================================================
iOS Safari       ``audio/mp4`` (AAC)
Chrome/Android   ``audio/webm`` (Opus)
Firefox          ``audio/ogg`` (Opus)
===============  =========================================================

whisper.cpp reads none of them. So the normalisation happens here, on the Mac,
where there is a decoder already installed and no per-device variation to chase:

* ``afconvert`` ships with macOS and reads AAC/MP4/M4A/CAF/MP3/WAV. That covers
  the iPhone, which is the device this exists for, with zero dependencies.
* ``ffmpeg`` is used when present, because it also reads WebM/Opus and Ogg —
  the Android and desktop-Chrome cases. Optional on purpose: the system must
  not stop working for the iPhone because a Homebrew formula is missing.

Audio touches the disk only inside a temporary directory that is removed on
every path out of this module, including failure. Nothing is kept.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["AudioNormalizer", "NormalizedAudio", "sniff_container"]

#: What whisper.cpp wants, and the only thing this module ever emits.
TARGET_RATE = 16000
TARGET_CHANNELS = 1
TARGET_WIDTH = 2

#: Container signatures, by the bytes a decoder would look at anyway. Sniffed
#: rather than trusting the request's Content-Type: the browser's declared MIME
#: and what it actually encoded do diverge, and the bytes cannot lie.
_SIGNATURES = (
    (b"RIFF", 0, "wav", "audio/wav"),
    (b"\x1aE\xdf\xa3", 0, "webm", "audio/webm"),
    (b"OggS", 0, "ogg", "audio/ogg"),
    (b"ftyp", 4, "mp4", "audio/mp4"),
    (b"caff", 0, "caf", "audio/x-caf"),
    (b"ID3", 0, "mp3", "audio/mpeg"),
    (b"\xff\xfb", 0, "mp3", "audio/mpeg"),
)


def sniff_container(data: bytes) -> Tuple[str, str]:
    """Return ``(extension, mime)`` for *data*, or ``("", "")`` if unknown."""
    if len(data) < 12:
        return "", ""
    for signature, offset, extension, mime in _SIGNATURES:
        if data[offset : offset + len(signature)] == signature:
            # RIFF alone is not enough — AVI is also RIFF.
            if extension == "wav" and data[8:12] != b"WAVE":
                continue
            return extension, mime
    return "", ""


@dataclass
class NormalizedAudio:
    """The result of trying to make an utterance readable."""

    wav: bytes = b""
    #: What arrived, e.g. ``"mp4"``.
    source_container: str = ""
    #: How it was converted: ``"passthrough"``, ``"afconvert"``, ``"ffmpeg"``.
    converter: str = ""
    reason: str = ""
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """Whether there is audio whisper can actually read."""
        return bool(self.wav)

    def to_dict(self) -> Dict[str, Any]:
        """Metadata for the audit record and the Control Center. No audio."""
        return {
            "source_container": self.source_container,
            "converter": self.converter,
            "bytes": len(self.wav),
            "duration_seconds": round(self.duration_seconds, 2),
            "reason": self.reason,
        }


@dataclass
class AudioNormalizer:
    """Converts browser recordings to 16 kHz mono WAV.

    Parameters
    ----------
    runner:
        Injected for tests, so the conversion path can be exercised without
        spawning a decoder.
    """

    timeout_seconds: float = 30.0
    runner: Optional[Callable[..., Any]] = None

    # -- availability -----------------------------------------------------

    @property
    def afconvert(self) -> str:
        return shutil.which("afconvert") or ""

    @property
    def ffmpeg(self) -> str:
        return shutil.which("ffmpeg") or ""

    def capabilities(self) -> Dict[str, Any]:
        """What this Mac can decode, for the health panel."""
        containers = ["wav"]
        if self.afconvert:
            containers += ["mp4", "m4a", "caf", "mp3"]
        if self.ffmpeg:
            containers += ["webm", "ogg"]
        return {
            "afconvert": bool(self.afconvert),
            "ffmpeg": bool(self.ffmpeg),
            "containers": sorted(set(containers)),
            # The iPhone is the device this exists for; say plainly whether it
            # is covered rather than leaving it to be inferred from a list.
            "iphone_supported": bool(self.afconvert) or bool(self.ffmpeg),
        }

    # -- the conversion ---------------------------------------------------

    def normalize(self, data: bytes) -> NormalizedAudio:
        """Return *data* as 16 kHz mono WAV, converting if needed.

        Never raises. A container nobody can decode is a
        :class:`NormalizedAudio` with no audio and a reason saying so — which
        the caller turns into "I didn't catch that", exactly as it would for
        silence. Failing closed is the same answer either way; the difference is
        that the reason is now recorded.
        """
        if not data:
            return NormalizedAudio(reason="no audio was uploaded")

        extension, _mime = sniff_container(data)
        if not extension:
            return NormalizedAudio(
                source_container="unknown",
                reason=f"unrecognised audio container (starts with {data[:4]!r})",
            )

        if extension == "wav" and self._already_correct(data):
            return NormalizedAudio(
                wav=data,
                source_container="wav",
                converter="passthrough",
                duration_seconds=self._duration(data),
            )

        return self._convert(data, extension)

    def _already_correct(self, data: bytes) -> bool:
        """Whether a WAV is already exactly what whisper wants."""
        try:
            import io
            import wave

            with wave.open(io.BytesIO(data), "rb") as handle:
                return (
                    handle.getframerate() == TARGET_RATE
                    and handle.getnchannels() == TARGET_CHANNELS
                    and handle.getsampwidth() == TARGET_WIDTH
                )
        except Exception:  # noqa: BLE001 - a WAV we cannot parse gets converted
            return False

    def _convert(self, data: bytes, extension: str) -> NormalizedAudio:
        tools = self._tools_for(extension)
        if not tools:
            return NormalizedAudio(
                source_container=extension,
                reason=(
                    f"no decoder for {extension} on this machine"
                    + ("" if self.ffmpeg else "; `brew install ffmpeg` adds webm/ogg")
                ),
            )

        with tempfile.TemporaryDirectory(prefix="sirvoice-audio-") as tmp:
            source = Path(tmp) / f"in.{extension}"
            target = Path(tmp) / "out.wav"
            source.write_bytes(data)

            for name, argv in tools:
                try:
                    proc = (self.runner or self._run)(argv(source, target))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("voice: %s could not run: %s", name, exc)
                    continue
                if getattr(proc, "returncode", 1) == 0 and target.exists():
                    wav = target.read_bytes()
                    if wav:
                        return NormalizedAudio(
                            wav=wav,
                            source_container=extension,
                            converter=name,
                            duration_seconds=self._duration(wav),
                        )
                logger.warning(
                    "voice: %s failed on %s: %s",
                    name,
                    extension,
                    (getattr(proc, "stderr", "") or "")[:200],
                )

        return NormalizedAudio(
            source_container=extension,
            reason=f"could not decode {extension} audio",
        )

    def _tools_for(self, extension: str):
        """Decoders to try, best first."""
        tools = []
        # afconvert first for Apple containers: it is already installed, and on
        # this Mac it is the only decoder guaranteed to exist.
        if self.afconvert and extension in ("mp4", "m4a", "caf", "mp3", "wav"):
            tools.append(("afconvert", self._afconvert_argv))
        if self.ffmpeg:
            tools.append(("ffmpeg", self._ffmpeg_argv))
        if self.afconvert and not tools:
            tools.append(("afconvert", self._afconvert_argv))
        return tools

    def _afconvert_argv(self, source: Path, target: Path):
        return [
            self.afconvert,
            "-f",
            "WAVE",
            "-d",
            f"LEI16@{TARGET_RATE}",
            "-c",
            str(TARGET_CHANNELS),
            str(source),
            str(target),
        ]

    def _ffmpeg_argv(self, source: Path, target: Path):
        return [
            self.ffmpeg,
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-ac",
            str(TARGET_CHANNELS),
            "-ar",
            str(TARGET_RATE),
            "-sample_fmt",
            "s16",
            "-y",
            str(target),
        ]

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
    def _duration(wav: bytes) -> float:
        try:
            import io
            import wave

            with wave.open(io.BytesIO(wav), "rb") as handle:
                rate = handle.getframerate()
                return handle.getnframes() / rate if rate else 0.0
        except Exception:  # noqa: BLE001
            return 0.0
