"""One door, for everything the owner says.

Wiz has two halves and the owner has one phone. The reliability half answers
"Fix it" and "is the site up"; the product half answers "add a download button"
and "what is Claude working on". Giving each half its own Telegram listener
would give the owner two bots, two allowlists to keep in step, and — because
Telegram refuses a second long-poll on one token — one of them silently not
running. So there is one door, here, and it decides which half a sentence
belongs to.

The decision is deliberately not a model's, and not a coin flip either. It is
two questions asked in order:

**Is this the narrow reliability instruction, and is there something for it to
act on?** The reliability owner-command table is a closed set of phrases with a
single effect — clear a repair cooldown — and it wins only when an outage is
actually open. That second condition is what stops "fix the sign-up form" on a
quiet afternoon being answered with "nothing is failing at the moment" instead
of being recorded as work.

**Otherwise, what does the dispatcher make of it?** Everything else goes to
:class:`~openjarvis.wiz.brain.Wiz` through :class:`TelegramIntake`, which
classifies it against the registered verb table, checks the capability exists
and is configured, checks the risk, and checks the channel's authority. A
sentence that names no verb is answered with an offer to list what Wiz can do —
never guessed at.

Three properties this file is responsible for:

**One allowlist.** The chat ids the notifier already sends *to* are the chat ids
it accepts *from*. There is no second list to drift, and an empty list accepts
nobody.

**The reply is a reply.** It goes out through the transport directly rather than
through the notification router: an answer to something the owner just sent must
not consume the hourly cap, be deduplicated against an unrelated alert, or be
dropped by a severity floor. Conversely it must never *become* a notification —
the owner asking a question should not put anything in the ledger.

**Silence for strangers.** A message from an unlisted chat is recorded and not
answered. Telling an unknown sender that this chat is connected to something is
telling them what to spoof.

Nothing here can widen authority. The Telegram ceiling is ``CODE_WRITE`` in
:mod:`openjarvis.wiz.authority`, in source, so no configuration reachable from
this file can let a chat message merge a pull request or change production.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

__all__ = ["OwnerDoor", "OwnerReply", "SeenMessages", "TelegramOwnerDoor"]


@dataclass
class SeenMessages:
    """Which (chat id, message id) pairs have already been handled.

    Telegram may redeliver an update — a dropped connection, a retried
    webhook — and a message id is stable and per-chat, so keying on the pair
    is exact rather than a text-similarity heuristic (which would also wrongly
    collapse two genuinely separate messages that happen to say the same
    thing). Persisted to disk when a path is given, so a redelivery that
    lands after a watcher restart is still recognised — in-memory-only
    otherwise, which still protects against redelivery within one process's
    lifetime.
    """

    path: Optional[Path] = None
    _seen: Set[Tuple[str, str]] = field(default_factory=set, repr=False)
    _loaded: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def already_handled(self, chat_id: Any, message_id: Any) -> bool:
        """Whether this exact message was already handled.

        A missing message id (an older channel, a message the transport
        does not give one to) can never be judged a duplicate — treating
        "unknown" as "duplicate" would silently drop real messages.
        """
        mid = str(message_id or "")
        if not mid:
            return False
        with self._lock:
            self._ensure_loaded()
            return (str(chat_id), mid) in self._seen

    def record(self, chat_id: Any, message_id: Any) -> None:
        mid = str(message_id or "")
        if not mid:
            return
        with self._lock:
            self._ensure_loaded()
            self._seen.add((str(chat_id), mid))
            self._save()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError):
            logger.exception("could not read the seen-messages ledger")
            return
        self._seen = {(str(pair[0]), str(pair[1])) for pair in data}

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(sorted(self._seen)))
        except OSError:
            logger.exception("could not persist the seen-messages ledger")


@dataclass(frozen=True)
class OwnerReply:
    """What the door decided, and what it will say back."""

    #: The text to send. Empty means send nothing — the correct answer to a
    #: stranger, and to a message that needed no answer.
    text: str = ""
    #: ``"reliability"``, ``"wiz"``, ``"refused"`` or ``""``.
    route: str = ""
    #: The verb the dispatcher chose, when it reached one.
    capability: str = ""
    handled: bool = False
    authorized: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize, for the journal and the Control Center."""
        return {
            "route": self.route,
            "capability": self.capability,
            "handled": self.handled,
            "authorized": self.authorized,
            "text": self.text,
        }


