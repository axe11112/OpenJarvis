"""What the owner may say back, and what it is allowed to change.

An escalation that ends "reply *Fix it* to let me continue" has to mean
something, and the moment a message from a phone can start work, the question
stops being about convenience and becomes about authority. This module answers
it with the same three rules that hold Sir Voice together, because the threat
model is the same and the transport is worse: a Telegram chat is a text field
anyone who learns a chat id might try to write into.

**An allowlist, checked first.** A message is authorised by its chat id against
the channel's existing ``allowed_chat_ids`` — the same list the notifier
already sends *to*. Nothing else is read, and an unrecognised sender is
refused before its text is interpreted, so a stranger cannot even discover
which phrases exist.

**An enumerable action set.** Matching is deterministic phrase matching against
:data:`COMMANDS`. There is no model in this path deciding what a sentence
means, because every safety property here rests on the set of reachable actions
being reviewable in one screenful, and a model that can be argued into a tool
call is not.

**One direction of travel.** The only thing "Fix it" changes is a *cooldown* —
the operational pacing that stops JARVIS retrying something it just failed at.
It does not clear the emergency stop, raise the attempt ceiling, approve a
merge, relax a verification gate, widen the repair scope, or touch production.
Those refuse for reasons a message from a phone does not answer, and the
correct response to "but I said fix it" is that they still refuse.

The reply is deliberately one line. An owner who says "Fix it" wants the thing
fixed, not a conversation about it: JARVIS acknowledges once and then goes
quiet until it is fixed or until something genuinely needs deciding.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from openjarvis.reliability.types import now_iso

logger = logging.getLogger(__name__)

__all__ = [
    "COMMANDS",
    "OwnerCommandResult",
    "OwnerCommands",
    "authorized_chat",
    "interpret",
]


# ---------------------------------------------------------------------------
# The allowlist of things an owner message may mean
# ---------------------------------------------------------------------------

#: ``(name, phrases)``. Substring matching against a normalised message.
#:
#: Short and closed on purpose. Every addition widens what a text message can
#: reach, and the two that are here are the two an escalation actually invites:
#: carry on, or tell me where things stand. Everything else the owner might
#: want is a decision, and decisions are made in Control Center where there is
#: a screen and a session.
COMMANDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "fix_it",
        (
            "fix it",
            "fix this",
            "fix them",
            "please fix",
            "go ahead",
            "carry on",
            "keep going",
            "keep trying",
            "continue",
            "proceed",
            "sort it out",
            "you have my approval to continue",
            # Said when the operator does not know, or does not care, what
            # exactly is broken. It means the same thing as "Fix it" and used
            # to fall through to the product side as a request to build
            # something called "whatever is wrong with production".
            "fix whatever is wrong",
            "fix production",
            "fix the site",
            "fix the website",
        ),
    ),
    (
        "status",
        (
            "status",
            "what's happening",
            "whats happening",
            "what is happening",
            "where are we",
            "any update",
            "how is it going",
            "hows it going",
        ),
    ),
)

#: Phrases that must never be read as a command, even though they contain one.
#:
#: "Don't fix it" contains "fix it". A negation that is silently dropped is the
#: single most dangerous failure a phrase matcher can have, so it refuses to
#: interpret rather than guessing which half the owner meant.
_NEGATIONS = (
    "don't",
    "dont",
    "do not",
    "stop",
    "never",
    "no need",
    "not yet",
    "hold off",
    "leave it",
    "wait",
)


def _normalise(text: str) -> str:
    lowered = str(text or "").strip().lower()
    lowered = re.sub(r"[^a-z0-9'\s]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def interpret(text: str) -> str:
    """The command a message means, or ``""`` when it means none of them.

    Returns the empty string for anything unrecognised *and* for anything
    negated. Both cases are answered by saying nothing rather than by picking
    the nearest match: an owner whose "don't fix it" started a repair would be
    right never to trust the channel again.
    """
    normalised = _normalise(text)
    if not normalised:
        return ""
    if any(negation in normalised for negation in _NEGATIONS):
        logger.info("owner message contains a negation; not interpreting it")
        return ""
    for name, phrases in COMMANDS:
        if any(phrase in normalised for phrase in phrases):
            return name
    return ""


def authorized_chat(chat_id: Any, allowed_chat_ids: Any) -> bool:
    """Whether *chat_id* is on the owner allowlist.

    An empty allowlist authorises nobody. That is the opposite of how the
    outbound channel treats an empty list, and it is the correct asymmetry:
    "send to nobody" is a misconfiguration that loses a message, "accept from
    anybody" is a misconfiguration that hands over a control.
    """
    wanted = str(chat_id or "").strip()
    if not wanted:
        return False
    if isinstance(allowed_chat_ids, (list, tuple, set)):
        allowed = [str(c).strip() for c in allowed_chat_ids]
    else:
        allowed = [c.strip() for c in str(allowed_chat_ids or "").split(",")]
    return wanted in [c for c in allowed if c]


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerCommandResult:
    """What happened when an owner message was acted on."""

    #: What to send back. Empty means send nothing — the normal case for an
    #: unrecognised message, and the reason a stray text does not start a
    #: conversation.
    reply: str = ""
    intent: str = ""
    authorized: bool = False
    executed: bool = False
    outage_key: str = ""
    ambiguous: bool = False
    #: Incidents whose cooldown was cleared, for the audit entry.
    resumed: Tuple[str, ...] = ()
    at: str = field(default_factory=now_iso)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize, for the audit log."""
        return {
            "at": self.at,
            "intent": self.intent,
            "authorized": self.authorized,
            "executed": self.executed,
            "outage_key": self.outage_key,
            "ambiguous": self.ambiguous,
            "resumed": list(self.resumed),
            "reply": self.reply,
        }


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


