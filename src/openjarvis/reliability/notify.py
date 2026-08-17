"""Owner notifications.

Three things matter here beyond "send a message":

* **Redaction.** A notification is data leaving the device, so every message
  passes the framework's :class:`BoundaryGuard` before it is sent.
* **Restraint.** An incident storm must not become a message storm. Severity
  routing, deduplication and an hourly cap mean the owner keeps reading the
  messages instead of muting the bot — which is the real failure mode.
* **Silence.** Most of what JARVIS does is not news.

That last one is a policy, not a tuning knob, and it is worth stating plainly
because it inverts the obvious design. The tempting version of this module
narrates: problem found, investigating, repairing, verifying, pull request
opened, merged. Each message is true and the sequence is worthless — an owner
who receives six messages per incident learns to swipe them away, and the one
that needed them arrives in a stream they have trained themselves to ignore.

So the owner hears from JARVIS in exactly three situations:

1. **It is fixed.** One message, at the end, saying what broke and what is
   waiting for them. Which message depends on the operating mode: a pull
   request is the outcome when merging is off, a live fix when it is on.
2. **Something serious happened** — a CRITICAL fault, a rollback.
3. **JARVIS stopped and needs a human** — attempts exhausted, a safety refusal,
   or a merge that went live and did not come good.

Everything else is logged and visible in the dashboard: incidents opening,
repairs starting, previews building, merges landing, production verification
running. Those are steps. The owner is told outcomes.

The copy itself is deterministic — assembled from the component, the outcome,
the recorded root cause and a pull request number. No model writes a
notification. A sentence sent to the owner's phone should be one that something
checked, and an explanation generated for the occasion is exactly the kind of
plausible text nobody verified.

The JARVIS voice is applied to user-facing text only. Technical precision is
never sacrificed for personality: the message says what happened, and the
persona is a wrapper around it.
"""

from __future__ import annotations

import logging
import re
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
    "plain_subject",
    "render_alert",
    "render_human_required",
    "render_post_merge_failed",
    "render_production_verified",
    "render_resolved",
    "render_rolled_back",
]

#: There is deliberately no renderer for a repair starting, a preview building,
#: a merge landing or production verification beginning. Those events are logged
#: and shown in the dashboard; none of them is sent. Keeping dead templates for
#: them would be worse than not having them — the copy would rot out of step
#: with this policy, and re-enabling one later would quietly emit the narrating
#: style this module exists to avoid. Adding an event back means writing its
#: message to the rules above, on purpose.

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


#: Plain-English names for the things JARVIS watches, keyed by component.
#:
#: The owner reads these on a phone. "authentication" is what the probe suite
#: calls it; "Login" is what broke. Anything unmapped falls back to the
#: component name, which is the honest answer when there is no better word.
_PLAIN_SUBJECT = {
    "authentication": "Login",
    "auth": "Login",
    "login": "Login",
    "website": "The website",
    "site": "The website",
    "frontend": "The website",
    "landing": "The website",
    "api": "The API",
    "backend": "The API",
    "database": "The database",
    "supabase": "The database",
    "dashboard": "The dashboard",
    "deployment": "The deployment",
    "signup": "Sign-up",
    "billing": "Billing",
}


def plain_subject(incident: Incident) -> str:
    """What broke, in words the owner uses."""
    component = (incident.component or "").strip().lower()
    if component in _PLAIN_SUBJECT:
        return _PLAIN_SUBJECT[component]
    for key, plain in _PLAIN_SUBJECT.items():
        if key and key in component:
            return plain
    return incident.component or "Something"


def _short_cause(incident: Incident) -> str:
    """One short clause explaining why, or empty when nothing safe is known.

    Deliberately not generated by a model. The root cause was already written
    down during analysis; this trims it to a phone-sized clause and otherwise
    says nothing. Inventing an explanation for a notification is how an owner
    ends up trusting a sentence nobody checked.
    """
    cause = (incident.resolution.root_cause or "").strip()
    if not cause:
        return ""
    # First sentence only, and never a paragraph.
    first = cause.split(". ")[0].strip().rstrip(".")
    if not first or len(first) > 140:
        return ""
    return first[:1].lower() + first[1:]


def _closing_reference(incident: Incident, *, pr_number: int = 0) -> str:
    """The one identifier line, when there is a useful one."""
    if pr_number:
        return f"PR #{pr_number}"
    if incident.resolution.pr_url:
        match = re.search(r"/pull/(\d+)", incident.resolution.pr_url)
        if match:
            return f"PR #{match.group(1)}"
    return f"Incident {incident.id}" if incident.id else ""


