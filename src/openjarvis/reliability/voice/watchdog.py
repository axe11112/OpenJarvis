"""Noticing that Sir should call, without the watcher knowing Sir exists.

The architectural rule this file protects: **the reliability core must not
depend on voice being alive.** A repair loop that imports a call orchestrator
is a repair loop that can be broken by a microphone, and the microphone is the
least important thing on the machine.

So nothing calls *out* of the watcher. This poller lives on the Control Center
side, reads the same incident store the watcher writes, and decides for itself
whether anything deserves a phone call. Kill it, and JARVIS carries on
monitoring, repairing and notifying exactly as before — the operator simply
stops being rung, which is the correct direction for that failure to point.

It is also why the poll is cheap and read-only: a `list(open_only=True)` every
thirty seconds against a SQLite file the watcher already has open. No locks
worth the name, nothing to contend with, nothing to corrupt.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = ["CallWatchdog"]

#: How often to look. Slower than the watcher's own tick on purpose: a call is
#: never urgent to the second, and a tight loop here would be a tight loop
#: against the store for no gain.
DEFAULT_INTERVAL = 30.0


@dataclass
class CallWatchdog:
    """Polls incidents and rings the phone when one genuinely warrants it.

    Parameters
    ----------
    trigger:
        The policy — :class:`~openjarvis.reliability.voice.trigger.CallTrigger`.
        Decides *whether* an incident is call-worthy.
    calls:
        The orchestrator. Decides whether a call may be *placed* right now,
        given cooldowns and whatever is already ringing.
    endpoints:
        The HTTP surface, so a call also appears in the Control Center for a
        phone that was off when the push went out.
    """

    store: Any
    trigger: Any
    calls: Any
    endpoints: Any = None
    interval: float = DEFAULT_INTERVAL
    sleep: Callable[[float], None] = None  # type: ignore[assignment]
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    def __post_init__(self) -> None:
        if self.sleep is None:
            self.sleep = self._stop.wait  # type: ignore[assignment]

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Begin polling on a daemon thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="sir-voice-call-watchdog"
        )
        self._thread.start()
        logger.info(
            "voice: watching for anything worth a call every %.0fs", self.interval
        )

    def stop(self) -> None:
        """Stop polling."""
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a failed poll must not end the loop
                logger.exception("voice: the call watchdog stumbled")
            self.sleep(self.interval)

    # -- one look ---------------------------------------------------------

    def tick(self) -> Optional[Any]:
        """Look once. Returns the call placed, or ``None``.

        Deliberately silent about everything it decides not to do: the normal
        outcome, on almost every tick forever, is that nothing warrants a call.
        """
        incidents = self._open_incidents()
        for incident in incidents:
            decision = self.trigger.evaluate(
                incident,
                production_authority_used=bool(
                    incident.metadata.get("post_merge_failure")
                ),
            )
            if not decision:
                continue

            call = self.calls.ring(
                reason=decision.reason,
                detail=self._detail(incident, decision),
                incident_id=incident.id,
            )
            if call is None:
                # Suppressed by a cooldown or an active call. The trigger has
                # already recorded that it fired, so it will not spam either.
                continue
            if self.endpoints is not None:
                self.endpoints.ring(
                    incident_id=incident.id,
                    reason=decision.reason,
                    detail=self._detail(incident, decision),
                )
            return call
        return None

    def _open_incidents(self):
        try:
            return list(self.store.list(open_only=True, limit=50) or [])
        except Exception:  # noqa: BLE001
            logger.exception("voice: could not read incidents")
            return []

    @staticmethod
    def _detail(incident: Any, decision: Any) -> str:
        """One plain sentence for a lock screen.

        The same vocabulary the Telegram messages use, for the same reason: an
        operator told "login" in a message and "authentication" on a call has to
        work out that they are the same thing, mid-outage.
        """
        from openjarvis.reliability.notify import plain_subject

        subject = plain_subject(incident)
        if decision.reason == "post_merge_failure":
            return f"My fix went live but {subject.lower()} still fails."
        if decision.reason == "production_deployment_failed":
            return "A production deployment failed."
        if decision.reason == "security_event":
            return "I refused something for safety reasons."
        return f"{subject} needs you. I stopped making changes."
