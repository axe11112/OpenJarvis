"""An escalation has to ask for something, or it does not happen.

The gate these tests defend is one field: :attr:`OwnerAsk.action`. Empty means
no message — not a shorter message, not a vaguer one. Half of what is here
checks that the gate closes; the other half checks that it opens for the cases
where staying quiet would mean hiding an outage.
"""

from __future__ import annotations

import pytest

from openjarvis.reliability.outage import OutageRegistry
from openjarvis.reliability.owner_ask import (
    OwnerAsk,
    build_owner_ask,
    last_good_deployment,
    owner_subjects,
)
from openjarvis.reliability.types import Correlation, Incident, Severity


def make(component="website", *, severity=Severity.CRITICAL, kind="navigation", **meta):
    return Incident(
        fingerprint=f"fp_{component}",
        severity=severity,
        component=component,
        title=f"{component} is unreachable",
        id="INC-00041",
        probe_id="homepage",
        metadata={"failure_kind": kind, **meta},
    )


ACTIONABLE = (
    "automatic repair is disabled",
    "the change touched a protected path: src/auth/session.ts",
    "post-merge: production did not verify (fleet_failed)",
    "a secret was found in the proposed change",
    "the change exceeded the allowed scope",
    "the security check refused the change",
)

NOT_ACTIONABLE = (
    "the check is flapping: 6 transitions in 5 minutes",
    "the repair was interrupted by a restart",
    "latency budget overrun",
    "the observer machine looked degraded",
)


@pytest.mark.parametrize("reason", ACTIONABLE)
def test_a_reason_with_an_operator_action_produces_one(reason):
    ask = build_owner_ask(make(), reason=reason, attempts=1, max_attempts=3)
    assert ask.actionable
    assert ask.action.strip()


@pytest.mark.parametrize("reason", NOT_ACTIONABLE)
def test_a_reason_that_asks_nothing_of_the_owner_produces_no_ask(reason):
    ask = build_owner_ask(make(), reason=reason, attempts=1, max_attempts=3)
    assert not ask.actionable
    assert ask.parked_reason


def test_an_unrecognised_reason_parks_rather_than_improvising():
    ask = build_owner_ask(make(), reason="something went sideways")
    assert not ask.actionable
    assert "no owner action is defined" in ask.parked_reason


def test_a_quiet_component_that_ran_out_of_attempts_parks():
    ask = build_owner_ask(
        make("billing", severity=Severity.MEDIUM, kind="assertion"),
        reason="3 repair attempts did not produce a verified fix",
        attempts=3,
        max_attempts=3,
    )
    assert not ask.actionable


def test_a_live_outage_that_ran_out_of_attempts_asks_for_a_decision():
    ask = build_owner_ask(
        make(),
        reason="3 repair attempts did not produce a verified fix",
        attempts=3,
        max_attempts=3,
    )
    assert ask.actionable
    assert "roll" in ask.action.lower()


def test_a_known_good_deployment_makes_the_ask_concrete():
    incident = make(last_good_deployment="ab77e19")
    ask = build_owner_ask(incident, reason="attempts exhausted")
    assert "ab77e19" in ask.action


def test_a_contract_failure_that_served_correctly_is_not_a_rollback_question():
    """It answered; it answered wrong. That is a bug, and bugs wait for a PR."""
    ask = build_owner_ask(
        make("signup", severity=Severity.HIGH, kind="assertion"),
        reason="attempts exhausted",
    )
    assert not ask.actionable


def test_an_unknown_failure_kind_is_never_assumed_harmless():
    """The blind spot that would hide a genuine outage in silence."""
    incident = Incident(
        fingerprint="fp",
        severity=Severity.HIGH,
        component="website",
        title="Website failed",
        id="INC-1",
    )
    ask = build_owner_ask(incident, reason="attempts exhausted")
    assert ask.actionable


# -- the digest -------------------------------------------------------------


def test_the_digest_ignores_how_many_times_it_was_tried():
    first = build_owner_ask(make(), reason="automatic repair is disabled", attempts=1)
    second = build_owner_ask(make(), reason="automatic repair is disabled", attempts=9)
    assert first.digest() == second.digest()


def test_the_digest_ignores_another_probe_joining():
    """Section 5: another probe joining an outage is not a new thing to do."""
    registry = OutageRegistry()
    incident = make()
    small = registry.assign(incident)
    first = build_owner_ask(
        incident, reason="automatic repair is disabled", outage=small
    )

    second_incident = make("signup")
    second_incident.id = "INC-00042"
    grown = registry.assign(second_incident)
    second = build_owner_ask(
        incident, reason="automatic repair is disabled", outage=grown
    )

    assert len(grown.components) == 2
    assert first.digest() == second.digest()


def test_the_digest_changes_when_the_action_does():
    first = build_owner_ask(make(), reason="automatic repair is disabled")
    second = build_owner_ask(make(), reason="a secret was found in the change")
    assert first.digest() != second.digest()


# -- content ----------------------------------------------------------------


def test_the_ask_names_every_affected_subject():
    registry = OutageRegistry()
    incident = make()
    registry.assign(incident)
    for component in ("authentication", "signup"):
        other = make(component)
        other.id = f"INC-{component}"
        outage = registry.assign(other)
    ask = build_owner_ask(
        incident, reason="automatic repair is disabled", outage=outage
    )
    assert "login" in ask.what_failed.lower()
    assert "sign-up" in ask.what_failed.lower()


def test_a_not_established_cause_is_never_quoted_at_the_owner():
    incident = make()
    incident.metadata["handover"] = {
        "cause": "Not established. I could not classify it."
    }
    ask = build_owner_ask(incident, reason="automatic repair is disabled")
    assert "not established" not in ask.cause.lower()


def test_the_deployment_becomes_the_cause_when_nothing_better_is_known():
    registry = OutageRegistry()
    incident = make()
    incident.correlation = Correlation(deployment_id="9f31c04")
    outage = registry.assign(incident)
    ask = build_owner_ask(
        incident, reason="automatic repair is disabled", outage=outage
    )
    assert "9f31c04" in ask.cause


def test_the_ask_serializes_everything_control_center_needs():
    ask = build_owner_ask(
        make(), reason="automatic repair is disabled", attempts=2, max_attempts=3
    )
    payload = ask.to_dict()
    for key in (
        "what_failed",
        "cause",
        "evidence",
        "tried",
        "why_blocked",
        "action",
        "actionable",
        "parked_reason",
        "digest",
    ):
        assert key in payload


def test_owner_subjects_falls_back_to_the_incident_alone():
    assert owner_subjects(make(), None) == ["The website"]


def test_last_good_deployment_is_empty_when_nothing_recorded():
    assert last_good_deployment(make()) == ""


def test_an_empty_ask_is_never_actionable():
    assert OwnerAsk().actionable is False
    assert OwnerAsk(action="   ").actionable is False
