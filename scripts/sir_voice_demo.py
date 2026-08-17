#!/usr/bin/env python3
"""Hold a Sir Voice conversation on this machine, end to end, with no phone.

Speaks each question through the Mac's own speakers using ``say``, feeds that
audio to the local whisper.cpp exactly as the browser would, and plays Sir's
answer back. Nothing leaves the machine and nothing is paid for.

It exists because the browser transport — the ``/voice`` page, the installable
PWA and the incoming-call screen — needs HTTPS on a private origin, which needs
Tailscale. This proves the half that does not: transcription, understanding,
answering from real JARVIS state, refusing what a voice may not do, and speech.

    uv run --extra dev python scripts/sir_voice_demo.py
    uv run --extra dev python scripts/sir_voice_demo.py --speak   # hear it aloud
    uv run --extra dev python scripts/sir_voice_demo.py --live    # your real store

By default it builds a throwaway incident store so the demo is repeatable and
touches nothing. ``--live`` reads the real one, read-only, and still refuses
every high-risk request.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from openjarvis.reliability.store import IncidentStore  # noqa: E402
from openjarvis.reliability.types import (  # noqa: E402
    Incident,
    IncidentState,
    RepairAttempt,
    Severity,
)
from openjarvis.reliability.voice.answers import VoiceFacts  # noqa: E402
from openjarvis.reliability.voice.commands import VoiceCommands  # noqa: E402
from openjarvis.reliability.voice.confirmations import ConfirmationStore  # noqa: E402
from openjarvis.reliability.voice.session import VoiceSession  # noqa: E402
from openjarvis.reliability.voice.stt import (  # noqa: E402
    DEFAULT_MODEL,
    WhisperTranscriber,
)
from openjarvis.reliability.voice.tts import MacSpeech  # noqa: E402

MODELS = Path.home() / ".openjarvis/voice/models"

QUESTIONS = (
    "What's the current status?",
    "What happened?",
    "What did you try?",
    "Did you change production?",
    "Enable production deployment.",  # must be refused
    "Goodbye.",
)


def utterance(phrase: str, voice: str = "Samantha") -> bytes:
    """Speak *phrase* and return 16 kHz mono WAV — what the browser would send."""
    with tempfile.TemporaryDirectory() as tmp:
        aiff, wav = Path(tmp) / "q.aiff", Path(tmp) / "q.wav"
        subprocess.run(
            ["say", "-v", voice, "-r", "180", "-o", str(aiff), phrase], check=True
        )
        subprocess.run(
            [
                "afconvert",
                "-f",
                "WAVE",
                "-d",
                "LEI16@16000",
                "-c",
                "1",
                str(aiff),
                str(wav),
            ],
            check=True,
        )
        return wav.read_bytes()


def demo_store(directory: Path) -> IncidentStore:
    """A throwaway store with one realistic, unresolved incident."""
    store = IncidentStore(directory / "incidents.db")
    incident = store.create(
        Incident(
            fingerprint="fp_login",
            severity=Severity.HIGH,
            component="authentication",
            title="Login redirects back to /login",
            probe_id="auth-login",
        )
    )
    incident.resolution.root_cause = "the session cookie was dropped on redirect"
    store.save(incident)
    store.add_attempt(incident, RepairAttempt(number=1, outcome="verification_failed"))
    store.add_attempt(incident, RepairAttempt(number=2, outcome="verification_failed"))
    store.transition(incident, IncidentState.INVESTIGATING, reason="voice demo")
    return store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speak", action="store_true", help="play Sir's answers aloud")
    parser.add_argument(
        "--live", action="store_true", help="read the real incident store"
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"whisper model (default {DEFAULT_MODEL})",
    )
    args = parser.parse_args()

    model_path = MODELS / f"ggml-{args.model}.bin"
    transcriber = WhisperTranscriber(model_path=str(model_path))
    speech = MacSpeech()

    if not transcriber.available:
        print(f"cannot run: {transcriber.unavailable_reason()}")
        print("  brew install whisper-cpp")
        print(f"  curl -L -o {model_path} \\")
        print(
            f"    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{args.model}.bin"
        )
        return 1

    tmp = tempfile.mkdtemp()
    if args.live:
        from openjarvis.core.config import load_config
        from openjarvis.core.paths import get_config_dir

        config = load_config()
        store = IncidentStore(get_config_dir() / "reliability" / "incidents.db")
        merge_enabled = config.reliability.merge.enabled
    else:
        store = demo_store(Path(tmp))
        merge_enabled = False

    confirmations = ConfirmationStore()
    session = VoiceSession(
        id="demo",
        commands=VoiceCommands(
            facts=VoiceFacts(store=store, merge_enabled=merge_enabled),
            confirmations=confirmations,
            store=None if args.live else store,  # never mutate the real store here
        ),
        transcriber=transcriber,
        speech=speech,
    )

    print(f"model: {args.model}   store: {'live (read-only)' if args.live else 'demo'}")
    print(f"Sir: {session.greeting()}\n")

    for question in QUESTIONS:
        audio = utterance(question)
        started = time.time()
        turn = session.hear(audio)
        elapsed = time.time() - started
        print(f'  you : "{question}"')
        print(f'  Sir heard : "{turn.heard}"')
        print(
            f"  intent    : {turn.intent or '(not understood)'} "
            f"risk={turn.risk or '-'} executed={turn.executed}"
        )
        print(f'  Sir : "{turn.said}"   [{elapsed:.1f}s]')
        if args.speak:
            subprocess.run(["say", "-v", "Daniel", "-r", "175", turn.said], check=False)
        print()

    waiting = [p.intent for p in confirmations.pending()] or "none"
    print(f"call ended: {session.ended} ({session.end_reason})")
    print(f"awaiting confirmation in the Control Center: {waiting}")
    print("no audio was written to disk; no paid service was contacted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
