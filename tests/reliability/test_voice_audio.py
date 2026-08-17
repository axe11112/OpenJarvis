"""Tests for browser-audio normalisation — the iPhone bug, pinned down.

Every spoken command from a real installed iPhone PWA returned "I didn't catch
that". The cause was not intent matching, transcription, or the network: the
page never uploaded anything, because it was encoding WAV from a Web Audio
graph that iOS leaves empty inside a PWA. The fix moves recording to
MediaRecorder — which hands back ``audio/mp4`` on iOS — and converts on the Mac.

So the load-bearing test here is the one that builds *real* AAC in an MP4
container with macOS's own encoder and asserts whisper can end up with
something readable. A hand-written fake header would have passed against the
broken code too.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import pytest

from openjarvis.reliability.voice.audio import (
    TARGET_CHANNELS,
    TARGET_RATE,
    TARGET_WIDTH,
    AudioNormalizer,
    sniff_container,
)
from openjarvis.reliability.voice.stt import WhisperTranscriber

MACOS_ONLY = pytest.mark.skipif(
    shutil.which("say") is None or shutil.which("afconvert") is None,
    reason="needs macOS `say` and `afconvert` to synthesise real audio",
)


def _speak(phrase: str, *, fmt: str) -> bytes:
    """Real audio in a real container, produced by the operating system.

    ``fmt`` is ``"m4a"`` (what an iPhone's MediaRecorder produces) or ``"wav"``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        aiff = directory / "s.aiff"
        subprocess.run(
            ["say", "-v", "Samantha", "-r", "180", "-o", str(aiff), phrase], check=True
        )
        target = directory / f"s.{fmt}"
        if fmt == "m4a":
            # AAC in an MP4 container: byte-for-byte the family iOS Safari
            # hands back from MediaRecorder.
            subprocess.run(
                ["afconvert", "-f", "m4af", "-d", "aac", str(aiff), str(target)],
                check=True,
            )
        else:
            subprocess.run(
                [
                    "afconvert",
                    "-f",
                    "WAVE",
                    "-d",
                    f"LEI16@{TARGET_RATE}",
                    "-c",
                    "1",
                    str(aiff),
                    str(target),
                ],
                check=True,
            )
        return target.read_bytes()


# ---------------------------------------------------------------------------
# Recognising what the browser sent
# ---------------------------------------------------------------------------


class TestContainerSniffing:
    def test_a_wav_is_recognised(self):
        header = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 8
        assert sniff_container(header) == ("wav", "audio/wav")

    def test_an_iphone_mp4_is_recognised(self):
        # `....ftypM4A ` — the box layout every MediaRecorder mp4 starts with.
        assert sniff_container(b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00")[0] == "mp4"

    def test_a_chrome_webm_is_recognised(self):
        assert sniff_container(b"\x1aE\xdf\xa3" + b"\x00" * 20)[0] == "webm"

    def test_a_firefox_ogg_is_recognised(self):
        assert sniff_container(b"OggS" + b"\x00" * 20)[0] == "ogg"

    def test_an_avi_is_not_mistaken_for_a_wav(self):
        """RIFF alone is not enough; AVI shares the prefix."""
        assert sniff_container(b"RIFF" + b"\x00" * 4 + b"AVI LIST")[0] == ""

    def test_nonsense_is_not_guessed_at(self):
        assert sniff_container(b"not audio at all, really") == ("", "")
        assert sniff_container(b"tiny") == ("", "")

    @MACOS_ONLY
    def test_real_macos_output_is_recognised(self):
        """The signatures match what an encoder actually writes."""
        assert sniff_container(_speak("hello", fmt="m4a"))[0] == "mp4"
        assert sniff_container(_speak("hello", fmt="wav"))[0] == "wav"


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


class TestNormalization:
    @MACOS_ONLY
    def test_real_iphone_audio_becomes_whisper_ready_wav(self):
        """The regression test for the bug. Real AAC in, real 16 kHz mono out."""
        audio = _speak("Is production healthy?", fmt="m4a")
        result = AudioNormalizer().normalize(audio)

        assert result.ok, result.reason
        assert result.source_container == "mp4"
        assert result.converter in ("afconvert", "ffmpeg")
        assert result.duration_seconds > 0.3

        with wave.open(__import__("io").BytesIO(result.wav), "rb") as handle:
            assert handle.getframerate() == TARGET_RATE
            assert handle.getnchannels() == TARGET_CHANNELS
            assert handle.getsampwidth() == TARGET_WIDTH
            assert handle.getnframes() > 0

    @MACOS_ONLY
    def test_a_correct_wav_is_passed_through_untouched(self):
        """No decoder is spawned for audio that is already right."""
        audio = _speak("hello", fmt="wav")
        result = AudioNormalizer().normalize(audio)
        assert result.converter == "passthrough"
        assert result.wav == audio

    @MACOS_ONLY
    def test_a_wrong_rate_wav_is_resampled(self):
        with tempfile.TemporaryDirectory() as tmp:
            aiff = Path(tmp) / "s.aiff"
            wav = Path(tmp) / "s.wav"
            subprocess.run(["say", "-o", str(aiff), "hello there"], check=True)
            # 44.1 kHz stereo: what a desktop browser might hand over.
            subprocess.run(
                [
                    "afconvert",
                    "-f",
                    "WAVE",
                    "-d",
                    "LEI16@44100",
                    "-c",
                    "2",
                    str(aiff),
                    str(wav),
                ],
                check=True,
            )
            result = AudioNormalizer().normalize(wav.read_bytes())

        assert result.ok and result.converter != "passthrough"
        with wave.open(__import__("io").BytesIO(result.wav), "rb") as handle:
            assert handle.getframerate() == TARGET_RATE
            assert handle.getnchannels() == TARGET_CHANNELS

    def test_empty_audio_fails_closed_with_a_reason(self):
        result = AudioNormalizer().normalize(b"")
        assert not result.ok
        assert "no audio" in result.reason

    def test_an_unknown_container_fails_closed_with_a_reason(self):
        result = AudioNormalizer().normalize(b"\x00\x01\x02\x03 this is not audio")
        assert not result.ok
        assert "unrecognised" in result.reason

    def test_a_decoder_that_fails_does_not_raise(self):
        class _Failed:
            returncode = 1
            stderr = "decode error"

        normalizer = AudioNormalizer(runner=lambda _argv: _Failed())
        result = normalizer.normalize(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 400)
        assert not result.ok
        assert "could not decode" in result.reason

    def test_capabilities_are_reported_honestly(self):
        caps = AudioNormalizer().capabilities()
        assert "wav" in caps["containers"]
        assert isinstance(caps["iphone_supported"], bool)
        if caps["afconvert"]:
            assert "mp4" in caps["containers"], "the iPhone container must be covered"


# ---------------------------------------------------------------------------
# Through the transcriber
# ---------------------------------------------------------------------------


MODEL = Path.home() / ".openjarvis" / "voice" / "models" / "ggml-tiny.en.bin"
NEEDS_WHISPER = pytest.mark.skipif(
    not MODEL.exists() or not WhisperTranscriber().binary,
    reason="needs whisper.cpp and the tiny.en model",
)


class TestEndToEndFromBrowserAudio:
    @MACOS_ONLY
    @NEEDS_WHISPER
    def test_iphone_container_transcribes(self):
        """The whole point: MP4/AAC in, words out.

        Before the fix this returned "" for every iPhone utterance, which the
        rest of the system correctly reported as "I didn't catch that".
        """
        transcriber = WhisperTranscriber(model_path=str(MODEL))
        heard = transcriber.transcribe(_speak("Is production healthy?", fmt="m4a"))
        assert heard, "iPhone audio must produce a transcript"
        assert "production" in heard.lower()

    @MACOS_ONLY
    @NEEDS_WHISPER
    def test_inspect_describes_the_conversion(self):
        transcriber = WhisperTranscriber(model_path=str(MODEL))
        info = transcriber.inspect(_speak("What happened?", fmt="m4a"))

        assert info["container"] == "mp4"
        assert "WAV" in info["format"]
        assert info["sample_rate"] == TARGET_RATE
        assert info["duration_seconds"] > 0.2
        assert not info.get("silent")
        assert "problem" not in info

    @MACOS_ONLY
    def test_inspect_names_silence_rather_than_blaming_the_words(self):
        """A live microphone that captured nothing must be distinguishable from
        speech nobody understood — they need completely different fixes."""
        with tempfile.TemporaryDirectory() as tmp:
            silent = Path(tmp) / "s.wav"
            with wave.open(str(silent), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(TARGET_RATE)
                handle.writeframes(b"\x00\x00" * TARGET_RATE)  # one second of nothing
            transcriber = WhisperTranscriber(model_path=str(MODEL))
            info = transcriber.inspect(silent.read_bytes())

        assert info["silent"] is True
        assert "silent" in info["problem"]
