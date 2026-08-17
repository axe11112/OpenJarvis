"""The microphone is working when a phone has been heard, and not before.

Every test here is written against the failure that shipped: a Control Center
showing voice ONLINE while every word spoken into the real iPhone came back as
"Sir, I didn't catch that."
"""

from __future__ import annotations

from openjarvis.reliability.voice.health import VoiceHealth
from openjarvis.reliability.voice.microphone import (
    FAILED,
    UNKNOWN,
    WORKING,
    MicrophoneRecord,
)


def _spoke(seconds=1.8, peak=8000, container="mp4", **extra):
    """A diagnosis for audio that plainly contains a person talking."""
    info = {
        "bytes": 40_000,
        "container": container,
        "format": f"{container} -> WAV via afconvert",
        "duration_seconds": seconds,
        "peak_amplitude": peak,
        "sample_rate": 16000,
        "channels": 1,
        "sample_width_bytes": 2,
        "silent": peak < 200,
    }
    info.update(extra)
    return info


def _silence():
    return _spoke(seconds=2.0, peak=12, silent=True, problem="silent: the "
                  "microphone produced no signal")


def _undecodable():
    return {
        "bytes": 38_000,
        "container": "mp4",
        "format": "mp4",
        "problem": "afconvert could not read the container",
    }


# ---------------------------------------------------------------------------
# The three states
# ---------------------------------------------------------------------------


def test_starts_unknown_not_working():
    record = MicrophoneRecord()
    assert record.state == UNKNOWN
    assert "no phone has spoken" in record.snapshot()["detail"]


def test_a_real_transcript_from_a_real_device_is_the_only_proof():
    record = MicrophoneRecord()
    record.observe(
        transcript="how are things",
        diagnosis=_spoke(),
        device="Mozilla/5.0 (iPhone; CPU iPhone OS 18_5)",
        source="device",
    )
    snapshot = record.snapshot()
    assert snapshot["state"] == WORKING
    assert "iPhone" in snapshot["detail"]


def test_a_synthesised_test_wav_proves_nothing():
    """The library working was never the thing in doubt."""
    record = MicrophoneRecord()
    record.observe(transcript="how are things", diagnosis=_spoke(), source="test")
    assert record.state == UNKNOWN


def test_speaking_into_it_and_getting_nothing_is_a_failure():
    record = MicrophoneRecord()
    for _ in range(3):
        record.observe(transcript="", diagnosis=_spoke(), source="device")
    snapshot = record.snapshot()
    assert snapshot["state"] == FAILED
    assert "no words came back" in snapshot["detail"]


def test_one_bad_utterance_does_not_condemn_the_microphone():
    record = MicrophoneRecord()
    record.observe(transcript="how are things", diagnosis=_spoke(), source="device")
    record.observe(transcript="", diagnosis=_spoke(), source="device")
    assert record.state == WORKING


def test_it_stops_working_and_says_so():
    record = MicrophoneRecord()
    record.observe(transcript="how are things", diagnosis=_spoke(), source="device")
    for _ in range(3):
        record.observe(transcript="", diagnosis=_spoke(), source="device")
    assert record.state == FAILED


def test_silence_is_not_a_failure():
    """Opening the page and not speaking must not turn the panel red."""
    record = MicrophoneRecord()
    for _ in range(5):
        record.observe(transcript="", diagnosis=_silence(), source="device")
    assert record.state == UNKNOWN
    assert "no sound has arrived" in record.snapshot()["detail"]


def test_a_short_tap_is_not_a_failure():
    record = MicrophoneRecord()
    tapped = _spoke(seconds=0.1, problem="shorter than 0.3s — nothing to transcribe")
    for _ in range(5):
        record.observe(transcript="", diagnosis=tapped, source="device")
    assert record.state == UNKNOWN


def test_audio_that_cannot_be_decoded_is_a_failure_of_the_path():
    """The owner cannot fix this by speaking louder, so it is not their fault."""
    record = MicrophoneRecord()
    for _ in range(3):
        record.observe(transcript="", diagnosis=_undecodable(), source="device")
    assert record.state == FAILED
    assert "afconvert" in record.snapshot()["detail"]


