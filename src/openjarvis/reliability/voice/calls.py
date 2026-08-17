"""Deciding to ring, ringing, and not ringing again.

The trigger policy in :mod:`~openjarvis.reliability.voice.trigger` answers "is
this worth a call". This module answers everything after that: whether a call is
already in progress, whether this phone was rung about the same thing five
minutes ago, what happens when the push fails, and how a declined call is
remembered.

Storm protection is the whole point, and it is not optional. A watcher ticks
every sixty seconds; an incident stuck in ``HUMAN_REQUIRED`` is stuck for as
long as the human is asleep. Without the guards below, that is a phone ringing
sixty times before breakfast, which does not get the operator's attention — it
gets the app deleted. So:

* one active call at a time, ever;
* one call per incident, then a cooldown measured in hours;
* a decline is a decision, and it is honoured with a longer cooldown than a
  missed call — the operator has said "not now" rather than failed to hear it;
* a bounded number of attempts, then it stops and falls back to Telegram;
* every attempt is audited whether it rang, was suppressed, or failed.

A call that cannot be delivered degrades to exactly one Telegram message. The
operator is told either way; only the urgency of the channel changes.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from openjarvis.reliability.types import now_iso

logger = logging.getLogger(__name__)

__all__ = ["Call", "CallOrchestrator"]

#: How long a ringing call waits before it counts as missed.
RING_SECONDS = 45.0

#: Silence after the operator declines. Longer than a miss on purpose: a
#: decline is an answer.
DECLINE_COOLDOWN = 3600.0

#: Silence after a call nobody answered.
MISSED_COOLDOWN = 900.0

#: Attempts for one incident before Sir gives up and writes instead.
MAX_ATTEMPTS = 2


@dataclass
class Call:
    """One attempt to get the operator's attention."""

    id: str
    reason: str
    detail: str
    incident_id: str = ""
    state: str = "RINGING"
    started_at: str = field(default_factory=now_iso)
    started_monotonic: float = 0.0
    answered_at: str = ""
    ended_at: str = ""
    test: bool = False
    push_delivered: int = 0
    push_failed: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """For the Control Center. Identifiers and verdicts only."""
        return {
            "id": self.id,
            "reason": self.reason,
            "detail": self.detail,
            "incident_id": self.incident_id,
            "state": self.state,
            "started_at": self.started_at,
            "answered_at": self.answered_at,
            "ended_at": self.ended_at,
            "test": self.test,
            "push_delivered": self.push_delivered,
            "push_failed": self.push_failed,
        }


