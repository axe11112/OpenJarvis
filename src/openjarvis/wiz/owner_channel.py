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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from openjarvis.core.proclock import LeaseTimeout, ProcessLease
from openjarvis.reliability.statefile import write_json_atomic

logger = logging.getLogger(__name__)

__all__ = [
    "OwnerDoor",
    "OwnerReply",
    "SeenLedgerUnavailable",
    "SeenMessages",
    "TelegramOwnerDoor",
]

#: How many (chat, message) pairs to keep. Telegram stops redelivering an
#: update after 24 hours, so a bounded window is enough, and an unbounded one
#: is a file that grows for as long as the machine runs.
MAX_SEEN = 2000


class SeenLedgerUnavailable(RuntimeError):
    """Raised when it cannot be established whether a message is new.

    Deliberately not a boolean. Both answers are wrong when the ledger cannot
    be read: "new" acts on the owner's instruction a second time — a second
    feature request, a second branch, a second pull request — and "already
    handled" drops what they said in silence, which looks to them exactly like
    Wiz ignoring them. The caller is told it does not know, and says so.
    """


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

    **The operation that matters is :meth:`claim`, and it is one operation.**
    Asking "have you seen this?" and then saying "you have now" is two, with a
    gap between them, and everything this class is for lives in that gap. Two
    pollers — a restarting watcher overlapping its replacement, a webhook
    server beside a long-poller, the CLI beside either — both read "not seen",
    both return ``False``, and the owner's one sentence becomes two feature
    requests, two branches, two pull requests. A ``threading.Lock`` does not
    close that: the two processes each have their own.

    So the check and the record happen together, under a lease the whole
    machine shares, against the file re-read at that moment rather than a copy
    this process loaded at startup. Exactly one caller is told the message is
    new.
    """

    path: Optional[Path] = None
    #: How long to wait for the machine-wide lease. Every holder does a read,
    #: a compare and one atomic write, so a wait beyond this means a stuck
    #: process, not contention.
    lease_timeout: float = 10.0
    _seen: Set[Tuple[str, str]] = field(default_factory=set, repr=False)
    _order: List[Tuple[str, str]] = field(default_factory=list, repr=False)
    _loaded: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- the one operation --------------------------------------------------

    def claim(self, chat_id: Any, message_id: Any) -> bool:
        """Take responsibility for this message, or report it already taken.

        ``True`` means this caller — this thread, in this process, on this
        machine — is the one that should act on it, and it is recorded before
        this returns. ``False`` means somebody already has it.

        Raises :class:`SeenLedgerUnavailable` when it cannot tell, which is
        neither of those and must not be flattened into either.

        A message with no id cannot be judged a duplicate and is always
        claimed: treating "unknown" as "already handled" would silently drop
        real messages from any transport that does not number them.
        """
        mid = str(message_id or "")
        if not mid:
            return True
        key = (str(chat_id), mid)
        with self._lock:
            if self.path is None:
                # In-memory only. One process, one set, still atomic here
                # because both halves happen under this lock.
                self._ensure_loaded()
                if key in self._seen:
                    return False
                self._remember(key)
                return True
            try:
                with self._lease.acquire(
                    timeout=self.lease_timeout, reason=f"claim {mid}"
                ):
                    # Re-read under the lease. A copy loaded at startup cannot
                    # know what another process recorded since, and this
                    # process may have been running for weeks.
                    self._reload()
                    if key in self._seen:
                        return False
                    self._remember(key)
                    if not self._save():
                        raise SeenLedgerUnavailable(
                            f"could not persist the seen-messages ledger at {self.path}"
                        )
                    return True
            except LeaseTimeout as exc:
                raise SeenLedgerUnavailable(str(exc)) from exc
            except OSError as exc:
                raise SeenLedgerUnavailable(str(exc)) from exc

    # -- the older two-call form, kept for callers that only ask ------------

    def already_handled(self, chat_id: Any, message_id: Any) -> bool:
        """Whether this exact message was already handled.

        A read, with no claim attached, so two callers can both get ``False``.
        Use :meth:`claim` anywhere that acts on the answer; this is for
        display and for tests.
        """
        mid = str(message_id or "")
        if not mid:
            return False
        with self._lock:
            if self.path is not None:
                try:
                    self._reload()
                except OSError:
                    logger.exception("could not read the seen-messages ledger")
            else:
                self._ensure_loaded()
            return (str(chat_id), mid) in self._seen

    def record(self, chat_id: Any, message_id: Any) -> None:
        mid = str(message_id or "")
        if not mid:
            return
        key = (str(chat_id), mid)
        with self._lock:
            if self.path is None:
                self._ensure_loaded()
                self._remember(key)
                return
            try:
                with self._lease.acquire(
                    timeout=self.lease_timeout, reason=f"record {mid}"
                ):
                    self._reload()
                    self._remember(key)
                    self._save()
            except (LeaseTimeout, OSError):
                logger.exception("could not record a handled message")

    # -- internals ---------------------------------------------------------

    @property
    def _lease(self) -> ProcessLease:
        return ProcessLease(Path(str(self.path) + ".lock"), owner="owner-door")

    def _remember(self, key: Tuple[str, str]) -> None:
        if key in self._seen:
            return
        self._seen.add(key)
        self._order.append(key)
        # Bounded, oldest first. Telegram stops redelivering after 24 hours,
        # so forgetting the oldest entries costs nothing real, while keeping
        # them forever is a file that grows for as long as the machine runs.
        while len(self._order) > MAX_SEEN:
            self._seen.discard(self._order.pop(0))

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._reload()

    def _reload(self) -> None:
        """Read the ledger from disk, replacing what is held in memory."""
        self._loaded = True
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            self._seen, self._order = set(), []
            return
        except json.JSONDecodeError:
            # Not reachable through this class's own writes now that they are
            # atomic. Keeping the file rather than overwriting it leaves the
            # evidence; the cost is that redeliveries from before it broke are
            # no longer recognised.
            logger.error(
                "the seen-messages ledger at %s is unreadable; redelivered "
                "messages from before now may be handled again",
                self.path,
            )
            self._seen, self._order = set(), []
            return
        order: List[Tuple[str, str]] = []
        seen: Set[Tuple[str, str]] = set()
        if isinstance(data, list):
            for entry in data:
                # Every shape this file has ever had: a two-element pair, and
                # a three-element pair carrying when it was recorded. A single
                # malformed entry used to raise out of this method, through
                # the door's blanket handler, and answer every message from
                # then on with "something went wrong".
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                key = (str(entry[0]), str(entry[1]))
                if key in seen:
                    continue
                seen.add(key)
                order.append(key)
        self._seen, self._order = seen, order[-MAX_SEEN:]
        self._seen = set(self._order)

    def _save(self) -> bool:
        """Atomically replace the ledger. ``False`` when it did not land.

        Atomic because ``write_text`` truncates before it writes: interrupted
        between the two — a sleeping laptop, a launchd reload — it leaves a
        file that reads back as an empty ledger, and an empty ledger means
        every redelivered message is acted on a second time.
        """
        if self.path is None:
            return True
        now = time.time()
        payload = [[chat, mid, now] for chat, mid in self._order]
        return write_json_atomic(self.path, payload)


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
        self,
        *,
        chat_id: Any,
        text: str,
        sender: str = "",
        message_id: Any = "",
        send: Optional[Callable[[str], None]] = None,
    ) -> OwnerReply:
        """Handle one inbound message. Never raises.

        *send*, when given, is a way to deliver text to the owner *before*
        this call returns — used so the "I'll work on it" acknowledgement
        can go out immediately, ahead of the auto-build handoff, which can
        block for as long as ``pipeline.run()`` takes (minutes, across
        retries). Callers with no live transport (the CLI's ``jarvis wiz
        say``, most tests) omit it and get the old single-reply-at-the-end
        behaviour unchanged.
        """
        try:
            return self._receive(
                chat_id=chat_id,
                text=text,
                sender=sender,
                message_id=message_id,
                send=send,
            )
        except Exception:  # noqa: BLE001 - an inbound message must not be able
            logger.exception("owner message handling failed")  # ...to stop Wiz.
            return OwnerReply(
                text=self._say("something went wrong handling that."),
                route="refused",
            )

    # -- routing -----------------------------------------------------------

    def _receive(
        self,
        *,
        chat_id: Any,
        text: str,
        sender: str,
        message_id: Any = "",
        send: Optional[Callable[[str], None]] = None,
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

        try:
            mine = self.seen.claim(chat_id, message_id)
        except SeenLedgerUnavailable:
            # Neither "new" nor "already handled" is safe to assume. Acting
            # would risk a second feature request, a second branch and a
            # second pull request from one sentence; assuming it was handled
            # would drop what the owner said in silence, which to them is
            # indistinguishable from being ignored. So: say so, and let them
            # send it again once the ledger is readable.
            logger.exception(
                "could not establish whether a message was already handled "
                "(chat=%s, message_id=%s)",
                (str(chat_id)[:8] + "…") if chat_id else "<empty>",
                message_id,
            )
            return OwnerReply(
                text=self._say(
                    "I could not record that message, so I have not acted on it "
                    "— send it again in a moment."
                ),
                route="refused",
                authorized=True,
            )
        if not mine:
            # A redelivery of something already acted on. Silent — the owner
            # already got their answer the first time, and answering twice
            # is indistinguishable from Wiz not remembering what it did.
            logger.info(
                "ignored a redelivered message (chat=%s, message_id=%s)",
                (str(chat_id)[:8] + "…") if chat_id else "<empty>",
                message_id,
            )
            return OwnerReply(route="duplicate", authorized=True)

        if self._is_live_reliability_instruction(message):
            return self._reliability(chat_id=chat_id, text=message)

        return self._dispatch(chat_id=chat_id, text=message, sender=sender, send=send)

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

    def _dispatch(
        self,
        *,
        chat_id: Any,
        text: str,
        sender: str,
        send: Optional[Callable[[str], None]] = None,
    ) -> OwnerReply:
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
        auto_build_dispatched = False
        if (capability == "feature.request"
            and getattr(result, "accepted", False)
            and getattr(result, "feature_id", "")):
            auto_build_dispatched = True
            feature_id = str(result.feature_id)

            if send is not None:
                # _auto_build_feature() calls pipeline.run(), which can take
                # minutes across retries — found on FEAT-00031/FEAT-00032,
                # where the owner's phone showed nothing until the whole
                # build finished, and sometimes saw "I need your help"
                # (sent mid-run, straight to the notifier, for a real
                # pipeline outcome) *before* ever seeing this acknowledgement.
                # Sending it now, ahead of the blocking call, fixes the
                # ordering without touching that separate notifier path or
                # spawning any new thread — this call still runs on the
                # same poller thread that received the message.
                if reply:
                    send(reply)
                build_result = self._auto_build_feature(feature_id, sender, chat_id)
                if build_result.get("started", False):
                    # The pipeline's own eventual outcome (shipped, needs
                    # approval, attempts exhausted, ...) is a NEEDS_OWNER /
                    # SUCCESS journal kind and reaches the owner through
                    # owner_notifier once pipeline.run() actually knows one
                    # — sending a second, already-sent "Starting work now."
                    # here would just be a stale echo of the ack above.
                    capability = "feature.request+build"
                    reply = ""
                else:
                    # Nothing else reports a refusal/failure to even start —
                    # that only reaches the journal (feature.auto_build_*),
                    # not owner_notifier — so this is the owner's one chance
                    # to hear it.
                    reply = build_result.get("detail") or "could not start building"
            else:
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

        if not reply and getattr(result, "accepted", False) and not auto_build_dispatched:
            # A read verb answered with facts and no words. The intake only
            # forwards a handler's own sentence, and the read verbs deliberately
            # produce none — so the owner asking "how is Wize?" used to get
            # silence. Rendering happens here rather than in the intake because
            # it is owner-facing English, and the intake is a security boundary
            # that should not also be a copywriter.
            reply = render(
                capability, getattr(result, "structured", None), persona=self.persona
            )

        if not reply and not auto_build_dispatched:
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
        reply_chat_id = str(getattr(message, "conversation_id", ""))
        reply = self.door.receive(
            chat_id=chat_id,
            text=getattr(message, "content", ""),
            sender=str(getattr(message, "sender", "") or ""),
            message_id=str(getattr(message, "message_id", "") or ""),
            send=lambda text: self._reply(reply_chat_id, text),
        )
        if not reply.text:
            return
        self._reply(reply_chat_id, reply.text)

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