@dataclass
class OwnerDoor:
    """Routes one owner message to the half of Wiz that should answer it.

    Parameters
    ----------
    commands:
        :class:`~openjarvis.reliability.owner_commands.OwnerCommands`. The
        narrow reliability table. ``None`` means this deployment has no
        reliability half and everything goes to the dispatcher.
    intake:
        :class:`~openjarvis.wiz.intake.TelegramIntake`. ``None`` means no
        product half, and unrecognised messages are answered honestly rather
        than silently dropped.
    allowed_chat_ids:
        The owner allowlist. Empty authorises nobody.
    outages:
        The outage registry, consulted for the one question that decides
        whether "fix it" means the reliability half: is anything failing?
    seen:
        :class:`SeenMessages`. A redelivered update — Telegram's own retry,
        not a second thing the owner typed — must not create a second
        FeatureRequest. Defaults to an in-memory-only instance, which still
        protects within one process's lifetime; give it a path to also
        survive a restart.
    """

    commands: Any = None
    intake: Any = None
    allowed_chat_ids: Any = ""
    outages: Any = None
    persona: bool = True
    seen: "SeenMessages" = field(default_factory=lambda: SeenMessages())

    def receive(
        self, *, chat_id: Any, text: str, sender: str = "", message_id: Any = ""
    ) -> OwnerReply:
        """Handle one inbound message. Never raises."""
        try:
            return self._receive(
                chat_id=chat_id, text=text, sender=sender, message_id=message_id
            )
        except Exception:  # noqa: BLE001 - an inbound message must not be able
            logger.exception("owner message handling failed")  # ...to stop Wiz.
            return OwnerReply(
                text=self._say("something went wrong handling that."),
                route="refused",
            )

    # -- routing -----------------------------------------------------------

    def _receive(
        self, *, chat_id: Any, text: str, sender: str, message_id: Any = ""
    ) -> OwnerReply:
        from openjarvis.reliability.owner_commands import authorized_chat

        if not authorized_chat(chat_id, self.allowed_chat_ids):
            logger.warning(
                "owner message from an unauthorised chat was ignored (%s)",
                (str(chat_id)[:8] + "…") if chat_id else "<empty>",
            )
            return OwnerReply(route="refused", authorized=False)

        message = str(text or "").strip()
        if not message:
            return OwnerReply(route="", authorized=True)

        if self.seen.already_handled(chat_id, message_id):
            # A redelivery of something already acted on. Silent — the owner
            # already got their answer the first time, and answering twice
            # is indistinguishable from Wiz not remembering what it did.
            logger.info(
                "ignored a redelivered message (chat=%s, message_id=%s)",
                (str(chat_id)[:8] + "…") if chat_id else "<empty>",
                message_id,
            )
            return OwnerReply(route="duplicate", authorized=True)
        self.seen.record(chat_id, message_id)

        if self._is_live_reliability_instruction(message):
            return self._reliability(chat_id=chat_id, text=message)

        return self._dispatch(chat_id=chat_id, text=message, sender=sender)

    def _is_live_reliability_instruction(self, text: str) -> bool:
        """Whether the reliability half should take this one.

        Both halves have a claim on the word "fix", and the tie is broken by
        the world rather than by the wording: if something is actually failing,
        "fix it" is about that. If nothing is, the same sentence is a request
        for work, and answering it with "nothing is failing at the moment"
        would be technically true and completely useless.
        """
        if self.commands is None:
            return False
        from openjarvis.reliability.owner_commands import interpret

        intent = interpret(text)
        if not intent:
            return False
        if intent == "status":
            # "What's happening?" is always answerable by the reliability half,
            # which knows whether anything is wrong; the product half only
            # knows what is being built.
            return True
        return bool(self._open_outages())

    def _open_outages(self) -> List[Any]:
        if self.outages is None:
            return []
        try:
            return list(self.outages.open_outages())
        except Exception:  # noqa: BLE001
            logger.exception("could not read the outage registry")
            return []

    def _reliability(self, *, chat_id: Any, text: str) -> OwnerReply:
        result = self.commands.handle(chat_id=chat_id, text=text)
        return OwnerReply(
            text=str(getattr(result, "reply", "") or ""),
            route="reliability",
            capability=f"reliability.{getattr(result, 'intent', '') or 'unknown'}",
            handled=bool(getattr(result, "executed", False)),
            authorized=True,
        )

    def _dispatch(self, *, chat_id: Any, text: str, sender: str) -> OwnerReply:
        if self.intake is None:
            return OwnerReply(
                text=self._say(
                    'I can tell you about the site, and I can act on "Fix it". '
                    "Building things is not configured here yet."
                ),
                route="refused",
                authorized=True,
            )

        from openjarvis.wiz.owner_speech import render

        result = self.intake.receive(chat_id=chat_id, text=text, sender=sender)
        capability = str(getattr(result, "capability", "") or "")
        reply = str(getattr(result, "reply", "") or "").strip()

        # Auto-build after recording: feature.request on Telegram should seamlessly
        # start execution (feature.build) so the owner gets smooth UX from one message
        if (capability == "feature.request"
            and getattr(result, "accepted", False)
            and getattr(result, "feature_id", "")):
            feature_id = str(result.feature_id)
            build_result = self._auto_build_feature(feature_id, sender, chat_id)
            if build_result.get("started", False):
                # Auto-build succeeded; update response
                capability = "feature.request+build"
                reply = f"{reply} Starting work now."
            else:
                # Auto-build failed; include reason in reply. `or`, not
                # dict.get's default: a present-but-empty detail (any read
                # capability's result, which has no "say" key at all) must
                # still fall back to something the owner can see, not a
                # reply that looks identical to a clean success.
                detail = build_result.get("detail") or "could not start building"
                reply = f"{reply} {detail}"

        if not reply and getattr(result, "accepted", False):
            # A read verb answered with facts and no words. The intake only
            # forwards a handler's own sentence, and the read verbs deliberately
            # produce none — so the owner asking "how is Wize?" used to get
            # silence. Rendering happens here rather than in the intake because
            # it is owner-facing English, and the intake is a security boundary
            # that should not also be a copywriter.
            reply = render(
                capability, getattr(result, "structured", None), persona=self.persona
            )

        if not reply:
            reply = self._say("I did not understand that one.")

        return OwnerReply(
            text=reply,
            route="wiz",
            capability=capability,
            handled=bool(getattr(result, "accepted", False)),
            authorized=True,
        )

    def _auto_build_feature(self, feature_id: str, sender: str, chat_id: Any) -> Dict[str, Any]:
        """Automatically start building a just-recorded feature.

        Called after feature.request succeeds to seamlessly transition to
        execution. Uses the same authority path as explicit feature.build
        calls — same actor, same channel, same policy check.

        Dispatched with the capability named explicitly
        (``wiz.handle(request, capability="feature.build")``) rather than by
        classifying the synthetic ``"build {feature_id}"`` text: there is no
        intent rule for ``feature.build`` at all, and any text containing a
        feature id — this synthetic text always does — is outranked by
        ``feature.status``'s ``\\bFEAT-\\d+\\b`` pattern (weight 15, against
        ``feature.request``'s weight 12, the only other candidate). So every
        synthetic auto-build request was silently misrouted to a read-only
        status lookup instead of ever reaching the build handler — no
        exception, no journal entry, nothing but an unremarkable Telegram
        reply, because ``feature.status``'s result has no ``"started"`` or
        ``"say"`` key for this method to notice was missing. ``Wiz.handle``'s
        ``capability`` parameter exists for exactly this — a caller that
        already knows the verb, the same mechanism the Control Center's own
        buttons use — and this is exactly that caller.
        """
        if self.intake is None or not hasattr(self.intake, "wiz"):
            return {"started": False, "detail": "build system not configured"}

        try:
            from openjarvis.wiz.brain import Request
            from openjarvis.wiz.authority import Channel
            from openjarvis.wiz.authority import Actor

            # Build request using same authority as the feature.request
            actor = Actor(
                actor_id=str(sender or chat_id),
                channel=Channel.TELEGRAM,
                authenticated=True,
            )
            request = Request(
                text=f"build {feature_id}",
                actor=actor,
                arguments={"feature_id": feature_id},
            )

            outcome = self.intake.wiz.handle(request, capability="feature.build")
            if not outcome.handled:
                reason = str(outcome.message or "not authorized to build")
                self._journal_auto_build_outcome(
                    feature_id, kind="feature.auto_build_refused", reason=reason
                )
                return {"started": False, "detail": reason}

            result = outcome.result if isinstance(outcome.result, dict) else {}
            started = bool(result.get("started", False))
            detail = str(result.get("say", ""))
            if not started:
                self._journal_auto_build_outcome(
                    feature_id,
                    kind="feature.auto_build_refused",
                    reason=detail or "did not start",
                )
            return {"started": started, "detail": detail}
        except Exception as exc:
            logger.exception("auto-build failed for %s", feature_id)
            self._journal_auto_build_outcome(
                feature_id, kind="feature.auto_build_failed", reason=str(exc)[:500]
            )
            return {"started": False, "detail": str(exc)}

    def _journal_auto_build_outcome(
        self, feature_id: str, *, kind: str, reason: str
    ) -> None:
        """Make an auto-build refusal or failure survive past the Telegram
        reply that carried it.

        Independent of whatever caused it: a Telegram reply is not durable
        evidence — it rotates out of the transport's own history, and
        nothing here re-reads it — so future diagnosis of "why didn't this
        build" must not depend on it. Bounded and secret-free: *reason*
        comes from ``outcome.message`` (fixed, human-authored refusal text)
        or a caught exception's ``str()``, truncated, never from anything an
        attacker controls or that could carry a credential.
        """
        journal = getattr(self.intake, "journal", None)
        if journal is None:
            return
        try:
            clock = getattr(self.intake, "clock", None)
            journal.record(
                at=clock() if clock else "",
                kind=kind,
                capability="feature.build",
                actor_id="telegram",
                channel="telegram",
                reason=reason[:500],
                detail={"feature_id": feature_id},
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "could not journal an auto-build outcome for %s", feature_id
            )

    def _say(self, text: str) -> str:
        return f"Sir, {text}" if self.persona else text[:1].upper() + text[1:]


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------


