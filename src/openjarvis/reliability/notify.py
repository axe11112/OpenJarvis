"""Owner notifications.

Two things matter here beyond "send a message":

* **Redaction.** A notification is data leaving the device, so every message
  passes the framework's :class:`BoundaryGuard` before it is sent.
* **Restraint.** An incident storm must not become a message storm. Severity
  routing, deduplication and an hourly cap mean the owner keeps reading the
  messages instead of muting the bot — which is the real failure mode.

The JARVIS voice is applied to user-facing text only. Technical precision is
never sacrificed for personality: the message says what happened, and the
persona is a wrapper around it.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from openjarvis.reliability.types import (
    Incident,
    RepairAttempt,
    Severity,
    VerificationResult,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ConsoleNotifier",
    "NotificationRouter",
    "Notifier",
    "TelegramNotifier",
    "render_alert",
    "render_human_required",
    "render_progress",
    "render_resolved",
    "render_rolled_back",
]

_SEVERITY_ICON = {
    Severity.CRITICAL: "🔴",
    Severity.HIGH: "🟠",
    Severity.MEDIUM: "🟡",
    Severity.LOW: "⚪",
}


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _sir(persona: bool, text: str) -> str:
    """Apply the JARVIS voice, when enabled."""
    return f"Sir, {text}" if persona else text[:1].upper() + text[1:]


def render_alert(incident: Incident, *, persona: bool = True) -> str:
    """First notification: a problem has been detected."""
    icon = _SEVERITY_ICON.get(incident.severity, "⚪")
    lead = _sir(
        persona,
        f"I've detected an issue with the {incident.component} component.",
    )
    lines = [
        f"{icon} JARVIS ALERT",
        "",
        lead,
        "",
        f"Component: {incident.component}",
        f"Severity: {incident.severity.value}",
        f"Status: {incident.state.value}",
        f"Incident: {incident.id}",
    ]
    if incident.summary:
        lines += ["", incident.summary]
    if incident.occurrences > 1:
        lines.append(f"\nObserved {incident.occurrences} times.")
    return "\n".join(lines)


def render_progress(
    incident: Incident, *, attempt: int, max_attempts: int, persona: bool = True
) -> str:
    """Mid-repair notification."""
    lead = _sir(
        persona,
        f"I've reproduced the {incident.component} issue. "
        "Claude Code is investigating the root cause.",
    )
    return "\n".join(
        [
            "🔧 JARVIS",
            "",
            lead,
            "",
            f"Incident: {incident.id}",
            f"Attempt: {attempt}/{max_attempts}",
        ]
    )


def render_resolved(
    incident: Incident,
    *,
    attempt: Optional[RepairAttempt] = None,
    verification: Optional[VerificationResult] = None,
    persona: bool = True,
) -> str:
    """Success notification.

    Always states how the fix was verified: "resolved" without "verified how"
    is exactly the claim JARVIS exists not to make.
    """
    lead = _sir(persona, "the repair has passed verification.")
    lines = [
        "🟢 JARVIS",
        "",
        lead,
        "",
        f"Incident: {incident.id}",
        f"Component: {incident.component}",
    ]
    if incident.resolution.root_cause:
        lines += ["", f"Root cause: {incident.resolution.root_cause}"]
    if attempt is not None:
        if attempt.diff_stat:
            lines.append(f"Change: {attempt.diff_stat}")
        if attempt.test_summary:
            lines.append(f"Tests: {attempt.test_summary}")
        lines.append(f"Attempts: {attempt.number}")
    if verification is not None:
        lines += [
            "",
            f"Verified by re-running probe '{verification.probe_id}' "
            f"against {verification.target_url or 'the candidate deployment'}.",
        ]
    if incident.resolution.pr_url:
        lines += ["", f"Pull request: {incident.resolution.pr_url}"]
    return "\n".join(lines)


def render_human_required(
    incident: Incident,
    *,
    reason: str,
    attempts: int,
    max_attempts: int,
    persona: bool = True,
) -> str:
    """Escalation notification."""
    lead = _sir(
        persona,
        "I was unable to safely resolve this issue. Human intervention is required.",
    )
    return "\n".join(
        [
            "🚨 JARVIS",
            "",
            lead,
            "",
            f"Incident: {incident.id}",
            f"Component: {incident.component}",
            f"Severity: {incident.severity.value}",
            f"Attempts: {attempts}/{max_attempts}",
            "",
            f"Reason: {reason}",
        ]
    )


def render_rolled_back(incident: Incident, *, reason: str, persona: bool = True) -> str:
    """Rollback notification."""
    lead = _sir(persona, "a deployment regressed and I have rolled it back.")
    return "\n".join(
        [
            "↩️ JARVIS",
            "",
            lead,
            "",
            f"Incident: {incident.id}",
            f"Component: {incident.component}",
            "",
            f"Reason: {reason}",
        ]
    )


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class Notifier(ABC):
    """Sends a message to the owner.

    Kept minimal so SMS, voice or email can be added without touching anything
    that produces notifications.
    """

    notifier_id: str

    @abstractmethod
    def send(self, message: str, *, severity: Severity = Severity.MEDIUM) -> bool:
        """Deliver *message*. Returns ``True`` on success."""


class ConsoleNotifier(Notifier):
    """Writes notifications to the log. The always-available fallback."""

    notifier_id = "console"

    def __init__(self) -> None:
        self.sent: List[str] = []

    def send(self, message: str, *, severity: Severity = Severity.MEDIUM) -> bool:
        """Log the message."""
        self.sent.append(message)
        logger.info("JARVIS notification [%s]:\n%s", severity.value, message)
        return True


class TelegramNotifier(Notifier):
    """Sends via the framework's existing :class:`TelegramChannel`.

    Parameters
    ----------
    chat_id:
        Destination chat.  Must be in the channel's ``allowed_chat_ids``.
    channel:
        Pre-built channel; constructed from config when omitted.
    """

    notifier_id = "telegram"

    def __init__(
        self,
        *,
        chat_id: str,
        bot_token: str = "",
        allowed_chat_ids: str = "",
        channel: Any = None,
    ) -> None:
        self._chat_id = str(chat_id)
        self._bot_token = bot_token
        self._allowed_chat_ids = allowed_chat_ids
        self._channel = channel

    @property
    def channel(self) -> Any:
        """Lazily build the Telegram channel."""
        if self._channel is None:
            from openjarvis.channels.telegram import TelegramChannel

            self._channel = TelegramChannel(
                bot_token=self._bot_token,
                allowed_chat_ids=self._allowed_chat_ids,
                parse_mode="",  # plain text: incident content is not markdown
            )
        return self._channel

    def send(self, message: str, *, severity: Severity = Severity.MEDIUM) -> bool:
        """Deliver via Telegram, reporting failure rather than raising."""
        if not self._chat_id:
            logger.warning("telegram notifier: no chat_id configured")
            return False
        try:
            return bool(self.channel.send(self._chat_id, message))
        except Exception:
            logger.exception("telegram notifier: send failed")
            return False


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@dataclass
class NotificationRouter:
    """Decides what actually reaches the owner.

    Parameters
    ----------
    notifier:
        Transport.
    min_severity:
        Anything below this is logged, not sent.
    max_per_hour:
        Hard cap.  When exceeded, messages are dropped and a single
        "suppressed N messages" note is sent once the window reopens.
    dedup_window_seconds:
        Identical messages inside this window are sent once.
    redact:
        Whether to run outbound text through ``BoundaryGuard``.
    """

    notifier: Notifier
    min_severity: Severity = Severity.MEDIUM
    max_per_hour: int = 20
    dedup_window_seconds: float = 300.0
    persona: bool = True
    redact: bool = True
    clock: Callable[[], float] = time.monotonic

    _sent_times: List[float] = field(default_factory=list, repr=False)
    _recent: Dict[str, float] = field(default_factory=dict, repr=False)
    _suppressed: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def notify(self, message: str, *, severity: Severity = Severity.MEDIUM) -> bool:
        """Route *message*, returning ``True`` when it was actually sent."""
        if not severity.at_least(self.min_severity):
            logger.debug("notification below min_severity, not sent")
            return False

        with self._lock:
            now = self.clock()
            self._prune(now)

            key = str(hash(message))
            last = self._recent.get(key)
            if last is not None and now - last < self.dedup_window_seconds:
                logger.debug("duplicate notification suppressed")
                return False

            # CRITICAL always gets through: a rate cap that silences the one
            # message the owner needs is worse than no cap at all.
            if (
                severity is not Severity.CRITICAL
                and len(self._sent_times) >= self.max_per_hour
            ):
                self._suppressed += 1
                logger.warning(
                    "notification rate cap reached (%d/hour); suppressed %d",
                    self.max_per_hour,
                    self._suppressed,
                )
                return False

            suppressed = self._suppressed
            self._suppressed = 0
            self._sent_times.append(now)
            self._recent[key] = now

        if suppressed:
            message = (
                f"{message}\n\n"
                f"({suppressed} further notification(s) were suppressed by the "
                "rate cap.)"
            )

        return self.notifier.send(self._prepare(message), severity=severity)

    # -- incident-shaped helpers ------------------------------------------

    def alert(self, incident: Incident) -> bool:
        """Notify that an incident has opened."""
        return self.notify(
            render_alert(incident, persona=self.persona), severity=incident.severity
        )

    def progress(self, incident: Incident, *, attempt: int, max_attempts: int) -> bool:
        """Notify that a repair attempt is under way."""
        return self.notify(
            render_progress(
                incident,
                attempt=attempt,
                max_attempts=max_attempts,
                persona=self.persona,
            ),
            severity=incident.severity,
        )

    def resolved(
        self,
        incident: Incident,
        *,
        attempt: Optional[RepairAttempt] = None,
        verification: Optional[VerificationResult] = None,
    ) -> bool:
        """Notify that an incident was resolved and verified."""
        return self.notify(
            render_resolved(
                incident,
                attempt=attempt,
                verification=verification,
                persona=self.persona,
            ),
            severity=incident.severity,
        )

    def human_required(
        self, incident: Incident, *, reason: str, attempts: int, max_attempts: int
    ) -> bool:
        """Notify that a human is needed.

        Always sent at CRITICAL regardless of the incident's own severity: an
        escalation the owner never sees is the same as no escalation.
        """
        return self.notify(
            render_human_required(
                incident,
                reason=reason,
                attempts=attempts,
                max_attempts=max_attempts,
                persona=self.persona,
            ),
            severity=Severity.CRITICAL,
        )

    def rolled_back(self, incident: Incident, *, reason: str) -> bool:
        """Notify that a deployment was rolled back."""
        return self.notify(
            render_rolled_back(incident, reason=reason, persona=self.persona),
            severity=Severity.CRITICAL,
        )

    # -- internals --------------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - 3600.0
        self._sent_times = [t for t in self._sent_times if t > cutoff]
        self._recent = {
            k: t for k, t in self._recent.items() if now - t < self.dedup_window_seconds
        }

    def _prepare(self, message: str) -> str:
        """Redact outbound content before it leaves the device."""
        if not self.redact:
            return message
        from openjarvis.security.credential_stripper import CredentialStripper

        try:
            from openjarvis.security.boundary import BoundaryGuard

            return BoundaryGuard(mode="redact").scan_outbound(
                message, destination=self.notifier.notifier_id
            )
        except Exception:  # pragma: no cover - defensive
            # Never send raw on a redaction failure: fall back to the stripper.
            logger.exception("outbound redaction failed; falling back to the stripper")
            return CredentialStripper().strip(message)
