"""The HTTP surface of a Sir Voice call.

Turn-based over plain POST, deliberately. The obvious build is WebRTC: it is
what "real-time browser audio" means to most people, and it is the wrong tool
here. WebRTC needs signalling, ICE, and a peer connection whose failure modes
are all asynchronous — for a conversation that is *one utterance in, one answer
out*, on a private network, with a 2-second transcription in the middle. A POST
carrying a WAV and returning JSON plus audio has none of that machinery, cannot
half-connect, and needs no dependency the Control Center does not already have.

The browser decides when the operator stopped speaking, because the browser is
the thing holding the microphone. Shipping every frame here to make that
decision would add latency to answer a question the client can already answer.

Six routes, and each one is either a read or a bounded action:

``POST /api/voice/answer``     pick up; returns a session id and a greeting
``POST /api/voice/utterance``  one turn: WAV in, transcript + reply + audio out
``POST /api/voice/text``       the same turn from typed text, for a quiet room
``POST /api/voice/hangup``     end the call
``GET  /api/voice/session``    transcript and liveness, for reconnecting
``GET  /api/voice/pending``    what is waiting for a human, and whether Sir called

Everything that changes state is a POST carrying the control token, so a page on
another origin cannot drive a call even if a browser were induced to send to it.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from openjarvis.reliability.types import now_iso

logger = logging.getLogger(__name__)

__all__ = ["VoiceEndpoints"]

#: Largest utterance accepted, in bytes. Sixteen-bit mono at 16 kHz is 32 kB a
#: second, so this is roughly forty seconds — far longer than any command, and
#: small enough that a malformed client cannot exhaust memory.
MAX_UTTERANCE_BYTES = 1_300_000

#: Longest a call may be silent before the server forgets it.
IDLE_SECONDS = 300.0


@dataclass
class VoiceEndpoints:
    """Routes a Control Center request into a voice session.

    Holds no HTTP machinery of its own: it is handed a path, a body and a
    query, and returns ``(status, payload)``. That keeps it testable without a
    socket and keeps the server's routing table readable.
    """

    sessions: Any
    confirmations: Any
    #: Set when a serious event has rung the phone and nobody has answered.
    pending_call: Optional[Dict[str, Any]] = None
    audit: Optional[Callable[[str, Dict[str, Any]], None]] = None
    #: Optional Web Push wiring. Absent means the Control Center still shows a
    #: waiting call when opened; only the ring is lost.
    push: Any = None
    subscriptions: Any = None
    _lock: Any = None

    def __post_init__(self) -> None:
        import threading

        self._lock = threading.Lock()

    # -- routing ----------------------------------------------------------

    def handle_get(
        self, path: str, query: Dict[str, Any]
    ) -> Tuple[int, Dict[str, Any]]:
        """Answer a read route."""
        if path == "/api/voice/session":
            return self._session(str((query.get("id") or [""])[0]))
        if path == "/api/voice/pending":
            return 200, {
                "call": self.pending_call,
                "confirmations": [c.to_dict() for c in self.confirmations.pending()],
                "at": now_iso(),
            }
        if path == "/api/voice/push-key":
            # The public half only. It is meant to be public — the browser
            # passes it to the push service to bind the subscription to us.
            return 200, {
                "key": self.push.key.application_server_key if self.push else "",
                "enabled": self.push is not None,
            }
        return 404, {"error": "no such route"}

    def handle_post(
        self, path: str, body: bytes, query: Dict[str, Any]
    ) -> Tuple[int, Dict[str, Any]]:
        """Answer a state-changing route. The caller has already checked the
        control token; this never re-decides authorisation, only what to do."""
        if path == "/api/voice/answer":
            return self._answer()
        if path == "/api/voice/utterance":
            return self._utterance(str((query.get("id") or [""])[0]), body)
        if path == "/api/voice/text":
            return self._text(str((query.get("id") or [""])[0]), body)
        if path == "/api/voice/hangup":
            return self._hangup(str((query.get("id") or [""])[0]))
        if path == "/api/voice/confirm":
            return self._confirm(body)
        if path == "/api/voice/subscribe":
            return self._subscribe(body)
        if path == "/api/voice/unsubscribe":
            return self._unsubscribe(body)
        return 404, {"error": "no such route"}

    # -- push registration ------------------------------------------------

    def _subscribe(self, body: bytes) -> Tuple[int, Dict[str, Any]]:
        """Register a phone to be rung."""
        if self.subscriptions is None:
            return 404, {"error": "push is not enabled"}
        try:
            payload = json.loads(body or b"{}")
            endpoint = str(payload.get("endpoint") or "")
        except (ValueError, AttributeError):
            return 400, {"error": "expected a push subscription"}
        if not endpoint.startswith("https://"):
            # Push endpoints are always https and always issued by the browser.
            # Anything else is a client bug or someone probing.
            return 400, {"error": "that is not a push endpoint"}
        self.subscriptions.add(endpoint, payload.get("keys") or {})
        self._audit("subscribed", {"origin": endpoint.split("/")[2]})
        return 200, {"ok": True, "count": len(self.subscriptions.all())}

    def _unsubscribe(self, body: bytes) -> Tuple[int, Dict[str, Any]]:
        if self.subscriptions is None:
            return 404, {"error": "push is not enabled"}
        try:
            endpoint = str(json.loads(body or b"{}").get("endpoint") or "")
        except (ValueError, AttributeError):
            return 400, {"error": "expected an endpoint"}
        removed = self.subscriptions.remove(endpoint)
        self._audit("unsubscribed", {"found": removed})
        return 200, {"ok": True, "removed": removed}

    # -- the call ---------------------------------------------------------

    def _answer(self) -> Tuple[int, Dict[str, Any]]:
        """Pick up. Clears the pending call: it has been answered."""
        session = self.sessions.start()
        if session is None:
            return 429, {"error": "too many calls are already open"}
        with self._lock:
            self.pending_call = None
        self._audit("answered", {"session": session.id})
        greeting = session.greeting()
        return 200, {
            "session": session.id,
            "speech": greeting,
            "audio": _encode(session.audio_for(greeting)),
            "transcript": session.transcript(),
        }

    def _utterance(self, session_id: str, body: bytes) -> Tuple[int, Dict[str, Any]]:
        """One turn of the conversation, from audio."""
        if len(body) > MAX_UTTERANCE_BYTES:
            return 413, {"error": "that utterance is too long"}
        session = self.sessions.get(session_id)
        if session is None:
            # The commonest real failure: the phone slept, the session was
            # reaped, and the operator is still holding it to their ear. Say so
            # precisely enough that the page can pick up again by itself.
            return 409, {"error": "the call has ended", "reconnect": True}
        turn = session.hear(body)
        self._audit("turn", {"session": session_id, **turn.to_dict()})
        return 200, self._turn_payload(session, turn)

    def _text(self, session_id: str, body: bytes) -> Tuple[int, Dict[str, Any]]:
        """The same turn, typed. Useful in a room where speaking is impossible,
        and it is the same authority path — typing "merge it" is refused exactly
        as saying it is."""
        session = self.sessions.get(session_id)
        if session is None:
            return 409, {"error": "the call has ended", "reconnect": True}
        try:
            said = str(json.loads(body or b"{}").get("text") or "")
        except (ValueError, AttributeError):
            return 400, {"error": 'expected {"text": "..."}'}
        if not said.strip():
            return 400, {"error": "nothing was said"}
        turn = session.say(said[:500])
        self._audit("turn", {"session": session_id, **turn.to_dict()})
        return 200, self._turn_payload(session, turn)

    def _turn_payload(self, session: Any, turn: Any) -> Dict[str, Any]:
        return {
            "session": session.id,
            "heard": turn.heard,
            "speech": turn.said,
            "intent": turn.intent,
            "risk": turn.risk,
            "executed": turn.executed,
            "confirmation_id": turn.confirmation_id,
            "ended": session.ended,
            "audio": _encode(session.audio_for(turn.said)),
        }

    def _hangup(self, session_id: str) -> Tuple[int, Dict[str, Any]]:
        ended = self.sessions.end(session_id, "hung up")
        self._audit("hangup", {"session": session_id, "found": ended})
        return 200, {"ended": True}

    def _session(self, session_id: str) -> Tuple[int, Dict[str, Any]]:
        """Liveness and transcript, so a reloaded page can rejoin its own call."""
        session = self.sessions.get(session_id)
        if session is None:
            return 200, {"live": False, "transcript": []}
        return 200, {
            "live": not session.ended,
            "session": session.id,
            "transcript": session.transcript(),
        }

    def _confirm(self, body: bytes) -> Tuple[int, Dict[str, Any]]:
        """Approve or decline a request a voice was not allowed to grant.

        Marks the decision and nothing else. Whatever acts on an approval is a
        separate, human-triggered path — see
        :mod:`~openjarvis.reliability.voice.confirmations`.
        """
        try:
            payload = json.loads(body or b"{}")
            confirmation_id = str(payload.get("id") or "")
            decision = str(payload.get("decision") or "").lower()
        except (ValueError, AttributeError):
            return 400, {"error": 'expected {"id": ..., "decision": ...}'}
        if decision not in ("approve", "decline"):
            return 400, {"error": "decision must be approve or decline"}
        ok = (
            self.confirmations.approve(confirmation_id)
            if decision == "approve"
            else self.confirmations.decline(confirmation_id)
        )
        self._audit(
            "confirmation",
            {"id": confirmation_id, "decision": decision, "applied": ok},
        )
        if not ok:
            return 409, {"error": "that request is no longer pending"}
        return 200, {
            "ok": True,
            "decision": decision,
            "confirmations": [c.to_dict() for c in self.confirmations.pending()],
        }

    # -- incoming calls ---------------------------------------------------

    def ring(self, *, incident_id: str, reason: str, detail: str) -> Dict[str, Any]:
        """Record that Sir wants the operator. Does not itself notify.

        The push notification and this record are separate on purpose: a phone
        that was asleep, out of range, or had notifications disabled still finds
        the call waiting when the Control Center is next opened.
        """
        call = {
            "incident_id": incident_id,
            "reason": reason,
            "detail": detail,
            "at": now_iso(),
        }
        with self._lock:
            self.pending_call = call
        self._audit("ring", call)
        self._knock()
        return call

    def _knock(self) -> None:
        """Wake every registered phone. Payload-free; details stay on the tailnet."""
        if self.push is None or self.subscriptions is None:
            return
        for subscription in self.subscriptions.all():
            delivered, detail = self.push.send(subscription)
            if not delivered and detail == "expired":
                # The browser rotated or dropped it. Keeping it would mean
                # failing forever against an endpoint that no longer exists.
                self.subscriptions.remove(subscription.endpoint)
            self._audit(
                "push",
                {
                    "delivered": delivered,
                    "detail": detail,
                    "origin": subscription.endpoint.split("/")[2],
                },
            )

    def _audit(self, event: str, payload: Dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit(event, payload)
        except Exception:  # noqa: BLE001 - an audit gap must not drop a call
            logger.exception("voice: could not audit %s", event)


def _encode(audio: bytes) -> str:
    """Base64 for the JSON reply, or ``""`` when there is no audio.

    Inline rather than a second request for the audio: it removes a round trip
    from every turn, and it means there is no temporary URL holding synthesised
    speech for anyone to fetch afterwards.
    """
    return base64.b64encode(audio).decode("ascii") if audio else ""