@dataclass
class TelegramOwnerDoor:
    """Attaches :class:`OwnerDoor` to the Telegram channel Wiz already uses.

    The same channel object the notifier sends through, so the credential, the
    chat id and the allowlist are one set of facts rather than three.

    **Run one listener, not two.** The reliability watcher can start its own
    narrow listener when ``[reliability.notify] accept_owner_commands`` is on;
    this door supersedes it and answers strictly more. Telegram refuses a second
    long-poll on the same bot token, so running both does not silently duplicate
    anything — the second one fails visibly — but the operator should still pick
    one, and this is the one to pick when the product half is configured.
    """

    door: OwnerDoor
    notifier: Any = None
    _connected: bool = field(default=False, repr=False)

    def start(self) -> bool:
        """Begin listening. Returns whether a listener was actually attached."""
        channel = self._channel()
        if channel is None:
            logger.info(
                "the owner door is enabled but the notification channel cannot "
                "receive; nothing is listening"
            )
            return False
        try:
            channel.on_message(self._on_message)
            channel.connect()
        except Exception:  # noqa: BLE001 - a listener that cannot start must
            logger.exception("could not start the owner door")
            return False  # ...not take Wiz down with it.
        self._connected = True
        logger.info("listening for the owner on Telegram")
        return True

    def stop(self) -> None:
        """Stop listening."""
        channel = self._channel()
        if channel is None or not self._connected:
            return
        try:
            channel.disconnect()
        except Exception:  # noqa: BLE001
            logger.exception("could not stop the owner door")
        self._connected = False

    # -- internals ---------------------------------------------------------

    def _channel(self) -> Any:
        channel = getattr(self.notifier, "channel", None)
        if channel is None or not hasattr(channel, "on_message"):
            return None
        return channel

    def _on_message(self, message: Any) -> None:
        """One inbound message. Never raises into the channel's thread."""
        chat_id = getattr(message, "conversation_id", "") or getattr(
            message, "sender", ""
        )
        reply = self.door.receive(
            chat_id=chat_id,
            text=getattr(message, "content", ""),
            sender=str(getattr(message, "sender", "") or ""),
            message_id=str(getattr(message, "message_id", "") or ""),
        )
        if not reply.text:
            return
        self._reply(str(getattr(message, "conversation_id", "")), reply.text)

    def _reply(self, chat_id: str, text: str) -> None:
        channel = self._channel()
        if channel is None or not chat_id:
            return
        try:
            channel.send(chat_id, text)
        except Exception:  # noqa: BLE001
            logger.exception("could not reply to the owner")


