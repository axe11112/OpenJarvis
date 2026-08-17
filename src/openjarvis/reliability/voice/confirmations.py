"""Requests a voice may make but may not grant.

The point of this module is the gap it creates. A voice asking to merge a pull
request produces a row here and nothing else: no merge, no flag flipped, no
authority moved. Someone then opens the Control Center, on a screen, and either
approves it or does not.

That gap is doing real work. A microphone is not an authenticated channel —
it hears whoever is in the room, a speaker playing a recording, a television.
Speech recognition then turns that into text with no notion of who said it.
Everything in :data:`~openjarvis.reliability.voice.intents.INTENTS` marked
``CONFIRM`` is something whose blast radius is production, and none of it should
rest on "the microphone heard a sentence".

Requests expire. A pending merge approved forty minutes after it was asked for
is being approved against a repository that has moved, by someone who has
probably forgotten the question.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.reliability.types import now_iso

logger = logging.getLogger(__name__)

__all__ = ["ConfirmationStore", "PendingConfirmation"]

#: How long a spoken request stays approvable.
DEFAULT_TTL_SECONDS = 600.0


@dataclass
class PendingConfirmation:
    """One high-risk request, awaiting a human at a keyboard."""

    id: str
    intent: str
    description: str
    transcript: str
    requested_at: str
    expires_at_monotonic: float
    session_id: str = ""
    state: str = "PENDING"
    decided_at: str = ""
    decided_by: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the dashboard. Carries no credential and no free text
        beyond the redacted transcript that produced it."""
        return {
            "id": self.id,
            "intent": self.intent,
            "description": self.description,
            "transcript": self.transcript,
            "requested_at": self.requested_at,
            "state": self.state,
            "session_id": self.session_id,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
        }


@dataclass
class ConfirmationStore:
    """Pending high-risk requests, in memory and optionally on disk.

    Deliberately *not* an execution engine. Approving a confirmation here marks
    it approved and nothing more — whatever acts on that approval is a separate,
    already-audited code path that a human triggered. This class must never grow
    a method that performs the thing it describes, or the gap it exists to
    create closes.
    """

    clock: Any = None
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    path: Optional[Path] = None
    _items: Dict[str, PendingConfirmation] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.clock is None:
            import time

            self.clock = time.monotonic

    # -- writes -----------------------------------------------------------

    def request(
        self, *, intent: str, description: str, transcript: str, session_id: str = ""
    ) -> PendingConfirmation:
        """Record a request. Grants nothing."""
        pending = PendingConfirmation(
            id=uuid.uuid4().hex[:12],
            intent=intent,
            description=description,
            transcript=transcript[:300],
            requested_at=now_iso(),
            expires_at_monotonic=self.clock() + self.ttl_seconds,
            session_id=session_id,
        )
        with self._lock:
            self._items[pending.id] = pending
            self._expire_locked()
        self._persist()
        logger.warning(
            "voice: %r requires confirmation in the Control Center (id=%s)",
            intent,
            pending.id,
        )
        return pending

    def approve(self, confirmation_id: str, *, actor: str = "operator") -> bool:
        """Mark a request approved. Does not carry it out."""
        return self._decide(confirmation_id, "APPROVED", actor)

    def decline(self, confirmation_id: str, *, actor: str = "operator") -> bool:
        """Mark a request declined."""
        return self._decide(confirmation_id, "DECLINED", actor)

    def _decide(self, confirmation_id: str, state: str, actor: str) -> bool:
        with self._lock:
            self._expire_locked()
            pending = self._items.get(confirmation_id)
            if pending is None or pending.state != "PENDING":
                return False
            pending.state = state
            pending.decided_at = now_iso()
            pending.decided_by = actor
        self._persist()
        return True

    # -- reads ------------------------------------------------------------

    def pending(self) -> List[PendingConfirmation]:
        """Every request still awaiting a decision, newest first."""
        with self._lock:
            self._expire_locked()
            items = [p for p in self._items.values() if p.state == "PENDING"]
        return sorted(items, key=lambda p: p.requested_at, reverse=True)

    def get(self, confirmation_id: str) -> Optional[PendingConfirmation]:
        """One request by id."""
        with self._lock:
            self._expire_locked()
            return self._items.get(confirmation_id)

    def all(self) -> List[PendingConfirmation]:
        """Everything, decided or not, newest first."""
        with self._lock:
            self._expire_locked()
            items = list(self._items.values())
        return sorted(items, key=lambda p: p.requested_at, reverse=True)

    # -- internals --------------------------------------------------------

    def _expire_locked(self) -> None:
        """Time out anything nobody answered. Caller holds the lock."""
        now = self.clock()
        for pending in self._items.values():
            if pending.state == "PENDING" and now >= pending.expires_at_monotonic:
                pending.state = "EXPIRED"
                pending.decided_at = now_iso()
                pending.decided_by = "timeout"

    def _persist(self) -> None:
        """Best-effort snapshot. A store that cannot write still refuses."""
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = [p.to_dict() for p in self.all()]
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("voice: could not persist pending confirmations")