@dataclass
class CallOrchestrator:
    """Rings the phone, once, and remembers that it did.

    Parameters
    ----------
    push / subscriptions:
        Web Push wiring. Absent means a call still registers — the Control
        Center shows it waiting — but the phone will not make a sound.
    fallback:
        ``(reason, detail) -> None``, called when a call cannot be delivered.
        In practice the Telegram router, so the operator is told regardless.
    audit:
        ``(event, payload) -> None`` for every decision.
    """

    push: Any = None
    subscriptions: Any = None
    fallback: Optional[Callable[[str, str], None]] = None
    audit: Optional[Callable[[str, Dict[str, Any]], None]] = None
    clock: Callable[[], float] = time.monotonic
    ring_seconds: float = RING_SECONDS
    decline_cooldown: float = DECLINE_COOLDOWN
    missed_cooldown: float = MISSED_COOLDOWN
    max_attempts: int = MAX_ATTEMPTS

    active: Optional[Call] = None
    history: List[Call] = field(default_factory=list)
    _cooldowns: Dict[str, float] = field(default_factory=dict, repr=False)
    _attempts: Dict[str, int] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- placing a call ---------------------------------------------------

    def ring(
        self,
        *,
        reason: str,
        detail: str,
        incident_id: str = "",
        test: bool = False,
    ) -> Optional[Call]:
        """Ring the phone, or explain why not.

        Returns the :class:`Call` when one was placed, ``None`` when it was
        suppressed. Suppression is a normal outcome, not an error.
        """
        with self._lock:
            self._expire_locked()

            if self.active is not None:
                # Never two calls at once. A second ring during a conversation
                # is noise at the exact moment the operator is dealing with the
                # first thing.
                self._audit_event(
                    "call_suppressed",
                    {"reason": reason, "why": "a call is already in progress"},
                )
                return None

            key = incident_id or reason
            if not test:
                until = self._cooldowns.get(key, 0.0)
                if self.clock() < until:
                    self._audit_event(
                        "call_suppressed",
                        {
                            "reason": reason,
                            "incident_id": incident_id,
                            "why": "cooling down",
                            "seconds_left": round(until - self.clock()),
                        },
                    )
                    return None

                if self._attempts.get(key, 0) >= self.max_attempts:
                    self._audit_event(
                        "call_suppressed",
                        {
                            "reason": reason,
                            "incident_id": incident_id,
                            "why": "attempts exhausted; falling back to a message",
                        },
                    )
                    self._fall_back(reason, detail)
                    return None
                self._attempts[key] = self._attempts.get(key, 0) + 1

            call = Call(
                id=uuid.uuid4().hex[:12],
                reason=reason,
                detail=detail,
                incident_id=incident_id,
                started_monotonic=self.clock(),
                test=test,
            )
            self.active = call

        self._audit_event("call_requested", call.to_dict())
        delivered = self._knock(call)
        if not delivered and not test:
            # The phone could not be woken. Say it once, in writing, rather
            # than letting a serious event wait for someone to open a browser.
            self._fall_back(reason, detail)
        return call

    def _knock(self, call: Call) -> bool:
        """Wake every registered phone. Returns whether any push landed."""
        if self.push is None or self.subscriptions is None:
            self._audit_event("call_delivery", {"id": call.id, "push": "unavailable"})
            return False

        delivered = 0
        for subscription in self.subscriptions.all():
            ok, detail = self.push.send(subscription)
            if ok:
                delivered += 1
            else:
                call.push_failed += 1
                if detail == "expired":
                    self.subscriptions.remove(subscription.endpoint)
            self._audit_event(
                "call_delivery",
                {"id": call.id, "delivered": ok, "detail": detail},
            )
        call.push_delivered = delivered
        return delivered > 0

    # -- the operator responds --------------------------------------------

    def answered(self, call_id: str = "") -> Optional[Call]:
        """The operator picked up. Clears the attempt count for the incident."""
        with self._lock:
            call = self.active
            if call is None or (call_id and call.id != call_id):
                return None
            call.state = "ANSWERED"
            call.answered_at = now_iso()
            key = call.incident_id or call.reason
            # Answering resolves the escalation, so the next genuine event
            # about this incident may ring again.
            self._attempts.pop(key, None)
            self._cooldowns.pop(key, None)
            self.active = None
            self.history.append(call)
        self._audit_event("call_answered", call.to_dict())
        return call

    def declined(self, call_id: str = "") -> Optional[Call]:
        """The operator said not now. Honoured with the longer cooldown."""
        return self._end(call_id, "DECLINED", self.decline_cooldown)

    def missed(self, call_id: str = "") -> Optional[Call]:
        """Nobody answered before the ring timed out."""
        return self._end(call_id, "MISSED", self.missed_cooldown)

    def _end(self, call_id: str, state: str, cooldown: float) -> Optional[Call]:
        with self._lock:
            call = self.active
            if call is None or (call_id and call.id != call_id):
                return None
            call.state = state
            call.ended_at = now_iso()
            if not call.test:
                self._cooldowns[call.incident_id or call.reason] = (
                    self.clock() + cooldown
                )
            self.active = None
            self.history.append(call)
        self._audit_event(f"call_{state.lower()}", call.to_dict())
        return call

    # -- state ------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """What the Control Center shows about calling."""
        with self._lock:
            self._expire_locked()
            active = self.active.to_dict() if self.active else None
            last = self.history[-1].to_dict() if self.history else None
            cooling = {
                key: round(until - self.clock())
                for key, until in self._cooldowns.items()
                if until > self.clock()
            }
        return {
            "active": active,
            "last": last,
            "cooling_down": cooling,
            "registered_phones": (
                len(self.subscriptions.all()) if self.subscriptions else 0
            ),
            "can_ring": bool(
                self.push and self.subscriptions and self.subscriptions.all()
            ),
        }

    def _expire_locked(self) -> None:
        """Time out a call nobody answered. Caller holds the lock."""
        call = self.active
        if call is None:
            return
        if self.clock() - call.started_monotonic < self.ring_seconds:
            return
        call.state = "MISSED"
        call.ended_at = now_iso()
        if not call.test:
            self._cooldowns[call.incident_id or call.reason] = (
                self.clock() + self.missed_cooldown
            )
        self.active = None
        self.history.append(call)
        logger.info("voice: call %s went unanswered", call.id)

    # -- internals --------------------------------------------------------

    def _fall_back(self, reason: str, detail: str) -> None:
        if self.fallback is None:
            return
        try:
            self.fallback(reason, detail)
        except Exception:  # noqa: BLE001 - a failed fallback must not raise
            logger.exception("voice: the call fallback message failed")

    def _audit_event(self, event: str, payload: Dict[str, Any]) -> None:
        logger.info("voice: %s %s", event, payload.get("reason", ""))
        if self.audit is None:
            return
        try:
            self.audit(event, payload)
        except Exception:  # noqa: BLE001
            logger.exception("voice: could not audit %s", event)
