"""The product-development half of Wiz, as verbs the brain can dispatch.

§5 and §33: every channel — the Control Center, the CLI, Telegram, Sir Voice —
arrives at the *same* verbs, through the same classifier, the same capability
registry and the same authority check. There is no Telegram feature pipeline and
no voice feature pipeline; there is one, and the channel is a field on the
request that caps what it may cause.

The split between the two authorities here is the important design decision.

``feature.request`` is ``SAFE_ACTION``. Recording that somebody would like a
download button changes nothing: it writes a row. Voice and Telegram can reach
it, and should — a request that cannot be made from the phone is a request that
gets forgotten by the time anybody is at a keyboard.

``feature.build`` is ``CODE_WRITE``. That is where the worktree is opened and
Claude is invoked, and it is a separate verb with a separate authority precisely
so that "I would like X" and "go and build X" are not the same sentence. A
channel may be allowed to say the first and not the second.

Neither of them decides the *feature's* risk. The capability's declared risk is
about the verb — asking Wiz to build something is ordinary — while the specific
change's risk is decided by the pipeline from the request text and the real
diff, and a HIGH one stops there for an approval no matter which verb reached
it. Two gates, at different granularities, and a feature has to pass both.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from openjarvis.wiz.authority import Actor, Authority, Channel
from openjarvis.wiz.brain import Request
from openjarvis.wiz.capabilities import Availability, CapabilitySpec, Risk
from openjarvis.wiz.features.model import FeatureState, Priority
from openjarvis.wiz.intents import IntentRule, intent_rule
from openjarvis.wiz.memory import ProductMemory, summarise

logger = logging.getLogger(__name__)

__all__ = [
    "ProductVerbs",
    "product_capabilities",
    "product_intent_rules",
]


def product_intent_rules() -> List[IntentRule]:
    """How an operator's sentence names a product verb.

    ``build`` and its synonyms are weighted above the read verbs, because "build
    me a coach dashboard" contains "dashboard" and must not be classified as a
    question about dashboards.
    """
    return [
        intent_rule(
            "feature.request",
            # The optional address is not decoration. §25's own example is
            # "Sir, add export to reports", and an anchor that demands the verb
            # first classifies the brief's example as unrecognised — which is
            # how a channel ends up feeling broken while every test passes.
            r"^\s*(sir|hey wiz|ok wiz|wiz)?[,:\s]*(please\s+)?"
            r"(build|make|add|create|implement|write)\b|"
            r"\b(can you (build|make|add|create)|i want|i'd like|"
            r"i would like|we need)\b",
            weight=12,
        ),
        intent_rule(
            "feature.list",
            r"\b(what are you (building|working on)|what'?s in progress|"
            r"feature (list|queue)|list (the )?features|what is being built)\b",
            weight=14,
        ),
        intent_rule(
            "product.recent",
            r"\b(what did (we|you) (build|ship|do)|what happened (yesterday|today)|"
            r"what shipped)\b",
            weight=14,
        ),
        intent_rule(
            "product.search",
            r"^\s*(search|find|look up)\b|\bwhy did (we|you)\b",
            weight=13,
        ),
    ]


def product_capabilities(
    *,
    pipeline_available: Callable[[], Availability],
    memory_available: Callable[[], Availability],
) -> List[CapabilitySpec]:
    """The product verbs Wiz declares, with the authority each truly needs."""
    return [
        CapabilitySpec(
            name="feature.request",
            summary="record something you would like built",
            # Writing a row. The worktree and the coding session are a separate
            # verb with a separate authority; see the module docstring.
            authority=Authority.SAFE_ACTION,
            risk=Risk.LOW,
            probe=memory_available,
        ),
        CapabilitySpec(
            name="feature.build",
            summary="actually build a recorded request",
            authority=Authority.CODE_WRITE,
            # The verb is ordinary. The *change's* risk is decided per feature
            # by the pipeline, from the request and then from the real diff.
            risk=Risk.MEDIUM,
            probe=pipeline_available,
        ),
        CapabilitySpec(
            name="feature.list",
            summary="show what I am building and what is waiting",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=memory_available,
        ),
        CapabilitySpec(
            name="feature.status",
            summary="show everything about one request",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=memory_available,
        ),
        CapabilitySpec(
            name="product.recent",
            summary="say what was built recently, and why",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=memory_available,
        ),
        CapabilitySpec(
            name="product.search",
            summary="search what we have built, decided and learned",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=memory_available,
        ),
    ]


@dataclass
class ProductVerbs:
    """The handlers behind the product capabilities.

    Holds the pipeline and the memory rather than building them, so a test
    proving "Telegram cannot make Wiz merge" proves it about the same objects
    the operator runs.
    """

    pipeline: Any = None
    memory: Optional[ProductMemory] = None

    #: Runs a feature after it is recorded. Injected so the Control Center can
    #: run it in the background while the CLI runs it in the foreground, without
    #: either of them being a second pipeline.
    runner: Optional[Callable[[str], Any]] = None

    def handlers(self) -> Dict[str, Callable[[Request], Any]]:
        return {
            "feature.request": self.request_feature,
            "feature.build": self.build_feature,
            "feature.list": self.list_features,
            "feature.status": self.feature_status,
            "product.recent": self.recent,
            "product.search": self.search,
        }

    # -- intake ------------------------------------------------------------

    def request_feature(self, request: Request) -> Dict[str, Any]:
        """Record a request, whatever channel it arrived on.

        Records and stops. Building is :meth:`build_feature`, and keeping them
        apart is what lets an operator's phone say "add a download button"
        without that sentence being able to start a coding session by itself.
        """
        if self.pipeline is None:
            return {"recorded": False, "detail": "feature work is not configured here"}

        text = str(request.arguments.get("text") or request.text or "").strip()
        text = _strip_lead_in(text)
        if not text:
            return {
                "recorded": False,
                "detail": "I did not catch what you would like me to build",
            }

        priority = Priority.parse(request.arguments.get("priority", "P3"))
        feature = self.pipeline.submit(
            text,
            actor=request.actor,
            title=str(request.arguments.get("title", "")),
            priority=priority,
        )
        if self.memory is not None:
            self.memory.remember_feature(feature)

        return {
            "recorded": True,
            "id": feature.id,
            "title": feature.title,
            "risk": feature.risk,
            "state": feature.state.value,
            # The sentence the operator actually hears. §34: short, simple,
            # natural, and never an internal term.
            "say": f"Sir, I'll work on it. I'm calling it {feature.id}.",
        }

    def build_feature(self, request: Request) -> Dict[str, Any]:
        """Start work on a recorded request."""
        if self.pipeline is None or self.runner is None:
            return {"started": False, "detail": "feature work is not configured here"}

        feature_id = str(request.arguments.get("feature_id", "")).strip()
        if not feature_id:
            return {"started": False, "detail": "which request should I build?"}

        result = self.runner(feature_id)
        feature = getattr(result, "feature", result)
        if self.memory is not None and feature is not None:
            self.memory.remember_feature(feature)
        return {
            "started": True,
            "id": getattr(feature, "id", feature_id),
            "state": getattr(getattr(feature, "state", None), "value", ""),
            "say": getattr(result, "message", "") or "",
        }

    # -- reading -----------------------------------------------------------

    def list_features(self, request: Request) -> Dict[str, Any]:
        if self.pipeline is None:
            return {"available": False, "detail": "feature work is not configured here"}
        active = self.pipeline.store.active(limit=20)
        return {
            "available": True,
            "building": [_brief(f) for f in active if not f.terminal],
            "waiting_for_you": [
                _brief(f)
                for f in self.pipeline.store.list(
                    states=[FeatureState.HUMAN_REQUIRED], limit=10
                )
            ],
            "ready": [
                _brief(f)
                for f in self.pipeline.store.list(states=[FeatureState.READY], limit=10)
            ],
        }

    def feature_status(self, request: Request) -> Dict[str, Any]:
        if self.pipeline is None:
            return {"available": False, "detail": "feature work is not configured here"}
        feature_id = str(request.arguments.get("feature_id", "")).strip()
        feature = self.pipeline.store.get(feature_id) if feature_id else None
        if feature is None:
            return {"available": False, "detail": f"I have no request {feature_id!r}"}
        return {"available": True, "feature": feature.to_dict()}

    def recent(self, request: Request) -> Dict[str, Any]:
        if self.memory is None:
            return {"available": False, "detail": "I am not keeping a record yet"}
        day = str(request.arguments.get("day", "")).strip()
        entries = (
            self.memory.on_day(day)
            if day
            else self.memory.recent(limit=int(request.arguments.get("limit", 10)))
        )
        return {
            "available": True,
            "entries": [e.to_dict() for e in entries],
            "say": summarise(entries),
        }

    def search(self, request: Request) -> Dict[str, Any]:
        if self.memory is None:
            return {"available": False, "detail": "I am not keeping a record yet"}
        query = str(request.arguments.get("query") or request.text or "")
        query = _strip_lead_in(query)
        entries = self.memory.search(
            query, limit=int(request.arguments.get("limit", 10))
        )
        return {
            "available": True,
            "query": query,
            "entries": [e.to_dict() for e in entries],
            "say": summarise(entries),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Openers that carry no information about what to build. Stripped so that the
#: recorded request reads like a requirement rather than like a chat message —
#: it becomes the goal Claude is given, and "Sir, could you please add a
#: download button" is a worse brief than "add a download button".
_LEAD_INS = (
    "sir,",
    "hey wiz,",
    "wiz,",
    "please",
    "could you",
    "can you",
    "i want you to",
    "i'd like you to",
    "i would like you to",
    "search for",
    "look up",
    "find",
)


def _strip_lead_in(text: str) -> str:
    cleaned = (text or "").strip()
    changed = True
    while changed:
        changed = False
        lowered = cleaned.lower()
        for opener in _LEAD_INS:
            if lowered.startswith(opener):
                cleaned = cleaned[len(opener) :].lstrip(" ,:")
                changed = True
                break
    return cleaned


def _brief(feature: Any) -> Dict[str, Any]:
    """One feature, as the Control Center lists it."""
    return {
        "id": feature.id,
        "title": feature.title,
        "state": feature.state.value,
        "risk": feature.risk,
        "source": feature.source,
        "attempts": feature.attempts_used,
        "preview_url": feature.preview_url,
        "pr_url": feature.pr_url,
        "updated_at": feature.updated_at,
    }


def operator_from(channel: Channel, actor_id: str = "operator") -> Actor:
    """An authenticated operator on *channel*.

    Only for call sites that have already authenticated. A channel that has not
    — an arbitrary inbound Telegram message — must build its own
    :class:`Actor` with ``authenticated=False``.
    """
    return Actor(actor_id=actor_id, channel=channel, authenticated=True)
