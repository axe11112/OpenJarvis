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

So the owner hears from JARVIS in exactly two situations, and for one
underlying problem they hear at most one of them:

1. **It is fixed** — and only when they were told it was broken in the first
   place. A problem they never heard about, that recovered on its own, produces
   nothing at all.
2. **JARVIS needs a specific thing from them** — a decision, an approval, a
   rotation, a rollback. Not "I could not fix it", which is a status; the
   message carries the exact operator action, and if
   :mod:`~openjarvis.reliability.owner_ask` cannot name one, nothing is sent
   and the incident parks visibly in Control Center instead.

Everything else is logged and visible in the dashboard: incidents opening,
severity rising, repairs starting, previews building, merges landing,
production verification running, another probe joining an outage. Those are
steps. The owner is told outcomes.

**One problem, one message.** Deduplication keys on the correlated *outage*
from :mod:`~openjarvis.reliability.outage`, not on the incident and not on its
fingerprint. A deployment that takes down the homepage, login and sign-up opens
three incidents — all three survive, with their own evidence, in the store and
the dashboard — and produces one owner-facing message naming all three
subjects. A new incident id for the same underlying problem produces none.

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

from openjarvis.reliability.owner_ask import OwnerAsk, build_owner_ask
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
    "RecordingNotifier",
    "TelegramNotifier",
    "plain_subject",
    "render_alert",
    "render_ask",
    "render_fixed",
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

    Short on purpose. The internal reason, the attempt count and the full
    handover are all in the dashboard; a phone message that reprints them buries
    the two facts that matter — something is broken, and nothing more will
    happen until a human looks.

    One line of cause is the exception, when there is one worth saying. "I need
    your help" without a subject is a message somebody reads at 3am and then has
    to go and find the actual information; one clause is the difference between
    a page and a briefing.
    """
    lines = [
        _sir(persona, "I need your help."),
        f"{plain_subject(incident)} is not working. {_plain_escalation(reason)}",
    ]
    cause = _handover_cause(incident) or _short_cause(incident)
    if cause:
        lines.append(f"It looks like {cause}.")
    lines.append("I stopped making changes.")
    reference = _closing_reference(incident)
    if reference:
        lines += ["", reference]
    return "\n".join(lines)


def render_ask(ask: OwnerAsk, *, persona: bool = True) -> str:
    """The one message that asks the owner for something.

    Four lines at most, and the fourth is the only one that matters: the exact
    thing the operator has to do. Everything else — the evidence, the attempts,
    the internal reason, the full handover — is in Control Center, where there
    is a screen and no one is half asleep.

    Never called with an ask that has no action:
    :meth:`NotificationRouter.human_required` refuses first. The rendering is
    written as if that guarantee holds because it does, and a message that
    reached a phone saying "I need your help" with nothing after it would be
    the exact failure this module exists to prevent.
    """
    lines = [_sir(persona, "I need your help."), f"{ask.what_failed}."]
    if ask.cause:
        lines[-1] = f"{ask.what_failed} because {ask.cause}."
    lines.append("I stopped making changes.")
    lines.append(ask.action)
    if ask.reference:
        lines += ["", ask.reference]
    return "\n".join(lines)


def render_fixed(
    incident: Incident, *, subjects: Sequence[str] = (), persona: bool = True
) -> str:
    """The one message a resolved outage sends.

    Deliberately short, and deliberately the same words whichever part of the
    pipeline established it: a repair that verified, a merge that reached
    production, or the problem simply going away after the owner was already
    woken. Three different internal facts, one thing the owner cares about.
    """
    lines = [_sir(persona, "it's fixed.")]
    names = [str(s).strip() for s in subjects if str(s).strip()]
    if len(names) > 1:
        rendered = ", ".join(n[:1].lower() + n[1:] for n in names[:-1])
        last = names[-1]
        lines.append(
            f"{rendered.capitalize()} and {last[:1].lower() + last[1:]} are "
            "working normally again."
        )
    else:
        lines.append("Everything is working normally again.")
    return "\n".join(lines)


def _handover_cause(incident: Incident) -> str:
    """One clause of cause from the recorded handover, when it established one.

    Never the "not established" placeholder: telling somebody at 3am that the
    cause looks like "not established" is worse than saying nothing, and the
    handover in the dashboard says it properly.
    """
    handover = (getattr(incident, "metadata", None) or {}).get("handover")
    if not isinstance(handover, dict):
        return ""
    cause = str(handover.get("cause") or "").strip()
    if not cause or cause.lower().startswith("not established"):
        return ""
    first = cause.split(". ")[0].strip().rstrip(".")
    if not first or len(first) > 140:
        return ""
    return first[:1].lower() + first[1:]


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

    Two lines, and they are the owner's own words rather than a summary of the
    pipeline. This message is the end of a sequence — problem found, repaired,
    four check suites, a preview, a merge, a production deployment, the original
    reproduction and the whole probe fleet re-run against production — and none
    of that belongs on a phone. Anyone who wants the deployment id, the merge SHA
    or the per-probe results has a dashboard and an audit log.

    Deliberately does *not* name the component or the cause. Every other message
    here does, because in those the owner has something to decide. Here there is
    nothing to decide: it is fixed, and the shortest true sentence is the kindest
    one at three in the morning.
    """
    return "\n".join(
        [
            _sir(persona, "it's fixed."),
            "The issue is resolved and everything is working normally.",
        ]
    )


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


