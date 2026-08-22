"""Requests arriving from a phone or a microphone.

§25 and §26. Both channels reach exactly the same verbs as the Control Center
and the CLI — there is no Telegram pipeline and no voice pipeline — and what
these adapters add is the part that is specific to arriving from outside: who
the sender is, whether we believe them, and what a transcript is allowed to
become.

The last one is the important one, and it is worth being explicit about the
failure it prevents.

**A transcript is data, never a command.** A voice adapter that took the words
after "run" and executed them would be a remote shell with speech recognition in
front of it, reachable by anything audible near the microphone — a television,
a passer-by, a phone playing a video. So :class:`VoiceIntake` does exactly one
thing with a transcript: it puts the whole string into a
:class:`~openjarvis.wiz.brain.Request` as ``text`` and hands it to the
dispatcher, which classifies it against a table of registered verbs. The
transcript cannot name a file, a command, a path or an argument. The most a
misheard sentence can achieve is a feature request nobody wanted, recorded, in
a list, which a person then reads.

**A sender claiming to be the owner is not the owner.** Telegram identifies
accounts, not people, and a forwarded message looks exactly like an original.
:class:`TelegramIntake` believes the *chat id*, which the operator configured,
and nothing in the message body. A message that says "this is the owner" from
an unlisted chat is refused, and the refusal is silent to the sender: telling an
unknown sender which chat ids are allowed is telling them what to spoof.

Both channels are capped by :data:`~openjarvis.wiz.authority.CHANNEL_CEILING`
at ``CODE_WRITE`` regardless of configuration, so neither can ever cause a merge
or a production change. That is enforced in the authority module, not here; what
is enforced here is that the actor these adapters build carries the right
channel and an honest ``authenticated`` flag.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from openjarvis.wiz.authority import Actor, Channel
from openjarvis.wiz.brain import Request

logger = logging.getLogger(__name__)

__all__ = ["IntakeResult", "TelegramIntake", "VoiceIntake"]

#: Wording that would make an assistant do something other than what the
#: channel is for. Never used to *decide* anything — the dispatcher already
#: refuses anything that does not name a registered verb — but recorded on the
#: result so that an attempt shows up in the journal as an attempt rather than
#: as an unrecognised sentence.
_INJECTION_SHAPED = re.compile(
    r"\b(ignore (your|all|previous|prior)|disregard (the|your)|"
    r"you are now|developer mode|system prompt|new instructions|"
    r"forget (your|everything)|override|jailbreak|"
    r"run the following|execute|sudo|rm -rf|curl |wget |bash -c|"
    r"reveal|print (the|your) (token|secret|key)|api[ _-]?key)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class IntakeResult:
    """What happened to one inbound message."""

    accepted: bool

    #: What to say back. Empty means say nothing at all, which is the correct
    #: reply to an unrecognised sender.
    reply: str = ""

    capability: str = ""
    feature_id: str = ""

    #: Set when the text was shaped like an attempt to redirect the assistant.
    #: Recorded, not acted on.
    suspicious: bool = False

    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reply": self.reply,
            "capability": self.capability,
            "feature_id": self.feature_id,
            "suspicious": self.suspicious,
            "detail": self.detail,
        }


@dataclass
class TelegramIntake:
    """Feature requests sent as chat messages.

    Parameters
    ----------
    wiz:
        The dispatcher. Not a pipeline: everything goes through the same door.
    owner_chat_ids:
        Chat ids the operator configured. A message from anywhere else is
        ignored — not refused with an explanation, ignored, because telling an
        unknown sender which ids are allowed is telling them what to spoof.
    journal:
        Where attempts are recorded.
    """

    wiz: Any
    owner_chat_ids: Sequence[str] = field(default_factory=tuple)
    journal: Any = None
    clock: Any = None

    def owned_by_operator(self, chat_id: Any) -> bool:
        """Whether *chat_id* is one the operator listed.

        Compared as strings, because a chat id arrives as an integer from one
        library and a string from another, and a comparison that silently fails
        on type is a comparison that lets everybody in or nobody.
        """
        wanted = {str(c).strip() for c in self.owner_chat_ids if str(c).strip()}
        return bool(wanted) and str(chat_id).strip() in wanted

    def receive(self, *, chat_id: Any, text: str, sender: str = "") -> IntakeResult:
        """Handle one inbound message."""
        cleaned = (text or "").strip()
        suspicious = bool(_INJECTION_SHAPED.search(cleaned))

        if not self.owned_by_operator(chat_id):
            # Recorded, because repeated messages from an unknown chat are worth
            # seeing; silent to the sender, because a helpful refusal is a hint.
            self._record(
                "telegram.refused_sender",
                f"message from an unlisted chat ({str(chat_id)[:32]})",
                suspicious=suspicious,
            )
            return IntakeResult(
                accepted=False,
                reply="",
                suspicious=suspicious,
                detail="this chat is not the operator's",
            )

        if not cleaned:
            return IntakeResult(accepted=False, reply="", detail="empty message")

        if suspicious:
            # Not refused: the dispatcher will refuse it anyway if it names no
            # verb, and refusing here would mean two places that decide. But it
            # is written down before it is dispatched, so the journal shows what
            # was attempted rather than only that something was unrecognised.
            self._record(
                "telegram.suspicious_text",
                cleaned[:400],
                suspicious=True,
            )

        actor = Actor(
            actor_id=str(sender or chat_id),
            channel=Channel.TELEGRAM,
            # The chat id matched the operator's. That is what authenticates a
            # Telegram message, and it is the strongest claim this channel can
            # ever make — which is why its ceiling stops at CODE_WRITE.
            authenticated=True,
        )
        outcome = self.wiz.handle(Request(text=cleaned, actor=actor))
        return self._render(outcome, suspicious=suspicious)

    def _render(self, outcome: Any, *, suspicious: bool) -> IntakeResult:
        if not outcome.handled:
            return IntakeResult(
                accepted=False,
                reply=outcome.message,
                capability=outcome.capability,
                suspicious=suspicious,
            )
        result = outcome.result if isinstance(outcome.result, dict) else {}
        return IntakeResult(
            accepted=True,
            # §25: "Sir, I'll work on it." and then quiet until there is
            # something worth saying. The handler already produces that
            # sentence; this does not invent a second one.
            reply=str(result.get("say", "")),
            capability=outcome.capability,
            feature_id=str(result.get("id", "")),
            suspicious=suspicious,
        )

    def _record(self, kind: str, reason: str, *, suspicious: bool) -> None:
        if self.journal is None:
            return
        try:
            self.journal.record(
                at=self.clock() if self.clock else "",
                kind=kind,
                capability="",
                actor_id="unknown",
                channel=Channel.TELEGRAM.value,
                reason=reason,
                detail={"suspicious": suspicious},
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("could not journal a Telegram intake event")


@dataclass
class VoiceIntake:
    """Feature requests spoken aloud.

    The whole transcript goes in as text and the dispatcher decides what it
    names. Nothing in this class extracts a command, a path, an argument or a
    filename from speech, and there is no method that could — which is the
    property, rather than a rule somebody has to remember.
    """

    wiz: Any
    journal: Any = None
    clock: Any = None

    #: Speech recognition on a laptop microphone mishears. A transcript this
    #: short is far more likely to be noise than a request, and recording noise
    #: as a feature request fills the operator's list with things they never
    #: said.
    min_words: int = 4

    def receive(self, transcript: str, *, actor_id: str = "operator") -> IntakeResult:
        """Turn a transcript into a request, or decline to."""
        cleaned = (transcript or "").strip()
        words = [w for w in cleaned.split() if w]

        if len(words) < self.min_words:
            return IntakeResult(
                accepted=False,
                reply="Sorry Sir, I did not catch that.",
                detail=f"only {len(words)} word(s)",
            )

        suspicious = bool(_INJECTION_SHAPED.search(cleaned))
        if suspicious:
            self._record("voice.suspicious_text", cleaned[:400])

        actor = Actor(
            actor_id=actor_id,
            channel=Channel.VOICE,
            # The voice system authenticated whoever is speaking as far as it
            # can — which is not very far, and is exactly why VOICE's ceiling
            # stops below anything that reaches production.
            authenticated=True,
        )
        outcome = self.wiz.handle(Request(text=cleaned, actor=actor))

        if not outcome.handled:
            return IntakeResult(
                accepted=False,
                reply=outcome.message or "I did not recognise that, Sir.",
                capability=outcome.capability,
                suspicious=suspicious,
            )

        result = outcome.result if isinstance(outcome.result, dict) else {}
        return IntakeResult(
            accepted=True,
            reply=str(result.get("say", "")),
            capability=outcome.capability,
            feature_id=str(result.get("id", "")),
            suspicious=suspicious,
        )

    def _record(self, kind: str, reason: str) -> None:
        if self.journal is None:
            return
        try:
            self.journal.record(
                at=self.clock() if self.clock else "",
                kind=kind,
                capability="",
                actor_id="operator",
                channel=Channel.VOICE.value,
                reason=reason,
                detail={"suspicious": True},
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("could not journal a voice intake event")


def spoken_capabilities() -> List[str]:
    """Verbs it makes sense to reach by speaking.

    Not enforced here — the authority ceiling and the capability registry do the
    enforcing — but useful to the voice system when it decides whether a
    transcript is worth passing on at all.
    """
    return ["feature.request", "feature.list", "product.recent", "product.search"]
