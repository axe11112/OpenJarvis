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
import re
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
            # "Improve the onboarding" and "investigate why sign-up is failing"
            # are requests for work in exactly the way "add a download button"
            # is. Restricting the opener to the constructive verbs meant the
            # operator's own examples — improve, fix, investigate — arrived as
            # unrecognised sentences, which reads as Wiz not understanding
            # English rather than as a missing pattern.
            r"(build|make|add|create|implement|write|improve|fix|change|update|"
            r"refactor|redesign|rewrite|enhance|clean up|speed up|"
            r"investigate|look into|diagnose|work out why|figure out why)\b|"
            r"\b(can you (build|make|add|create|improve|fix)|i want|i'd like|"
            r"i would like|we need|"
            # How an operator reports a symptom rather than naming a change.
            r"(users|people|customers|nobody|no one) (are|is|can'?t|cannot|"
            r"keep|have) )\b",
            weight=12,
        ),
        intent_rule(
            "feature.list",
            r"\b(what are you (building|working on)|what'?s in progress|"
            r"feature (list|queue)|list (the )?features|what is being built|"
            # "What is Claude working on?" is the same question with the
            # implementation named. An operator who knows a coding agent does
            # the work asks about the agent, and being unable to answer that
            # makes Wiz look like it does not know what it is doing.
            r"what('?s| is| are) claude (working on|doing|up to)|"
            r"what are you up to)\b",
            weight=14,
        ),
        intent_rule(
            "product.recent",
            r"\b(what did (we|you) (build|ship|do)|"
            r"what happened (yesterday|today|last night|overnight|this week)|"
            r"what shipped|show me what changed|what'?s changed)\b",
            weight=14,
        ),
        intent_rule(
            # "Why did conversions drop?" is not a question Wiz can answer from
            # analytics it does not have. It *is* a question it can answer from
            # what it has recorded — decisions, deployments, incidents — and
            # "here is everything I know about conversions, which may be
            # nothing" is a better answer than not understanding the sentence.
            "product.search",
            r"^\s*(search|find|look up)\b|\bwhy (did|do|does|is|are|has|have)\b",
            weight=13,
        ),
        intent_rule(
            # "Why hasn't this shipped?" and "how is FEAT-00042 going" are the
            # same question, and the second phrasing is the one an operator
            # uses once they know the id. Weighted above ``feature.list``
            # because a sentence naming a specific request is asking about that
            # request, not for the whole queue.
            "feature.status",
            r"\bFEAT-\d+\b|"
            r"\b(why (hasn'?t|has not|isn'?t|is (it|that) not) "
            r"(it |that |this )?(ship\w*|merg\w*|done|finished|out)|"
            r"what'?s? (happening|going on) with|"
            r"how is (it|that|this) going|where (is|are) (it|we) (up to|with))\b",
            weight=15,
        ),
        intent_rule(
            # Stopping is always available and always at least as safe as
            # carrying on, so the phrasing is generous on purpose — an operator
            # who wants work to stop should not have to find the right words.
            "feature.cancel",
            r"\b(stop|cancel|abandon|drop|forget) (that|the|this|it|work|task|"
            r"request|feature|building|FEAT-\d+)|"
            r"\b(stop|cancel) (working on|building)\b|"
            r"^\s*(stop|cancel) it\b",
            weight=20,
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
            name="feature.cancel",
            summary="stop work on a request",
            # SAFE_ACTION, not CODE_WRITE. Stopping is strictly less powerful
            # than what is already running, and putting it behind the authority
            # that starts work would mean the channel that could begin
            # something might not be able to end it.
            authority=Authority.SAFE_ACTION,
            risk=Risk.LOW,
            probe=memory_available,
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
            "feature.cancel": self.cancel_feature,
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

    def cancel_feature(self, request: Request) -> Dict[str, Any]:
        """Stop work on a request.

        Resolving *which* request is the interesting part. An operator saying
        "stop that" from a phone rarely quotes an id, and guessing wrong means
        cancelling work they wanted. So: an explicit id wins; otherwise exactly
        one thing must be running, and if two are, Wiz asks rather than picks.
        """
        if self.pipeline is None:
            return {"cancelled": False, "detail": "feature work is not configured here"}

        feature_id = str(request.arguments.get("feature_id", "")).strip()
        if not feature_id:
            named = _FEATURE_ID.search(str(request.text or ""))
            feature_id = named.group(0).upper() if named else ""

        if not feature_id:
            running = [
                f for f in self.pipeline.store.active(limit=20) if not f.terminal
            ]
            if not running:
                return {
                    "cancelled": False,
                    "say": "Sir, nothing is running at the moment.",
                }
            if len(running) > 1:
                names = ", ".join(f"{f.id} ({f.title})" for f in running[:4])
                return {
                    "cancelled": False,
                    "ambiguous": True,
                    "candidates": [f.id for f in running],
                    "say": f"Sir, which one — {names}?",
                }
            feature_id = running[0].id

        try:
            feature = self.pipeline.cancel(
                feature_id, reason=f"stopped by {request.actor.channel.value}"
            )
        except KeyError:
            return {"cancelled": False, "say": f"Sir, I have no request {feature_id}."}

        if self.memory is not None:
            self.memory.remember_feature(feature)
        return {
            "cancelled": True,
            "id": feature.id,
            "state": feature.state.value,
            "say": f"Sir, I've stopped {feature.id}. Nothing was undone.",
        }

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
#: A feature id quoted anywhere in a sentence.
_FEATURE_ID = re.compile(r"\bFEAT-\d+\b", re.IGNORECASE)

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
