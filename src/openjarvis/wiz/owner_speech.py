"""Turning what Wiz knows into what Sir says.

Every verb returns structured data, because structured data is what the Control
Center renders, what the journal records and what tests assert on. None of it is
a sentence, and the owner asked a question in English from a phone.

So this module is the one place a result becomes a sentence, and it is
deterministic on purpose — the same rule the notification copy follows, for the
same reason. A model asked to summarise "3 open incidents, one CRITICAL" will
produce something fluent, occasionally wrong, and never checked. A table of
renderers produces something plainer that is true every time, and "true every
time" is the whole basis on which an owner stops reading the dashboard.

Three rules for the copy itself:

**Two sentences, usually one.** The owner is reading this on a phone, standing
up. Detail belongs in Control Center; this says the thing and stops.

**No internal vocabulary.** Not "capability", not "HUMAN_REQUIRED", not
"fingerprint", not "FEAT-00042 transitioned to VERIFYING". A feature id appears
because the owner can quote it back; nothing else internal does.

**Nothing is claimed that was not checked.** A handler that reports it could not
read the incident store produces "I cannot see the site's health from here", not
a cheerful summary of zero incidents. An empty result and a failed read look
identical in a count and completely different to somebody deciding whether to
get out of bed.

When a handler already produced its own ``say`` — the product verbs do, because
their sentence depends on state only they hold — that sentence wins. This module
fills the gap for the read verbs, which return facts and no words.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = ["say", "render"]


def _sir(persona: bool, text: str) -> str:
    return f"Sir, {text}" if persona else text[:1].upper() + text[1:]


def _join(items: Sequence[str]) -> str:
    """``"a, b and c"`` — the way a person would say a list."""
    values = [str(i).strip() for i in items if str(i).strip()]
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + f" and {values[-1]}"


def _count(n: int, singular: str, plural: str = "") -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


# ---------------------------------------------------------------------------
# Per-verb renderers
# ---------------------------------------------------------------------------


def _reliability_status(result: Mapping[str, Any], persona: bool) -> str:
    if not result.get("available"):
        # The honest answer. A zero that came from a failed read is the most
        # dangerous number this module could produce.
        return _sir(persona, "I cannot see the site's health from here.")
    open_count = int(result.get("open", 0) or 0)
    if not open_count:
        return _sir(persona, "everything is working normally.")

    incidents = list(result.get("incidents") or [])
    subjects = _join([str(i.get("component") or "something") for i in incidents[:3]])
    worst = _worst_severity(incidents)
    lead = "something is wrong" if worst != "CRITICAL" else "something is badly wrong"
    if subjects:
        return _sir(persona, f"{lead} with {subjects}. I am working on it.")
    return _sir(
        persona, f"{lead}: {_count(open_count, 'open problem')}. I am working on it."
    )


def _worst_severity(incidents: Sequence[Mapping[str, Any]]) -> str:
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
    worst, rank = "", -1
    for incident in incidents:
        value = str(incident.get("severity") or "").upper()
        if order.get(value, -1) > rank:
            worst, rank = value, order.get(value, -1)
    return worst


def _reliability_incidents(result: Mapping[str, Any], persona: bool) -> str:
    if not result.get("available"):
        return _sir(persona, "I cannot see the incident record from here.")
    incidents = list(result.get("incidents") or [])
    if not incidents:
        return _sir(persona, "nothing has gone wrong recently.")
    lines = [
        _sir(persona, f"the last {_count(len(incidents[:5]), 'thing')} that broke:")
    ]
    for incident in incidents[:5]:
        component = str(incident.get("component") or "something")
        state = str(incident.get("state") or "")
        lines.append(f"• {component} — {_plain_state(state)}")
    return "\n".join(lines)


#: Internal lifecycle states, in the owner's words. Anything unmapped is
#: reported as being worked on, which is true of every state that is not
#: terminal and is a better answer than the enum name.
_PLAIN_STATE = {
    "RESOLVED": "fixed",
    "COMPLETE": "done",
    "HUMAN_REQUIRED": "waiting for you",
    "RECOVERY_REQUIRED": "waiting for you",
    "FAILED": "I could not fix it",
    "ROLLED_BACK": "undone",
    "CANCELLED": "stopped",
    "READY": "ready to go",
    "MERGING": "going live",
    "DEPLOYING": "going live",
    "PRODUCTION_VERIFYING": "live, being checked",
    "BUILDING": "being written",
    "TESTING": "being tested",
    "PREVIEWING": "being previewed",
    "VERIFYING": "being checked",
    "PLANNING": "being planned",
    "UNDERSTANDING": "being read up on",
    "APPROVED_FOR_BUILD": "about to start",
    "RECEIVED": "in the queue",
}


def _plain_state(state: str) -> str:
    return _PLAIN_STATE.get(str(state).upper(), "being worked on")


def _feature_list(result: Mapping[str, Any], persona: bool) -> str:
    if not result.get("available"):
        return _sir(persona, "I am not set up to build anything here yet.")
    building = list(result.get("building") or [])
    waiting = list(result.get("waiting_for_you") or [])
    ready = list(result.get("ready") or [])

    if not (building or waiting or ready):
        return _sir(persona, "I am not building anything at the moment.")

    lines: List[str] = []
    if building:
        lines.append(
            _sir(persona, f"I am working on {_count(len(building), 'thing')}:")
        )
        for item in building[:5]:
            lines.append(
                f"• {item.get('title') or item.get('id')} — "
                f"{_plain_state(str(item.get('state') or ''))}"
            )
    if ready:
        titles = _join([str(i.get("title") or i.get("id")) for i in ready[:3]])
        lines.append(f"Ready to go: {titles}.")
    if waiting:
        titles = _join([str(i.get("title") or i.get("id")) for i in waiting[:3]])
        lines.append(f"Waiting for you: {titles}.")
    return "\n".join(lines)


def _feature_status(result: Mapping[str, Any], persona: bool) -> str:
    if not result.get("available"):
        return _sir(persona, str(result.get("detail") or "I do not know that one."))
    feature = dict(result.get("feature") or {})
    title = str(feature.get("title") or feature.get("id") or "it")
    state = _plain_state(str(feature.get("state") or ""))
    identifier = str(feature.get("id") or "")
    sentence = f"{title} is {state}."
    reason = str(feature.get("last_reason") or "").strip()
    if state == "waiting for you" and reason:
        sentence += f" {reason[:160].rstrip('.')}."
    return _sir(persona, sentence) + (f"\n\n{identifier}" if identifier else "")


def _capabilities(result: Mapping[str, Any], persona: bool) -> str:
    configured = list(result.get("configured") or [])
    unavailable = list(result.get("unavailable") or [])
    if not configured:
        return _sir(persona, "nothing is configured here yet, so I can only listen.")
    lines = [_sir(persona, "here is what I can do:")]
    for spec in configured[:12]:
        lines.append(f"• {spec.get('summary') or spec.get('name')}")
    if unavailable:
        # Named rather than hidden. A capability the operator expected and does
        # not see is a question; a capability listed as unavailable is an
        # answer.
        lines.append(
            f"\n{_count(len(unavailable), 'other thing')} I would be able to do "
            "if it were set up here."
        )
    return "\n".join(lines)


def _authority(result: Mapping[str, Any], persona: bool) -> str:
    channel = str(result.get("asking_channel") or "this channel")
    granted = dict(result.get("granted") or {})
    mine = list(granted.get(channel) or [])
    if not mine:
        return _sir(
            persona,
            f"from {channel} I can answer questions, and nothing more. "
            "Anything that changes something has to come from Control Center.",
        )
    allowed = _join([_plain_authority(a) for a in mine])
    return _sir(persona, f"from {channel} I am allowed to: {allowed}.")


_PLAIN_AUTHORITY = {
    "READ": "answer questions",
    "SAFE_ACTION": "record things",
    "CODE_WRITE": "write code in a branch",
    "PR_WRITE": "open a pull request",
    "PRODUCTION_CHANGE": "change production",
    "SECRET_ACCESS": "read credentials",
}


def _plain_authority(value: Any) -> str:
    return _PLAIN_AUTHORITY.get(str(value).upper(), str(value).lower())


#: Wiz's own internal check names, in the words the owner uses for them.
#: Anything genuinely broken (FAILED) is worth a phone sentence; anything
#: merely unconfigured is not — an owner who never turned Sir Voice on does
#: not need to be told it is off every time they ask how Wiz is doing.
_PLAIN_TROUBLE = {
    "audit_trail": "my record of decisions has been tampered with",
    "watcher": "the watcher is not running",
    "coding_engine": "I have no coding tool here, so I cannot build anything",
    "task_engine": "the feature queue is broken",
    "notification_ledger": "my memory of what I've told you is broken",
    "scheduler": "my scheduled tasks have stopped running",
    "sir_voice": "I cannot hear or speak right now",
}


def _wiz_health(result: Mapping[str, Any], persona: bool) -> str:
    """Wiz's own health. Never a statement about the website.

    Reads the full report from :mod:`openjarvis.wiz.health` when it is
    present — every check, not a summary of three of them — and only ever
    reports a check that is actually FAILED. ``NOT_CONFIGURED`` and
    ``UNKNOWN`` are correct, informative states for a check the operator has
    simply never turned on, and saying "I am not well" about them would make
    the sentence worthless the first time it was wrong.
    """
    checks = list(result.get("checks") or [])
    if checks:
        broken = [
            _PLAIN_TROUBLE.get(c.get("name", ""), c.get("name", "something"))
            for c in checks
            if str(c.get("state", "")).upper() == "FAILED"
        ]
        if broken:
            return _sir(persona, f"I am not well: {_join(broken)}.")
        return _sir(persona, "I am working normally.")

    # No full report attached — the older, narrower shape.
    journal = dict(result.get("journal") or {})
    troubles: List[str] = []
    if journal.get("enabled") and journal.get("intact") is False:
        troubles.append("my record of decisions has been tampered with")
    engine = str(result.get("coding_engine") or "")
    if "not" in engine.lower() or "missing" in engine.lower():
        troubles.append("I have no coding tool here, so I cannot build anything")

    if troubles:
        return _sir(persona, f"I am not well: {_join(troubles)}.")
    return _sir(persona, "I am working normally.")


def _product_entries(result: Mapping[str, Any], persona: bool) -> str:
    if not result.get("available"):
        return _sir(persona, str(result.get("detail") or "I have no record of that."))
    said = str(result.get("say") or "").strip()
    if said:
        return said if said.lower().startswith("sir") else _sir(persona, said)
    entries = list(result.get("entries") or [])
    if not entries:
        return _sir(persona, "I have nothing recorded about that.")
    lines = [_sir(persona, f"{_count(len(entries[:5]), 'thing')} I have on record:")]
    for entry in entries[:5]:
        lines.append(f"• {entry.get('summary') or entry.get('title') or entry}")
    return "\n".join(lines)


#: Verb name to renderer. A verb absent from this table falls back to its own
#: ``say``, and then to an honest admission — never to a guess at what its
#: fields meant.
_RENDERERS: Dict[str, Callable[[Mapping[str, Any], bool], str]] = {
    "reliability.status": _reliability_status,
    "reliability.incidents": _reliability_incidents,
    "feature.list": _feature_list,
    "feature.status": _feature_status,
    "wiz.capabilities": _capabilities,
    "wiz.authority": _authority,
    "wiz.health": _wiz_health,
    "product.recent": _product_entries,
    "product.search": _product_entries,
}


def render(capability: str, result: Any, *, persona: bool = True) -> str:
    """One or two plain sentences for *result*, or ``""`` when there is nothing.

    A handler's own ``say`` wins: the product verbs build sentences that depend
    on state only they hold, and a second renderer guessing at the same thing
    would eventually disagree with them.
    """
    if not isinstance(result, Mapping):
        return ""
    said = str(result.get("say") or "").strip()
    if said:
        return said

    renderer = _RENDERERS.get(str(capability))
    if renderer is None:
        return ""
    try:
        return renderer(result, persona)
    except Exception:  # noqa: BLE001 - a rendering bug must not eat the answer
        logger.exception("could not render a reply for %s", capability)
        return ""


def say(outcome: Any, *, persona: bool = True) -> str:
    """What to send back for one dispatcher :class:`Outcome`.

    A refusal carries its own message and it is already written for a person —
    "I cannot do that here: the Claude CLI is not installed" is exactly what
    should reach the phone.
    """
    if outcome is None:
        return ""
    if not getattr(outcome, "handled", False):
        return str(getattr(outcome, "message", "") or "")
    rendered = render(
        str(getattr(outcome, "capability", "") or ""),
        getattr(outcome, "result", None),
        persona=persona,
    )
    if rendered:
        return rendered
    # Handled, and nothing to say about it. Better than inventing a summary of
    # fields this module has never seen.
    return _sir(persona, "done.")
