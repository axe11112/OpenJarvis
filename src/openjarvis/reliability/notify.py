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
from typing import Any, Callable, Dict, List, Optional, Sequence

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
    "MultiNotifier",
    "Notifier",
    "TelegramNotifier",
    "render_alert",
    "render_human_required",
    "render_merge_attempt",
    "render_merge_outcome",
    "render_recovered",
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


def render_recovered(
    incident: Incident,
    *,
    recovery_type: Any = None,
    persona: bool = True,
) -> str:
    """Render a recovery notice.

    Says plainly whether JARVIS did anything. Claiming a transient failure as a
    fix would make JARVIS look more effective than it is, which is the kind of
    flattery that gets a system trusted past its competence.
    """
    value = getattr(recovery_type, "value", str(recovery_type or "UNKNOWN"))
    external = value == "RECOVERED_EXTERNALLY"
    lines = [
        "\U0001f7e2 JARVIS",
        "",
        _sir(persona, f"Incident {incident.id} recovered."),
        "",
        f"Component: {incident.component}",
        f"Severity: {incident.severity.value}",
        f"Occurrences: {incident.occurrences}",
        "",
        (
            "No repair was required — the failure stopped reproducing on its own."
            if external
            else "The verified repair holds."
        ),
        "",
        "Production: UNCHANGED",
    ]
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


def render_merge_attempt(
    incident: Incident,
    *,
    pr_number: int,
    head_sha: str,
    method: str,
    persona: bool = True,
) -> str:
    """Sent immediately before JARVIS merges anything.

    Deliberately sent *before* rather than only after. This is the moment the
    owner can still intervene, and a notification that only ever arrives once
    the code is on the default branch is a report, not a warning.
    """
    lead = _sir(persona, "every merge gate has passed and I am merging.")
    return "\n".join(
        [
            "⏳ JARVIS — merging",
            "",
            lead,
            "",
            f"Incident: {incident.id}",
            f"Pull request: #{pr_number}",
            f"Commit: {head_sha[:12]}",
            f"Method: {method}",
            "",
            "This merges verified work into the default branch. It does not "
            "deploy: JARVIS has no deploy authority.",
        ]
    )


def render_merge_outcome(
    incident: Incident, *, record: Any, persona: bool = True
) -> str:
    """Sent after a merge attempt, whichever way it went.

    A refusal is reported as an ordinary outcome rather than as a failure. The
    gates refusing is the system working, and phrasing it as an error would
    train the owner to ignore exactly the message that matters.
    """
    if getattr(record, "merged", False):
        lead = _sir(persona, "the pull request is merged.")
        lines = [
            "🟢 JARVIS — merged",
            "",
            lead,
            "",
            f"Incident: {incident.id}",
            f"Pull request: #{record.pr_number}",
            f"Verified commit: {record.verified_head_sha[:12]}",
            f"Merge commit: {record.merge_commit_sha[:12]}",
            f"Method: {record.method}",
            "",
            "No production deployment was performed by JARVIS.",
        ]
        return "\n".join(lines)

    decision = getattr(record, "decision", None)
    failures = list(getattr(decision, "failures", []) or [])
    lead = _sir(persona, "I did not merge.")
    lines = [
        "⛔ JARVIS — merge refused",
        "",
        lead,
        "",
        f"Incident: {incident.id}",
        f"Pull request: #{record.pr_number or 'none recorded'}",
        "",
        "Refused because:",
    ]
    lines += [f"  • {g.name}: {g.detail}" for g in failures[:8]] or [
        f"  • {getattr(record, 'error', '') or 'unknown'}"
    ]
    if len(failures) > 8:
        lines.append(f"  … and {len(failures) - 8} more")
    lines += ["", "Nothing was merged and nothing was deployed."]
    return "\n".join(lines)


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


class MultiNotifier(Notifier):
    """Fans a message out to several transports.

    Delivery is best-effort per transport: one provider being down must not
    stop the others, and the result is ``True`` when *any* of them delivered.
    An escalation the owner never sees is the same as no escalation.

    **On SMS and voice.** The brief for this phase asks that these remain
    possible without being invented. They are: a provider only has to implement
    :meth:`Notifier.send`. None ships here, because every SMS and voice gateway
    worth relying on is a paid third-party service, and shipping a fake one
    would be worse than shipping nothing — it would look like coverage. See
    ``docs/JARVIS_RELIABILITY.md``.
    """

    notifier_id = "multi"

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self._notifiers = list(notifiers)

    def send(self, message: str, *, severity: Severity = Severity.MEDIUM) -> bool:
        """Deliver through every transport, reporting whether any succeeded."""
        delivered = False
        for notifier in self._notifiers:
            try:
                delivered = bool(notifier.send(message, severity=severity)) or delivered
            except Exception:
                logger.exception(
                    "notifier %s failed; continuing",
                    getattr(notifier, "notifier_id", "unknown"),
                )
        return delivered

    @property
    def notifiers(self) -> List[Notifier]:
        """The underlying transports."""
        return list(self._notifiers)


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

    def recovered(self, incident: Incident, *, recovery_type: Any = None) -> bool:
        """Notify that an incident cleared, saying whether JARVIS did it."""
        return self.notify(
            render_recovered(
                incident, recovery_type=recovery_type, persona=self.persona
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

    def merge_attempt(
        self, incident: Incident, *, pr_number: int, head_sha: str, method: str
    ) -> bool:
        """Notify that a merge is about to happen.

        HIGH regardless of the incident's severity: this is the last message
        before code reaches the default branch without a human, and the rate cap
        deciding the owner does not need to know would defeat the point.
        """
        return self.notify(
            render_merge_attempt(
                incident,
                pr_number=pr_number,
                head_sha=head_sha,
                method=method,
                persona=self.persona,
            ),
            severity=Severity.HIGH,
        )

    def merge_outcome(self, incident: Incident, *, record: Any) -> bool:
        """Notify how a merge attempt ended, including a refusal."""
        merged = bool(getattr(record, "merged", False))
        return self.notify(
            render_merge_outcome(incident, record=record, persona=self.persona),
            severity=Severity.HIGH if merged else Severity.MEDIUM,
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