def render_alert(incident: Incident, *, persona: bool = True) -> str:
    """A problem serious enough to interrupt the owner.

    Only reached for CRITICAL incidents — :meth:`NotificationRouter.alert` stays
    silent for everything JARVIS is allowed to handle on its own. An owner told
    about every failing probe stops reading the messages, and then the one that
    mattered arrives in a stream they have learned to ignore.
    """
    lines = [
        _sir(persona, "something serious happened."),
        f"{plain_subject(incident)} is down.",
    ]
    cause = _short_cause(incident)
    if cause:
        lines.append(f"It looks like {cause}.")
    lines.append("No production changes were made.")
    reference = _closing_reference(incident)
    if reference:
        lines += ["", reference]
    return "\n".join(lines)


def render_resolved(
    incident: Incident,
    *,
    attempt: Optional[RepairAttempt] = None,
    verification: Optional[VerificationResult] = None,
    persona: bool = True,
) -> str:
    """The one message a successful repair sends, in pull-request mode.

    Everything the loop did to get here — reproducing, repairing, four local
    check suites, a preview deployment, a re-run of the original probe — is in
    the dashboard and the audit log. None of it belongs on a phone. What the
    owner needs is that it is fixed, roughly why, and what is waiting for them.
    """
    lines = [_sir(persona, "I fixed the issue.")]
    cause = _short_cause(incident)
    subject = plain_subject(incident)
    if cause:
        lines.append(f"{subject} was failing because {cause}.")
    else:
        lines.append(f"{subject} was failing.")

    reference = _closing_reference(incident)
    if reference.startswith("PR "):
        lines.append(f"The fix passed all checks and {reference} is ready.")
    else:
        lines.append("The fix passed all checks.")
        if reference:
            lines += ["", reference]
    return "\n".join(lines)


#: Internal escalation reasons, translated into one plain sentence each.
#:
#: Matched on substrings of the reason the loop recorded, most specific first.
#: The internal text stays in the audit log where the detail is useful; what
#: goes to the phone is what the owner can act on.
_PLAIN_ESCALATION = (
    ("post-merge", "My fix went live but the site is still not working."),
    (
        "production did not verify",
        "My fix went live but the site is still not working.",
    ),
    ("protected path", "The fix would have touched files I am not allowed to change."),
    ("secret", "I found something that looked like a password in the change."),
    ("security check", "The fix did not pass my safety checks."),
    ("scope", "The fix was bigger than I am allowed to make on my own."),
    ("flapping", "The problem keeps coming back, so I stopped."),
    ("attempts", "I tried a few times and could not fix it safely."),
    ("interrupted", "I was interrupted part-way through a repair."),
    ("disabled", "I am not allowed to repair this automatically."),
)


def _plain_escalation(reason: str) -> str:
    """One plain sentence for why JARVIS stopped."""
    lowered = (reason or "").lower()
    for needle, plain in _PLAIN_ESCALATION:
        if needle in lowered:
            return plain
    return "I could not fix it safely."


def render_human_required(
    incident: Incident,
    *,
    reason: str,
    attempts: int,
    max_attempts: int,
    persona: bool = True,
) -> str:
    """JARVIS has stopped and needs the owner.

    Short on purpose. The internal reason, the attempt count and the evidence
    are all in the dashboard; a phone message that reprints them buries the two
    facts that matter — something is broken, and nothing more will happen until
    a human looks.
    """
    lines = [
        _sir(persona, "I need your help."),
        f"{plain_subject(incident)} is not working. {_plain_escalation(reason)}",
        "I stopped making changes.",
    ]
    reference = _closing_reference(incident)
    if reference:
        lines += ["", reference]
    return "\n".join(lines)


def render_rolled_back(incident: Incident, *, reason: str, persona: bool = True) -> str:
    """A live change was undone. The owner hears about this one."""
    lines = [
        _sir(persona, "I rolled a change back."),
        f"{plain_subject(incident)} broke after a deployment, so I put it back.",
    ]
    reference = _closing_reference(incident)
    if reference:
        lines += ["", reference]
    return "\n".join(lines)