@dataclass
class OwnerCommands:
    """Acts on messages from the owner. Refuses everything else.

    Parameters
    ----------
    allowed_chat_ids:
        The channel's owner allowlist, as configured. A comma-separated string
        or a sequence.
    outages:
        :class:`~openjarvis.reliability.outage.OutageRegistry`, to find which
        problem "it" refers to.
    store:
        Incident store, for the incidents inside that outage.
    gate:
        :class:`~openjarvis.reliability.watch.RepairGate`. The one thing a
        command may touch, and only its cooldowns.
    audit:
        ``(OwnerCommandResult) -> None``, called for every outcome including
        refusals. An unauthorised attempt is exactly the event worth recording.
    enabled:
        When false, every message is refused without being interpreted.
    """

    allowed_chat_ids: Any = ""
    outages: Any = None
    store: Any = None
    gate: Any = None
    audit: Optional[Callable[[OwnerCommandResult], None]] = None
    persona: bool = True
    enabled: bool = True

    def handle(self, *, chat_id: Any, text: str) -> OwnerCommandResult:
        """Interpret and act on one owner message."""
        if not self.enabled:
            logger.info("owner commands are disabled; message ignored")
            return self._record(OwnerCommandResult(intent="", authorized=False))

        if not authorized_chat(chat_id, self.allowed_chat_ids):
            # Refused before interpretation, and answered with silence. A
            # reply would confirm to an unknown sender that this chat is
            # connected to something worth talking to.
            logger.warning(
                "owner command from an unauthorised chat was refused (%s)",
                str(chat_id)[:8] + "…" if chat_id else "<empty>",
            )
            return self._record(OwnerCommandResult(intent="", authorized=False))

        intent = interpret(text)
        if not intent:
            return self._record(OwnerCommandResult(intent="", authorized=True))
        if intent == "status":
            return self._record(self._status())
        return self._record(self._fix_it())

    # -- the commands ------------------------------------------------------

    def _fix_it(self) -> OwnerCommandResult:
        """Resume safe autonomous work on the current problem."""
        candidates = self._open_outages()
        if not candidates:
            return OwnerCommandResult(
                reply=self._say("there is nothing failing at the moment."),
                intent="fix_it",
                authorized=True,
            )
        if len(candidates) > 1:
            return OwnerCommandResult(
                reply=self._which(candidates),
                intent="fix_it",
                authorized=True,
                ambiguous=True,
            )

        outage = candidates[0]
        resumed = self._resume(outage)
        if self.outages is not None:
            try:
                self.outages.acknowledge(
                    outage.key,
                    note='the owner replied "Fix it"; repair cooldowns cleared',
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not record the acknowledgement")
        return OwnerCommandResult(
            reply=self._say("I'm working on it."),
            intent="fix_it",
            authorized=True,
            executed=True,
            outage_key=outage.key,
            resumed=tuple(resumed),
        )

    def _status(self) -> OwnerCommandResult:
        """One line about what is currently broken. Reads, changes nothing."""
        candidates = self._open_outages()
        if not candidates:
            return OwnerCommandResult(
                reply=self._say("everything is working normally."),
                intent="status",
                authorized=True,
            )
        described = "; ".join(self._describe(o) for o in candidates[:3])
        return OwnerCommandResult(
            reply=self._say(f"still working on {described}."),
            intent="status",
            authorized=True,
            outage_key=candidates[0].key,
        )

    # -- helpers -----------------------------------------------------------

    def _open_outages(self) -> List[Any]:
        if self.outages is None:
            return []
        try:
            return list(self.outages.open_outages())
        except Exception:  # noqa: BLE001
            logger.exception("could not read the outage registry")
            return []

    def _resume(self, outage: Any) -> List[str]:
        """Clear the repair cooldown for everything in this outage.

        The complete list of what "Fix it" does. Every other reason a repair
        might not start — the emergency stop, the concurrency limit, the
        attempt ceiling, a protected path, a failed verification — is unchanged
        and will refuse again, which is the point.
        """
        if self.gate is None:
            return []
        keys: List[str] = []
        keys.extend(str(i) for i in getattr(outage, "incident_ids", []) or [])
        keys.extend(str(f) for f in getattr(outage, "fingerprints", []) or [])
        try:
            cleared = self.gate.clear_cooldown(*keys)
        except Exception:  # noqa: BLE001
            logger.exception("could not clear the repair cooldown")
            return []
        logger.info(
            "owner asked me to continue on outage %s; cleared cooldown for %s",
            getattr(outage, "key", ""),
            ", ".join(cleared) or "nothing",
        )
        return cleared

    def _describe(self, outage: Any) -> str:
        components = [str(c) for c in getattr(outage, "components", []) or []]
        if not components:
            return str(getattr(outage, "family", "") or "an open problem")
        if len(components) == 1:
            return components[0]
        return ", ".join(components[:-1]) + f" and {components[-1]}"

    def _which(self, candidates: Sequence[Any]) -> str:
        """One question, not a menu.

        Asked rather than guessed, because "Fix it" with two unrelated things
        broken is genuinely ambiguous, and picking the more recent one is how
        an owner ends up having authorised work on something they were not
        thinking about.
        """
        options = " or ".join(self._describe(o) for o in candidates[:3])
        return self._say(f"which one — {options}?")

    def _say(self, text: str) -> str:
        return f"Sir, {text}" if self.persona else text[:1].upper() + text[1:]

    def _record(self, result: OwnerCommandResult) -> OwnerCommandResult:
        if self.audit is None:
            return result
        try:
            self.audit(result)
        except Exception:  # noqa: BLE001
            logger.exception("could not audit the owner command")
        return result


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------


@dataclass
class OwnerCommandListener:
    """Attaches owner-command handling to the notification channel.

    Reuses the channel the notifier already sends on, which is what makes the
    allowlist the same list at both ends: the owner is whoever JARVIS was
    already configured to talk to. There is no second credential, no second
    chat id and no second place for the two to drift apart.

    The reply goes out through the transport directly rather than through
    :class:`~openjarvis.reliability.notify.NotificationRouter`. An
    acknowledgement is a reply to something the owner just sent, not an alert:
    it must not consume the hourly cap, must not be deduplicated against an
    unrelated message, and must not be suppressed by a severity floor.
    """

    commands: OwnerCommands
    notifier: Any = None
    _connected: bool = field(default=False, repr=False)

    def start(self) -> bool:
        """Begin listening. Returns whether a listener was actually attached."""
        channel = self._channel()
        if channel is None:
            logger.info(
                "owner commands are enabled but the notification channel "
                "cannot receive; nothing is listening"
            )
            return False
        try:
            channel.on_message(self._on_message)
            channel.connect()
        except Exception:  # noqa: BLE001 - a listener that cannot start must
            logger.exception("could not start the owner command listener")
            return False  # ...not take the watcher down with it.
        self._connected = True
        logger.info("listening for owner commands on the notification channel")
        return True

    def stop(self) -> None:
        """Stop listening."""
        channel = self._channel()
        if channel is None or not self._connected:
            return
        try:
            channel.disconnect()
        except Exception:  # noqa: BLE001
            logger.exception("could not stop the owner command listener")
        self._connected = False

    # -- internals ---------------------------------------------------------

    def _channel(self) -> Any:
        channel = getattr(self.notifier, "channel", None)
        if channel is None or not hasattr(channel, "on_message"):
            return None
        return channel

    def _on_message(self, message: Any) -> None:
        """One inbound message. Never raises into the channel's thread."""
        try:
            chat_id = getattr(message, "conversation_id", "") or getattr(
                message, "sender", ""
            )
            result = self.commands.handle(
                chat_id=chat_id, text=getattr(message, "content", "")
            )
        except Exception:  # noqa: BLE001
            logger.exception("owner command handling failed")
            return
        if not result.reply:
            return
        self._reply(str(getattr(message, "conversation_id", "")), result.reply)

    def _reply(self, chat_id: str, text: str) -> None:
        channel = self._channel()
        if channel is None or not chat_id:
            return
        try:
            channel.send(chat_id, text)
        except Exception:  # noqa: BLE001
            logger.exception("could not reply to the owner")