class RecordingNotifier(Notifier):
    """Keeps every message instead of delivering it.

    Exists so the notification policy can be exercised against real recorded
    incident history without anything reaching a phone. ``jarvis reliability
    replay-notifications`` runs the whole router over the incident database
    through one of these, which is the only honest way to answer "how many
    messages would the new rules have sent?" — and the only safe way, because
    the alternative is finding out by sending them.
    """

    notifier_id = "recording"

    def __init__(self, *, notifier_id: str = "recording") -> None:
        self.notifier_id = notifier_id
        self.messages: List[Dict[str, Any]] = []

    def send(self, message: str, *, severity: Severity = Severity.MEDIUM) -> bool:
        """Record the message. Always succeeds, never delivers."""
        self.messages.append({"message": message, "severity": severity.value})
        return True

    @property
    def count(self) -> int:
        """How many messages would have reached the owner."""
        return len(self.messages)


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
    #: Remembers what the owner has already been told, across restarts. Without
    #: it, every watcher restart re-announces every open problem.
    ledger: Any = None
    #: Groups incidents into the underlying problem they are evidence of. Every
    #: dedup decision keys on the outage this returns; without it the router
    #: falls back to the fingerprint, which is one message per probe.
    outages: Any = None
    #: Whether a CRITICAL detection may interrupt the owner on its own.
    #:
    #: Off. Detection is not an outcome — a probe failing, an incident opening
    #: and a severity rising are all the system working, and the owner hears
    #: either the fix or the actionable escalation. The deferral machinery below
    #: is kept and still correct for an operator who turns this on knowing what
    #: it costs; :meth:`rolled_back` is the one detection-shaped event that is
    #: still sent unconditionally, because a live change having been undone is
    #: a fact about production the owner cannot get any other way.
    alert_on_critical: bool = False

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
        """An incident has opened. Silent.

        A probe failing, an incident opening, a severity rising: these are the
        system working, and JARVIS exists precisely so the owner does not have
        to watch them. Detection is not an outcome. The owner hears either the
        fix or an escalation that names something for them to do, never the
        commentary in between — and never a message that arrives *before*
        JARVIS knows which of those two it is going to be.

        That last point is why :attr:`alert_on_critical` defaults to off rather
        than to a short grace period. A CRITICAL that JARVIS may not repair
        escalates to ``HUMAN_REQUIRED`` seconds later, and the owner used to get
        both: "something serious happened", then "I need your help". Holding the
        first briefly made the pair rarer without making it impossible — a
        repair that takes four minutes to exhaust itself outruns any grace
        window worth having.

        The deferral machinery below is kept and still correct, for an operator
        who turns detection alerts back on knowing what they cost. When enabled,
        a CRITICAL is held for :attr:`critical_grace_seconds`, cancelled if an
        escalation for the same *outage* supersedes it, and sent otherwise — so
        a standalone CRITICAL that nothing follows still gets through.
        """
        if not self.alert_on_critical:
            logger.info(
                "incident %s (%s) handled silently; detection is not an "
                "outcome and no notification was sent",
                incident.id,
                incident.severity.value,
            )
            return False

        if incident.severity is not Severity.CRITICAL:
            logger.info(
                "incident %s (%s) handled silently; no notification sent",
                incident.id,
                incident.severity.value,
            )
            return False

        self._outage(incident)
        if not self._is_news(incident):
            return False

        message = render_alert(incident, persona=self.persona)
        if self.critical_grace_seconds <= 0:
            self._remember(incident)
            return self.notify(message, severity=Severity.CRITICAL)

        # Not recorded yet: a held alert has not been said. Recording here would
        # make an escalation that supersedes it look like a repeat and silence
        # the only message the owner actually needs.
        #
        # Held per *outage*, not per incident: when a deployment failure opens
        # three incidents and one of them escalates, the escalation has to be
        # able to cancel a hold raised by either of the other two.
        self._defer_alert(self._identity(incident), message, incident)
        return False

    # -- what the owner already knows -------------------------------------

    def _is_news(self, incident: Incident, *, ask: Optional[OwnerAsk] = None) -> bool:
        """Whether telling the owner would tell them anything."""
        if self.ledger is None:
            return True
        try:
            return bool(self.ledger.should_notify(incident, ask=ask))
        except Exception:  # noqa: BLE001 - a broken ledger must not silence us
            logger.exception("could not consult the notification ledger")
            return True

    def _remember(self, incident: Incident, *, ask: Optional[OwnerAsk] = None) -> None:
        if self.ledger is None:
            return
        try:
            self.ledger.record(incident, ask=ask)
        except Exception:  # noqa: BLE001
            logger.exception("could not record the notification")

    def _was_told(self, incident: Incident, *, when_unknown: bool = True) -> bool:
        """Whether the owner is currently carrying an unresolved escalation.

        The gate on every success message. An outage that never reached them
        does not get a "it's fixed" — that sentence only means something as the
        end of a conversation they were part of, and sending it to somebody who
        heard nothing about the problem is how "everything is working normally"
        becomes the noisiest message of the day.

        ``when_unknown`` is what to assume with no ledger, and the two callers
        answer it differently on purpose. A repair JARVIS *performed* reports
        itself when there is no memory to consult — losing a genuine success is
        worse than repeating one. A fault that recovered on its own does not:
        without a record of having woken somebody there is no conversation to
        end, and announcing every transient blip that cleared itself is the
        exact noise this module exists to remove.
        """
        if self.ledger is None:
            return when_unknown
        try:
            return bool(self.ledger.was_told(incident))
        except Exception:  # noqa: BLE001
            logger.exception("could not consult the notification ledger")
            return when_unknown

    # -- one problem, however many probes ---------------------------------

    def _outage(self, incident: Incident) -> Any:
        """The correlated outage for *incident*, and its key on the incident.

        Assigning writes ``metadata["outage_key"]`` back onto the incident, so
        everything downstream — the ledger, the Control Center, the audit
        entry — agrees about which problem this is without re-deriving it.
        """
        if self.outages is None:
            return None
        try:
            outage = self.outages.assign(incident)
        except Exception:  # noqa: BLE001 - correlation is an optimisation
            logger.exception("could not correlate %s into an outage", incident.id)
            return None
        try:
            incident.metadata["outage_key"] = outage.key
        except Exception:  # noqa: BLE001
            pass
        return outage

    def _identity(self, incident: Incident) -> str:
        """The dedup identity: the outage when there is one, else fingerprint."""
        from openjarvis.reliability.notify_ledger import owner_identity

        return owner_identity(incident)

    def _subjects(self, incident: Incident, outage: Any) -> List[str]:
        """Every component the owner would name, for a grouped message."""
        from openjarvis.reliability.owner_ask import owner_subjects

        return owner_subjects(incident, outage)

    # -- deferral ---------------------------------------------------------

    def _defer_alert(
        self, identity: str, message: str, incident: Optional[Incident] = None
    ) -> None:
        """Hold a CRITICAL alert, so an escalation can supersede it."""
        incident_id = identity
        with self._alert_lock:
            existing = self._deferred.pop(incident_id, None)
            if existing is not None:
                existing.cancel()
            timer = self._scheduler(
                self.critical_grace_seconds,
                lambda: self._send_deferred(incident_id, message, incident),
            )
            self._deferred[incident_id] = timer
        timer.daemon = True
        timer.start()
        logger.info(
            "incident %s: holding the CRITICAL alert for %.0fs in case it escalates",
            incident_id,
            self.critical_grace_seconds,
        )

    def _send_deferred(
        self, incident_id: str, message: str, incident: Optional[Incident] = None
    ) -> None:
        """Nothing superseded it, so the owner hears about it after all."""
        with self._alert_lock:
            self._deferred.pop(incident_id, None)
        if incident is not None:
            # Recorded only now, at the moment it is actually said. A held alert
            # that was cancelled must leave no trace, or it would silence the
            # escalation that replaced it.
            self._remember(incident)
        self.notify(message, severity=Severity.CRITICAL)

    def _supersede(self, identity: str) -> bool:
        """Drop a held alert because a better message is going out instead.

        Keyed on the outage identity, so an escalation raised by *any* incident
        in a group cancels a hold raised by any other. Three probes failing on
        one deployment must not produce "something serious happened" from the
        homepage alongside "I need your help" from login.
        """
        incident_id = identity
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
        """The success message, in pull-request mode.

        Sent only when the owner was told there was a problem. A fault they
        never heard about, fixed before it needed them, is not good news — it is
        an interruption about something they were deliberately spared, and it
        is the reason "most incidents are handled silently" has to mean
        *silently at both ends*.

        HIGH regardless of the incident's own severity: this is the outcome the
        owner stayed quiet for, and the severity that got them here was about
        the fault, not about the news that it is over.
        """
        return self._succeed(
            incident,
            build=lambda outage: render_resolved(
                incident,
                attempt=attempt,
                verification=verification,
                persona=self.persona,
            ),
        )

    def _succeed(
        self,
        incident: Incident,
        *,
        build: Callable[[Any], str],
        when_unknown: bool = True,
    ) -> bool:
        """One success message per outage, and only to somebody who was told.

        Every route to "it works again" — a verified repair, a merge that
        reached production, the problem clearing on its own after the owner was
        already woken — comes through here, so the three of them can never
        become three messages about one recovery.
        """
        outage = self._outage(incident)
        identity = self._identity(incident)
        self._supersede(identity)

        if not self._was_told(incident, when_unknown=when_unknown):
            logger.info(
                "%s resolved; the owner was never told it broke, so nothing is sent",
                incident.id,
            )
            self._close_outage(outage)
            return False

        message = build(outage)
        sent = self.notify(message, severity=Severity.HIGH)
        if sent and self.ledger is not None:
            try:
                # Recorded, not forgotten: a second component of the same
                # outage reaching RESOLVED must not send a second "it's fixed".
                self.ledger.record_fixed(incident)
            except Exception:  # noqa: BLE001
                logger.exception("could not record the success in the ledger")
        self._close_outage(outage)
        return sent

    def _close_outage(self, outage: Any) -> None:
        if outage is None or self.outages is None:
            return
        try:
            self.outages.resolve(outage.key)
        except Exception:  # noqa: BLE001
            logger.exception("could not close outage %s", getattr(outage, "key", ""))

    def recovered(self, incident: Incident, *, recovery_type: Any = None) -> bool:
        """Silent — unless the owner is still holding an open escalation.

        A fault that stopped on its own needed nobody, and reporting it is the
        definition of noise. But if JARVIS already woke somebody about this
        outage, they are waiting on it, and leaving them waiting because the
        recovery happened to be external rather than repaired is a distinction
        that matters to the metrics and to nobody else.

        So: no owner escalation outstanding, nothing sent. One outstanding, the
        single success message, and the ledger closes the account.
        """
        if not self._was_told(incident, when_unknown=False):
            logger.info(
                "incident %s recovered without a repair (not notified)", incident.id
            )
            self._close_outage(self._outage(incident))
            return False
        return self._succeed(
            incident,
            when_unknown=False,
            build=lambda outage: render_fixed(
                incident,
                subjects=self._subjects(incident, outage),
                persona=self.persona,
            ),
        )

    def human_required(
        self, incident: Incident, *, reason: str, attempts: int, max_attempts: int
    ) -> bool:
        """Notify that a human is needed.

        Always sent at CRITICAL regardless of the incident's own severity: an
        escalation the owner never sees is the same as no escalation.

        Supersedes any CRITICAL alert still being held for this incident. Both
        describe the same event; this one also says what to do about it.

        Two gates stand in front of it, and both exist because of messages a
        real owner received and could do nothing with.

        **There must be a specific ask.** The escalation assembles an
        :class:`~openjarvis.reliability.owner_ask.OwnerAsk` first, and if that
        cannot name an operator action, nothing is sent. "The website needs
        you" and "I could not fix it safely" are statuses, not requests; they
        belong in Control Center, which is where the incident is parked with the
        reason it could not be handed over. JARVIS keeps investigating.

        **It must not already have been said.** The ledger keys on the
        correlated outage and on a digest of the ask itself, so an outage that
        sits in ``HUMAN_REQUIRED`` across a dozen watcher cycles, two restarts
        and three fresh incident ids is announced once — and announced again
        only when what is being asked of the owner actually changes.
        """
        outage = self._outage(incident)
        identity = self._identity(incident)
        ask = build_owner_ask(
            incident,
            reason=reason,
            outage=outage,
            attempts=attempts,
            max_attempts=max_attempts,
        )
        self._record_ask(incident, ask)

        if not ask.actionable:
            logger.info(
                "incident %s: not escalating to the owner — %s. Parked in "
                "Control Center.",
                incident.id,
                ask.parked_reason or "no operator action could be named",
            )
            # A held CRITICAL is *not* superseded here: there is no better
            # message coming, so whatever the alert policy decided still
            # stands.
            return False

        self._supersede(identity)
        if not self._is_news(incident, ask=ask):
            return False
        self._remember(incident, ask=ask)
        return self.notify(
            render_ask(ask, persona=self.persona), severity=Severity.CRITICAL
        )

    def _record_ask(self, incident: Incident, ask: OwnerAsk) -> None:
        """Store the ask on the incident, so Control Center can show it.

        Written whether or not it is sent — an escalation that was *withheld*
        is exactly the thing an operator looking at a parked incident needs to
        see, along with the reason no action could be named.
        """
        try:
            incident.metadata["owner_ask"] = ask.to_dict()
        except Exception:  # noqa: BLE001
            logger.exception("could not record the owner ask for %s", incident.id)

    def rolled_back(self, incident: Incident, *, reason: str) -> bool:
        """Notify that a live deployment was undone.

        The one detection-shaped event still sent unconditionally. Production
        changed underneath the owner, JARVIS did it, and there is no other way
        for them to find out at the moment it matters. Still deduplicated on
        the outage, so undoing one deployment that three probes were failing on
        is one message.
        """
        self._outage(incident)
        identity = self._identity(incident)
        self._supersede(identity)
        if not self._is_news(incident):
            return False
        self._remember(incident)
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
        """The success message, in live mode.

        Same gate as every other route to "it works again": one per outage, and
        only to an owner who was told it was broken.
        """
        return self._succeed(
            incident,
            build=lambda outage: render_fixed(
                incident,
                subjects=self._subjects(incident, outage),
                persona=self.persona,
            ),
        )

    def post_merge_failed(
        self, incident: Incident, *, record: Any, result: Any
    ) -> bool:
        """Notify that a merge landed and production did not verify.

        CRITICAL unconditionally. The rate limiter lets CRITICAL through by
        design, and this is the message that exists for that exemption: the
        change is live, unreviewed, and unproven.

        Supersedes a held alert for the same outage, for the same reason the
        escalation does. Routed through the same ask machinery as every other
        escalation, so the message names the rollback the owner has to make
        rather than reporting that verification failed.
        """
        rule = str(getattr(result, "rule", "") or "")
        pr_number = int(getattr(record, "pr_number", 0) or 0)
        reason = f"post-merge: production did not verify ({rule or 'unknown rule'})"
        if pr_number:
            try:
                incident.metadata.setdefault("pr_number", pr_number)
            except Exception:  # noqa: BLE001
                pass
        return self.human_required(
            incident, reason=reason, attempts=incident.attempts_used, max_attempts=0
        )

    # -- internals --------------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - 3600.0
        self._sent_times = [t for t in self._sent_times if t > cutoff]
        self._recent = {
            k: t for k, t in self._recent.items() if now - t < self.dedup_window_seconds
        }

    def _prepare(self, message: str) -> str:
        """Redact outbound content before it leaves the device.

        Both layers run, always, and the order matters.

        ``BoundaryGuard`` is the richer of the two, but it does not fail loudly
        when its Rust-backed scanners are missing: it logs a warning, disables
        the scanners and returns the text *unchanged*. Because that is not an
        exception, an ``except`` clause guarding this call never fires, and the
        fallback it guards never runs — which is how a GitHub token reached a
        notifier verbatim. A degraded guard is indistinguishable from a clean
        message, so it cannot be the only thing standing between a credential
        and Telegram.

        ``CredentialStripper`` is pure Python with no native dependency, so it
        works everywhere and is applied unconditionally afterwards. Running it
        over already-redacted text is a cheap no-op; running it over text the
        guard silently passed through is the whole point.
        """
        if not self.redact:
            return message
        from openjarvis.security.credential_stripper import CredentialStripper

        prepared = message
        try:
            from openjarvis.security.boundary import BoundaryGuard

            prepared = BoundaryGuard(mode="redact").scan_outbound(
                prepared, destination=self.notifier.notifier_id
            )
        except Exception:
            logger.exception("outbound redaction failed; the stripper still runs")

        try:
            return CredentialStripper().strip(prepared)
        except Exception:
            # Nothing can vouch for this text, so send the part that is known
            # safe rather than the part that might carry a credential.
            logger.exception("credential stripping failed; withholding the body")
            return "(message withheld: outbound redaction is unavailable)"
