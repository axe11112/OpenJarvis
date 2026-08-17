"""Whether Sir can actually hear, speak, and reach the phone.

Written to a rule that the rest of JARVIS already follows and that this kind of
panel usually breaks: **unknown is not healthy**. A voice stack reports ONLINE
only when every part of it has been checked and found working. If whisper's
model file cannot be found, if the tailnet is unreachable, if no phone has ever
registered — that is DEGRADED or OFFLINE, named, with the reason attached.

The failure this prevents is specific. A dashboard that shows a green "VOICE"
light because nothing has thrown an exception yet is worse than no light at
all: the operator stops checking, and finds out the microphone was never
reachable at the moment they most needed to talk.

Every check is cheap and local — a file that exists, a binary on PATH, a count
of registered phones. Nothing here dials out, so rendering the Control Center
cannot be slowed down by a network timeout.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ["VoiceHealth"]

ONLINE = "ONLINE"
DEGRADED = "DEGRADED"
OFFLINE = "OFFLINE"
UNKNOWN = "UNKNOWN"


@dataclass
class VoiceHealth:
    """Reports what works, and refuses to guess about the rest."""

    transcriber: Any = None
    speech: Any = None
    normalizer: Any = None
    #: The record of what real phones have managed to say. Separate from the
    #: transcriber on purpose: "whisper loads" and "the owner can be heard" are
    #: different claims, and only the second one is the product.
    microphone: Any = None
    subscriptions: Any = None
    sessions: Any = None
    calls: Any = None
    access: Any = None
    tailscale_runner: Optional[Callable[..., Any]] = None

    # -- parts ------------------------------------------------------------

    def stt(self) -> Dict[str, Any]:
        if self.transcriber is None:
            return {"state": UNKNOWN, "detail": "no transcriber is configured"}
        if getattr(self.transcriber, "available", False):
            model = str(getattr(self.transcriber, "model_path", ""))
            return {"state": "READY", "detail": model.rsplit("/", 1)[-1] or "ready"}
        return {
            "state": "FAILED",
            "detail": self.transcriber.unavailable_reason() or "unavailable",
        }

    def mic(self) -> Dict[str, Any]:
        """Whether a real device has ever produced a transcript.

        The one check here that cannot be satisfied by installing something.
        Everything else in this panel is a statement about this machine; this is
        a statement about the phone in the owner's hand, and it is the only one
        that has ever been wrong in the direction that mattered.
        """
        if self.microphone is None:
            return {
                "state": UNKNOWN,
                "detail": "no microphone record is configured",
            }
        return self.microphone.snapshot()

    def tts(self) -> Dict[str, Any]:
        if self.speech is None:
            return {"state": UNKNOWN, "detail": "no speech synthesiser is configured"}
        if getattr(self.speech, "available", False):
            return {"state": "READY", "detail": getattr(self.speech, "voice", "")}
        return {
            "state": "FAILED",
            "detail": self.speech.unavailable_reason() or "unavailable",
        }

    def audio(self) -> Dict[str, Any]:
        """Whether the containers a phone actually sends can be decoded."""
        if self.normalizer is None:
            return {"state": UNKNOWN, "detail": "no audio normaliser is configured"}
        caps = self.normalizer.capabilities()
        if caps["iphone_supported"]:
            return {
                "state": "READY",
                "detail": ", ".join(caps["containers"]),
                "containers": caps["containers"],
            }
        return {
            "state": "FAILED",
            "detail": "cannot decode what an iPhone records; afconvert is missing",
        }

    def phone(self) -> Dict[str, Any]:
        if self.subscriptions is None:
            return {"state": UNKNOWN, "detail": "push is not configured"}
        registered = self.subscriptions.all()
        if not registered:
            return {
                "state": "NOT_REGISTERED",
                "detail": "no phone has installed Sir yet",
            }
        newest = max((s.added_at or "") for s in registered)
        return {
            "state": "REGISTERED",
            "detail": f"{len(registered)} phone(s), newest {newest[:19]}",
            "count": len(registered),
        }

    def call_channel(self) -> Dict[str, Any]:
        """What kind of ring is actually possible right now.

        ``LIMITED`` is the honest answer for the free path: a notification with
        a sound, not a full-screen incoming call. Reporting READY would be a
        promise iOS does not let this keep.
        """
        if self.calls is None:
            return {"state": UNKNOWN, "detail": "calling is not configured"}
        snapshot = self.calls.snapshot()
        if not snapshot.get("can_ring"):
            return {
                "state": "UNAVAILABLE",
                "detail": "no registered phone to ring",
            }
        return {
            "state": "LIMITED",
            "detail": (
                "Web Push notification with sound — not a native incoming call. "
                "CallKit needs a paid Apple Developer membership."
            ),
        }

    def tailscale(self) -> Dict[str, Any]:
        if self.access is None or not getattr(self.access, "tailscale_enabled", False):
            return {"state": UNKNOWN, "detail": "not serving over Tailscale"}
        binary = shutil.which("tailscale") or (
            "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        )
        try:
            proc = (self.tailscale_runner or subprocess.run)(
                [binary, "status", "--peers=false"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:  # noqa: BLE001 - unknown, never "fine"
            return {"state": UNKNOWN, "detail": "could not ask tailscale"}
        if getattr(proc, "returncode", 1) == 0:
            return {"state": "REACHABLE", "detail": self.access.tailscale_host}
        return {"state": "UNREACHABLE", "detail": "tailscale is not connected"}

    # -- the whole ---------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Everything, plus one overall verdict derived from it."""
        parts = {
            "stt": self.stt(),
            "microphone": self.mic(),
            "tts": self.tts(),
            "audio": self.audio(),
            "phone": self.phone(),
            "call_channel": self.call_channel(),
            "tailscale": self.tailscale(),
        }
        states = [part["state"] for part in parts.values()]

        # Hearing and speaking are what a call *is*. Without either, voice is
        # offline however healthy the rest looks — and a microphone known to be
        # broken is exactly "without hearing", however well the engine loads.
        if (
            parts["stt"]["state"] == "FAILED"
            or parts["tts"]["state"] == "FAILED"
            or parts["microphone"]["state"] == "FAILED"
        ):
            overall = OFFLINE
        elif "FAILED" in states or "UNREACHABLE" in states:
            overall = DEGRADED
        elif parts["microphone"]["state"] != "WORKING":
            # The rule that had to be written down after a green light shipped
            # over a microphone that had never once worked: ONLINE requires a
            # real device to have been heard. Not whisper installed, not a model
            # file present, not a test with a synthesised WAV — a phone, over
            # the network, producing words.
            overall = DEGRADED
        elif UNKNOWN in states or "NOT_REGISTERED" in states:
            # Deliberately not ONLINE. Something here has not been established,
            # and a green light for an unverified component is the exact lie
            # this module exists to avoid.
            overall = DEGRADED
        else:
            overall = ONLINE

        return {
            "voice": overall,
            "parts": parts,
            "open_sessions": (
                self.sessions.open_sessions() if self.sessions is not None else 0
            ),
        }