def render_production_verified(
    incident: Incident, *, record: Any, result: Any, persona: bool = True
) -> str:
    """The one message a successful repair sends once merging is enabled.

    The live-mode counterpart of :func:`render_resolved`, and it says the one
    thing that differs: the fix is not waiting for anybody, it is already
    running. No deployment IDs, no merge SHAs — those are dashboard facts.
    """
    lines = [_sir(persona, "it's fixed.")]
    cause = _short_cause(incident)
    subject = plain_subject(incident)
    if cause:
        lines.append(f"{subject} was failing because {cause}.")
    else:
        lines.append(f"{subject} was failing.")
    lines.append("The fix is live and all checks are passing.")
    return "\n".join(lines)


def render_post_merge_failed(
    incident: Incident, *, record: Any, result: Any, persona: bool = True
) -> str:
    """Sent when a merge landed and production did not come good.

    The worst message JARVIS can send, and written to be acted on: unreviewed
    code is on the default branch, production is not verified, and the loop has
    stopped itself rather than trying again.
    """
    rule = str(getattr(result, "rule", "") or "")
    if rule in ("deployment_missing", "no_merge_sha"):
        what = "My fix was merged but the new version never went live."
    elif rule == "deployment_not_ready":
        what = "My fix was merged but the deployment failed."
    else:
        what = f"My fix went live but {plain_subject(incident).lower()} still fails."

    lines = [
        _sir(persona, "I need your help."),
        what,
        "I stopped making changes. Nothing was undone.",
    ]
    reference = _closing_reference(incident, pr_number=getattr(record, "pr_number", 0))
    if reference:
        lines += ["", reference]
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
    #: How long a CRITICAL alert waits for an escalation to supersede it. Zero
    #: sends immediately, which is what the tests that are not about deferral
    #: use.
    critical_grace_seconds: float = 20.0
    #: Builds the delay. Injected so tests need not wait twenty seconds.
    scheduler: Optional[Callable[[float, Callable[[], None]], Any]] = None

    _sent_times: List[float] = field(default_factory=list, repr=False)
    _recent: Dict[str, float] = field(default_factory=dict, repr=False)
    _suppressed: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _deferred: Dict[str, Any] = field(default_factory=dict, repr=False)
    _alert_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def _scheduler(self) -> Callable[[float, Callable[[], None]], Any]:
        return self.scheduler or threading.Timer

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
        """An incident has opened. Silent unless it is genuinely serious.

        A probe failing once, an incident opening, a repair starting: these are
        the system working, and JARVIS exists precisely so the owner does not
        have to watch them. Only CRITICAL interrupts — everything JARVIS is
        allowed to handle automatically is handled silently, and the owner hears
        either the fix or the escalation, never the commentary in between.

        Even a CRITICAL is held briefly rather than sent at once. A CRITICAL
        that JARVIS may not repair escalates to ``HUMAN_REQUIRED`` within the
        same tick, and the owner would get two messages seconds apart about one
        event: "something serious happened", then "I need your help". The second
        is strictly more useful — it says what to do — so the first waits out a
        short grace period and is cancelled if the escalation arrives. A
        CRITICAL that nothing supersedes is sent when the grace expires, so a
        standalone one still gets through.
        """
        if incident.severity is not Severity.CRITICAL:
            logger.info(
                "incident %s (%s) handled silently; no notification sent",
                incident.id,
                incident.severity.value,
            )
            return False

        message = render_alert(incident, persona=self.persona)
        if self.critical_grace_seconds <= 0:
            return self.notify(message, severity=Severity.CRITICAL)

        self._defer_alert(incident.id, message)
        return False

    # -- deferral ---------------------------------------------------------

    def _defer_alert(self, incident_id: str, message: str) -> None:
        """Hold a CRITICAL alert, so an escalation can supersede it."""
        with self._alert_lock:
            existing = self._deferred.pop(incident_id, None)
            if existing is not None:
                existing.cancel()
            timer = self._scheduler(
                self.critical_grace_seconds,
                lambda: self._send_deferred(incident_id, message),
            )
            self._deferred[incident_id] = timer
        timer.daemon = True
        timer.start()
        logger.info(
            "incident %s: holding the CRITICAL alert for %.0fs in case it escalates",
            incident_id,
            self.critical_grace_seconds,
        )

    def _send_deferred(self, incident_id: str, message: str) -> None:
        """Nothing superseded it, so the owner hears about it after all."""
        with self._alert_lock:
            self._deferred.pop(incident_id, None)
        self.notify(message, severity=Severity.CRITICAL)

    def _supersede(self, incident_id: str) -> bool:
        """Drop a held alert because a better message is going out instead."""
        with self._alert_lock:
            timer = self._deferred.pop(incident_id, None)
        if timer is None:
            return False
        timer.cancel()
        logger.info(
            "incident %s: the escalation supersedes the CRITICAL alert", incident_id
        )
        return True

    def flush(self) -> None:
        """Send every held alert now. For shutdown, and for tests."""
        with self._alert_lock:
            timers = list(self._deferred.items())
            self._deferred.clear()
        for _incident_id, timer in timers:
            timer.cancel()
            function = getattr(timer, "function", None)
            if callable(function):
                function()

    def progress(self, incident: Incident, *, attempt: int, max_attempts: int) -> bool:
        """Silent. A repair being under way is not news; its outcome is."""
        logger.info(
            "incident %s: repair attempt %d/%d (not notified)",
            incident.id,
            attempt,
            max_attempts,
        )
        return False

    def resolved(
        self,
        incident: Incident,
        *,
        attempt: Optional[RepairAttempt] = None,
        verification: Optional[VerificationResult] = None,
    ) -> bool:
        """The success message, in pull-request mode. Always sent.

        HIGH regardless of the incident's own severity: this is the outcome the
        owner stayed quiet for, and the severity that got them here was about
        the fault, not about the news that it is over.
        """
        return self.notify(
            render_resolved(
                incident,
                attempt=attempt,
                verification=verification,
                persona=self.persona,
            ),
            severity=Severity.HIGH,
        )

    def recovered(self, incident: Incident, *, recovery_type: Any = None) -> bool:
        """Silent. A fault that stopped on its own needed nobody.

        Neither a fix JARVIS made nor a problem anybody must act on — reporting
        it is the definition of noise.
        """
        logger.info(
            "incident %s recovered without a repair (not notified)", incident.id
        )
        return False

    def human_required(
        self, incident: Incident, *, reason: str, attempts: int, max_attempts: int
    ) -> bool:
        """Notify that a human is needed.

        Always sent at CRITICAL regardless of the incident's own severity: an
        escalation the owner never sees is the same as no escalation.

        Supersedes any CRITICAL alert still being held for this incident. Both
        describe the same event; this one also says what to do about it.
        """
        self._supersede(incident.id)
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
        """Silent under the current policy.

        This message used to be the owner's last chance to intervene before code
        reached the default branch — deliberately sent *before* the merge rather
        than after. The policy now asks for outcomes only, so it is logged
        instead. If you want the warning back, this is the method to change; the
        rendering is still here and still correct.
        """
        logger.info(
            "incident %s: merging PR #%s at %s (%s) — not notified",
            incident.id,
            pr_number,
            head_sha[:12],
            method,
        )
        return False

    def merge_outcome(self, incident: Incident, *, record: Any) -> bool:
        """Silent. A merge is a step; production verification is the outcome.

        A refusal is silent for the same reason: the gates declining to merge is
        the system working as designed, and it leaves an audit entry either way.
        """
        logger.info(
            "incident %s: merge %s (not notified)",
            incident.id,
            "succeeded" if getattr(record, "merged", False) else "refused",
        )
        return False

    # -- post-merge production verification -------------------------------

    def production_deployment(self, incident: Incident, *, observation: Any) -> bool:
        """Silent. Mid-flight progress, not an outcome."""
        logger.info(
            "incident %s: production deployment %s observed (not notified)",
            incident.id,
            getattr(observation, "deployment_id", ""),
        )
        return False

    def production_verification_started(
        self, incident: Incident, *, observation: Any, target_url: str = ""
    ) -> bool:
        """Silent. Checking is not an outcome; the verdict is a minute away."""
        logger.info(
            "incident %s: production verification started against %s (not notified)",
            incident.id,
            target_url,
        )
        return False

    def production_verified(
        self, incident: Incident, *, record: Any, result: Any
    ) -> bool:
        """The success message, in live mode. Always sent."""
        return self.notify(
            render_production_verified(
                incident, record=record, result=result, persona=self.persona
            ),
            severity=Severity.HIGH,
        )

    def post_merge_failed(
        self, incident: Incident, *, record: Any, result: Any
    ) -> bool:
        """Notify that a merge landed and production did not verify.

        CRITICAL unconditionally. The rate limiter lets CRITICAL through by
        design, and this is the message that exists for that exemption: the
        change is live, unreviewed, and unproven.

        Supersedes a held alert for the same incident, for the same reason the
        escalation does.
        """
        self._supersede(incident.id)
        return self.notify(
            render_post_merge_failed(
                incident, record=record, result=result, persona=self.persona
            ),
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