def test_an_unmeasurable_turn_cannot_break_a_working_microphone():
    record = MicrophoneRecord()
    record.observe(transcript="how are things", diagnosis=_spoke(), source="device")
    for _ in range(5):
        record.observe(transcript="", diagnosis={}, source="device")
    assert record.state == WORKING


def test_recovery_clears_the_failure():
    record = MicrophoneRecord()
    for _ in range(3):
        record.observe(transcript="", diagnosis=_spoke(), source="device")
    assert record.state == FAILED
    record.observe(transcript="how are things", diagnosis=_spoke(), source="device")
    assert record.state == WORKING


def test_no_audio_and_no_transcript_text_reach_disk(tmp_path):
    path = tmp_path / "microphone.json"
    record = MicrophoneRecord(path=path)
    record.observe(
        transcript="merge the pull request for the billing outage",
        diagnosis=_spoke(),
        device="iPhone",
        source="device",
    )
    written = path.read_text(encoding="utf-8")
    assert "billing" not in written
    assert "merge" not in written
    assert '"last_success_words": 8' in written


def test_the_answer_survives_a_restart(tmp_path):
    path = tmp_path / "microphone.json"
    MicrophoneRecord(path=path).observe(
        transcript="how are things", diagnosis=_spoke(), device="iPhone",
        source="device",
    )
    assert MicrophoneRecord(path=path).state == WORKING


def test_a_corrupt_record_reads_as_unknown_not_working(tmp_path):
    path = tmp_path / "microphone.json"
    path.write_text("{oh no", encoding="utf-8")
    assert MicrophoneRecord(path=path).state == UNKNOWN


# ---------------------------------------------------------------------------
# The panel — the actual release-blocking rule
# ---------------------------------------------------------------------------


class _Ready:
    available = True
    model_path = "/models/ggml-tiny.en.bin"
    voice = "Daniel"

    def capabilities(self):
        return {"iphone_supported": True, "containers": ["wav", "mp4", "webm"]}


class _Phones:
    def all(self):
        return [type("S", (), {"added_at": "2026-08-17T06:00:00Z"})()]


class _Calls:
    def snapshot(self):
        return {"can_ring": True}


class _Access:
    tailscale_enabled = True
    tailscale_host = "mac.tailnet.ts.net"


def _health(microphone):
    return VoiceHealth(
        transcriber=_Ready(),
        speech=_Ready(),
        normalizer=_Ready(),
        microphone=microphone,
        subscriptions=_Phones(),
        calls=_Calls(),
        access=_Access(),
        tailscale_runner=lambda *a, **k: type("P", (), {"returncode": 0})(),
    )


def test_voice_is_not_online_merely_because_whisper_is_installed():
    """The release-blocking rule, stated as a test.

    Every component below is healthy. No phone has ever been heard. This must
    not read ONLINE, because that is precisely the lie the owner was shown.
    """
    snapshot = _health(MicrophoneRecord()).snapshot()
    assert snapshot["parts"]["stt"]["state"] == "READY"
    assert snapshot["parts"]["microphone"]["state"] == UNKNOWN
    assert snapshot["voice"] != "ONLINE"


def test_voice_goes_online_once_a_real_phone_has_been_heard():
    record = MicrophoneRecord()
    record.observe(
        transcript="how are things", diagnosis=_spoke(), device="iPhone",
        source="device",
    )
    assert _health(record).snapshot()["voice"] == "ONLINE"


def test_a_broken_microphone_takes_voice_offline():
    record = MicrophoneRecord()
    for _ in range(3):
        record.observe(transcript="", diagnosis=_spoke(), source="device")
    assert _health(record).snapshot()["voice"] == "OFFLINE"


def test_the_engine_and_the_microphone_are_reported_separately():
    """Two lights, because they fail independently and mean different things."""
    parts = _health(MicrophoneRecord()).snapshot()["parts"]
    assert parts["stt"]["state"] == "READY"
    assert parts["microphone"]["state"] == UNKNOWN
    assert parts["stt"] is not parts["microphone"]
