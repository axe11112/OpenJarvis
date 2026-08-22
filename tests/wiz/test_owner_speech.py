"""Facts into sentences, deterministically.

The rule these tests defend is that an empty result and a failed read must never
produce the same sentence. A count of zero incidents means "everything is
working"; a store that could not be opened means "I cannot see", and the whole
value of the assistant rests on not confusing the two.
"""

from __future__ import annotations

import pytest

from openjarvis.wiz.owner_speech import render, say


class Outcome:
    def __init__(self, capability, result, handled=True, message=""):
        self.capability = capability
        self.result = result
        self.handled = handled
        self.message = message


# ---------------------------------------------------------------------------
# The distinction that matters most
# ---------------------------------------------------------------------------


def test_a_healthy_site_and_an_unreadable_store_do_not_sound_alike():
    healthy = render("reliability.status", {"available": True, "open": 0})
    blind = render("reliability.status", {"available": False, "detail": "no store"})
    assert healthy != blind
    assert "working normally" in healthy
    assert "cannot see" in blind


def test_an_open_incident_names_what_is_wrong():
    text = render(
        "reliability.status",
        {
            "available": True,
            "open": 2,
            "incidents": [
                {"component": "login", "severity": "CRITICAL"},
                {"component": "signup", "severity": "HIGH"},
            ],
        },
    )
    assert "login" in text and "signup" in text
    assert "badly wrong" in text


def test_no_incidents_on_record_is_not_an_error():
    text = render("reliability.incidents", {"available": True, "incidents": []})
    assert "nothing has gone wrong" in text


def test_an_unreadable_incident_record_says_so():
    text = render("reliability.incidents", {"available": False})
    assert "cannot see" in text


# ---------------------------------------------------------------------------
# Wiz's own health is never a claim about the website
# ---------------------------------------------------------------------------


def test_wiz_health_says_nothing_about_the_site():
    text = render(
        "wiz.health",
        {
            "capabilities_declared": 5,
            "capabilities_implemented": 5,
            "journal": {"enabled": True, "intact": True},
            "coding_engine": "claude 1.2.3",
        },
    )
    assert "working normally" in text
    for word in ("site", "website", "production", "incident"):
        assert word not in text.lower()


def test_a_tampered_journal_is_reported_as_wiz_being_unwell():
    text = render(
        "wiz.health",
        {
            "capabilities_declared": 5,
            "capabilities_implemented": 5,
            "journal": {"enabled": True, "intact": False},
            "coding_engine": "claude 1.2.3",
        },
    )
    assert "not well" in text
    assert "tampered" in text


def test_the_full_report_only_speaks_about_failed_checks():
    """Section 8: many checks are NOT_CONFIGURED on an ordinary machine.

    Sir Voice being off, no scheduler, no watcher on this platform — none of
    that is Wiz being unwell, and reporting it that way would make "I am not
    well" mean nothing the first time an operator who never enabled voice
    heard it.
    """
    text = render(
        "wiz.health",
        {
            "checks": [
                {"name": "audit_trail", "state": "HEALTHY"},
                {"name": "sir_voice", "state": "NOT_CONFIGURED"},
                {"name": "scheduler", "state": "NOT_CONFIGURED"},
                {"name": "watcher", "state": "NOT_CONFIGURED"},
            ]
        },
    )
    assert "working normally" in text


def test_the_full_report_names_a_real_failure():
    text = render(
        "wiz.health",
        {
            "checks": [
                {"name": "watcher", "state": "FAILED"},
                {"name": "sir_voice", "state": "NOT_CONFIGURED"},
            ]
        },
    )
    assert "not well" in text
    assert "watcher is not running" in text


def test_a_missing_coding_tool_is_named():
    text = render(
        "wiz.health",
        {
            "capabilities_declared": 5,
            "capabilities_implemented": 5,
            "journal": {"enabled": True, "intact": True},
            "coding_engine": "the 'claude' CLI is not installed",
        },
    )
    assert "cannot build" in text


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


def test_nothing_being_built_is_said_plainly():
    text = render(
        "feature.list",
        {"available": True, "building": [], "waiting_for_you": [], "ready": []},
    )
    assert "not building anything" in text


def test_the_feature_list_uses_the_owners_words_not_the_state_machine():
    text = render(
        "feature.list",
        {
            "available": True,
            "building": [
                {"id": "FEAT-1", "title": "download button", "state": "BUILDING"}
            ],
            "waiting_for_you": [
                {"id": "FEAT-2", "title": "dark mode", "state": "HUMAN_REQUIRED"}
            ],
            "ready": [],
        },
    )
    assert "being written" in text
    assert "Waiting for you" in text
    assert "BUILDING" not in text
    assert "HUMAN_REQUIRED" not in text


def test_a_feature_status_explains_why_it_is_waiting():
    text = render(
        "feature.status",
        {
            "available": True,
            "feature": {
                "id": "FEAT-00042",
                "title": "dark mode",
                "state": "HUMAN_REQUIRED",
                "last_reason": "the change touched a protected path",
            },
        },
    )
    assert "waiting for you" in text
    assert "protected path" in text
    assert "FEAT-00042" in text


# ---------------------------------------------------------------------------
# Discipline
# ---------------------------------------------------------------------------


def test_a_handlers_own_sentence_wins():
    """Two renderers for one fact eventually disagree."""
    assert render("feature.list", {"say": "Sir, I'll work on it."}) == (
        "Sir, I'll work on it."
    )


def test_an_unknown_verb_invents_nothing():
    assert render("something.new", {"a": 1, "b": 2}) == ""


def test_a_handled_verb_with_nothing_to_say_still_answers():
    assert say(Outcome("something.new", {"a": 1})) == "Sir, done."


def test_a_refusal_carries_its_own_message():
    outcome = Outcome("feature.build", None, handled=False, message="I cannot do that.")
    assert say(outcome) == "I cannot do that."


def test_a_renderer_that_raises_does_not_eat_the_answer():
    # A result the renderer cannot read must not produce a traceback where a
    # sentence was expected.
    assert render("reliability.status", {"available": True, "open": "many"}) == ""


def test_persona_can_be_switched_off():
    assert not render(
        "reliability.status", {"available": True, "open": 0}, persona=False
    ).startswith("Sir,")


@pytest.mark.parametrize(
    "capability,result",
    [
        ("reliability.status", {"available": True, "open": 0}),
        ("wiz.health", {"capabilities_implemented": 1, "journal": {}}),
        (
            "feature.list",
            {"available": True, "building": [], "waiting_for_you": [], "ready": []},
        ),
        ("wiz.authority", {"asking_channel": "telegram", "granted": {}}),
    ],
)
def test_no_internal_vocabulary_survives_rendering(capability, result):
    text = render(capability, result).lower()
    for jargon in ("capability", "authority.", "availability", "fingerprint", "none"):
        assert jargon not in text, (capability, jargon)