def build_owner_door(
    config: Any,
    *,
    runtime: Any = None,
    commands: Any = None,
    outages: Any = None,
) -> Optional[OwnerDoor]:
    """Assemble the door from configuration, or return ``None``.

    ``None`` when owner commands are switched off, which is the default. An
    inbound control path should exist because somebody turned it on, not
    because a file was missing.
    """
    rc = getattr(config, "reliability", None)
    notify = getattr(rc, "notify", None)
    if not (
        getattr(notify, "enabled", False)
        and getattr(notify, "accept_owner_commands", False)
    ):
        return None

    allowed = getattr(getattr(config, "channel", None), "telegram", None)
    allowed_chat_ids = getattr(allowed, "allowed_chat_ids", "") or ""

    intake = None
    if runtime is not None and getattr(runtime, "wiz", None) is not None:
        from openjarvis.wiz.intake import TelegramIntake

        intake = TelegramIntake(
            wiz=runtime.wiz,
            owner_chat_ids=[
                c.strip() for c in str(allowed_chat_ids).split(",") if c.strip()
            ],
            journal=getattr(runtime, "journal", None),
        )

    from openjarvis.wiz.runtime import wiz_home

    return OwnerDoor(
        commands=commands,
        intake=intake,
        allowed_chat_ids=allowed_chat_ids,
        outages=outages,
        persona=bool(getattr(notify, "persona", True)),
        # On disk, not just in memory: a redelivery that lands after the
        # watcher restarts must still be recognised as the same message.
        seen=SeenMessages(path=wiz_home() / "telegram_seen.json"),
    )
