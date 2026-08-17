"""Sir works the problem, and says something useful when it stops.

Written against the complaint "Sir gives up much too easily" and against the
message that made it look that way: "3 repair attempts did not produce a
verified fix", which is true and useless.
"""

from __future__ import annotations

import pytest

from openjarvis.reliability.playbook import (
    STAGES,
    STRATEGIES,
    AutonomyMetrics,
    CauseClass,
    Handover,
    IncidentHistory,
    build_handover,
    classify_cause,
    next_strategy,
)
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import (
    Incident,
    IncidentState,
    ProbeResult,
    RepairAttempt,
    Severity,
    VerificationResult,
)


@pytest.fixture
def store(tmp_path):
    s = IncidentStore(tmp_path / "incidents.db")
    yield s
    s.close()


def _incident(store, **overrides):
    defaults = dict(
        fingerprint="fp-login",
        severity=Severity.HIGH,
        component="authentication",
        title="Login redirects back to /login",
    )
    defaults.update(overrides)
    return store.create(Incident(**defaults))


def _result(**overrides):
    defaults = dict(probe_id="auth-login", success=False, steps_completed=4)
    defaults.update(overrides)
    return ProbeResult(**defaults)


# ---------------------------------------------------------------------------
# The shape of the work
# ---------------------------------------------------------------------------


def test_the_playbook_confirms_before_it_acts():
    """Acting on a fault that has gone away is how a blip becomes a phone call."""
    assert STAGES[0].key == "confirm"


def test_the_playbook_ends_with_alternatives_not_with_repair():
    assert STAGES[-1].key == "alternatives"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_a_slow_but_correct_page_is_latency_not_an_outage(store):
    """INC-00020 and INC-00021, exactly."""
    incident = _incident(store)
    result = _result(failure_kind="slow", http_status=200, steps_completed=4)
    assert classify_cause(incident, result) == CauseClass.LATENCY


def test_a_500_is_classified_as_code(store):
    assert classify_cause(_incident(store), _result(http_status=500)) == CauseClass.CODE


def test_a_gateway_error_is_transient(store):
    result = _result(http_status=503)
    assert classify_cause(_incident(store), result) == CauseClass.TRANSIENT


def test_a_403_is_configuration_not_a_coding_mistake(store):
    result = _result(http_status=403)
    assert classify_cause(_incident(store), result) == CauseClass.CONFIGURATION


def test_what_cannot_be_placed_is_unknown_not_guessed(store):
    incident = _incident(store, occurrences=9)
    result = _result(failure_kind="assertion")
    assert classify_cause(incident, result) == CauseClass.UNKNOWN


# ---------------------------------------------------------------------------
# Alternatives — the stage that was missing
# ---------------------------------------------------------------------------


def test_the_first_attempt_starts_from_what_changed():
    assert next_strategy([]).key == "recent_change"


def test_a_second_attempt_is_a_different_idea():
    first = next_strategy([])
    second = next_strategy([first.key])
    assert second is not None
    assert second.key != first.key


def test_a_strategy_that_already_failed_is_never_repeated():
    """The behaviour that made three attempts worth one."""
    tried = []
    for _ in range(len(STRATEGIES)):
        strategy = next_strategy(tried)
        assert strategy.key not in tried
        tried.append(strategy.key)
    assert next_strategy(tried) is None


def test_running_out_of_ideas_is_a_reason_a_person_can_read():
    tried = [s.key for s in STRATEGIES]
    assert next_strategy(tried) is None


def test_classification_reorders_where_to_look_first():
    """A 403 is not usually application logic, so do not start there."""
    strategy = next_strategy([], cause=CauseClass.CONFIGURATION)
    assert strategy.key == "dependency_or_config"


def test_no_strategy_instructs_a_particular_change():
    """Guidance is a direction to look; the verifier still decides."""
    for strategy in STRATEGIES:
        assert strategy.hypothesis
        assert strategy.guidance


# ---------------------------------------------------------------------------
# Handover
# ---------------------------------------------------------------------------


def test_a_vague_handover_is_incomplete():
    handover = Handover(what_failed="Login is broken")
    assert not handover.is_complete()
    assert "the cause" in handover.missing()


def test_a_handover_says_what_is_needed(store):
    incident = _incident(store)
    incident.attempts.append(
        RepairAttempt(
            number=1,
            strategy="recent_change",
            outcome="verification_failed",
            verification=VerificationResult(
                passed=False, actual="the login page still redirects to /login"
            ),
        )
    )
    handover = build_handover(
        incident,
        reason="3 repair attempts did not produce a verified fix",
        max_attempts=3,
        result=_result(http_status=500),
    )
    assert handover.is_complete()
    rendered = handover.render()
    assert "What failed" in rendered
    assert "What I tried" in rendered
    assert "What I need from you" in rendered
    assert "still redirects to /login" in rendered


def test_a_handover_names_the_hypotheses_that_were_eliminated(store):
    incident = _incident(store)
    incident.attempts.append(RepairAttempt(number=1, strategy="recent_change"))
    incident.attempts.append(RepairAttempt(number=2, strategy="data_shape"))
    handover = build_handover(incident, reason="stopped", max_attempts=3)
    rendered = handover.render()
    assert "a recent change broke it" in rendered
    assert "input is different" in rendered


def test_an_external_failure_does_not_ask_for_a_code_review(store):
    incident = _incident(store)
    handover = build_handover(
        incident, reason="rate limited", result=_result(http_status=429)
    )
    assert "third-party" in handover.what_is_needed
    assert handover.cause_class == CauseClass.EXTERNAL


def test_an_unknown_cause_is_stated_as_unknown_not_invented(store):
    incident = _incident(store, occurrences=4)
    handover = build_handover(
        incident, reason="stopped", result=_result(failure_kind="assertion")
    )
    assert "Not established" in handover.cause


def test_a_handover_with_no_attempts_says_production_is_untouched(store):
    incident = _incident(store)
    handover = build_handover(incident, reason="repair is disabled")
    assert "untouched" in handover.what_is_needed


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_history_finds_earlier_occurrences_of_the_same_failure(store):
    old = _incident(store)
    store.transition(old, IncidentState.RESOLVED, reason="recovered")
    new = _incident(store)
    assert [i.id for i in IncidentHistory(store).previous(new)] == [old.id]


def test_history_does_not_count_the_incident_itself(store):
    incident = _incident(store)
    assert IncidentHistory(store).previous(incident) == []


def test_history_remembers_which_ideas_were_already_tried(store):
    old = _incident(store)
    store.add_attempt(old, RepairAttempt(number=1, strategy="recent_change"))
    store.transition(old, IncidentState.RESOLVED, reason="recovered")
    new = _incident(store)
    assert "recent_change" in IncidentHistory(store).strategies_tried(new)


def test_a_failure_that_keeps_clearing_itself_is_counted(store):
    for _ in range(3):
        old = _incident(store)
        store.transition(old, IncidentState.RESOLVED, reason="recovered on its own")
    new = _incident(store)
    assert IncidentHistory(store).self_recovered(new) == 3
    assert "cleared without intervention" in IncidentHistory(store).summary(new)


def test_history_never_raises_when_the_store_is_broken():
    class _Broken:
        def list_by_fingerprint(self, *a, **k):
            raise RuntimeError("disk on fire")

    incident = Incident(fingerprint="fp", severity=Severity.HIGH, component="a",
                        title="b")
    assert IncidentHistory(_Broken()).previous(incident) == []


# ---------------------------------------------------------------------------
# Autonomy
# ---------------------------------------------------------------------------


def test_autonomy_counts_what_was_handled_without_a_person(store):
    handled = _incident(store)
    store.transition(handled, IncidentState.RESOLVED, reason="recovered")
    escalated = _incident(store, fingerprint="fp-other")
    store.transition(escalated, IncidentState.HUMAN_REQUIRED, reason="stuck")

    metrics = AutonomyMetrics(store).snapshot()
    assert metrics["closed"] == 2
    assert metrics["handled_without_a_human"] == 1
    assert metrics["escalated"] == 1
    assert metrics["autonomy_rate"] == 0.5


def test_autonomy_separates_repairs_from_things_that_fixed_themselves(store):
    repaired = _incident(store)
    store.add_attempt(repaired, RepairAttempt(number=1, outcome="verified"))
    store.transition(repaired, IncidentState.RESOLVED, reason="fixed")
    recovered = _incident(store, fingerprint="fp-other")
    store.transition(recovered, IncidentState.RESOLVED, reason="recovered")

    metrics = AutonomyMetrics(store).snapshot()
    assert metrics["repaired_by_jarvis"] == 1
    assert metrics["recovered_on_their_own"] == 1


def test_autonomy_reports_unavailable_rather_than_zero_when_it_cannot_read():
    class _Broken:
        def list(self, **k):
            raise RuntimeError("no database")

    assert AutonomyMetrics(_Broken()).snapshot() == {"available": False}


# ---------------------------------------------------------------------------
# The two incidents the owner actually complained about
#
# Both are replayed from what the live database recorded, not from an
# invention: INC-00020 was homepage-http taking 9.65s against a 5.00s budget,
# and INC-00021 was auth-gate-dashboard taking 41.30s against 30.00s. Both were
# filed CRITICAL, both reached HUMAN_REQUIRED without a single repair attempt —
# INC-00021 within 0.85 seconds of being detected — and both resolved
# themselves. The site was never down. It was slow, twice.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "component,probe,took,budget",
    [
        ("website", "homepage-http", 9.65, 5.00),
        ("authorization", "auth-gate-dashboard", 41.30, 30.00),
    ],
)
def test_a_slow_page_no_longer_wakes_anybody(store, component, probe, took, budget):
    from openjarvis.reliability.policy import SafetyPolicy
    from openjarvis.reliability.severity import classify

    result = ProbeResult(
        probe_id=probe,
        success=False,
        failure_kind="slow",
        http_status=200,
        steps_completed=4,
        error=f"took {took}s, over the {budget}s budget",
    )
    verdict = classify(
        component=component, result=result, declared=Severity.CRITICAL
    )
    assert verdict.severity is Severity.LOW
    assert verdict.rule == "responded_but_slow"

    incident = _incident(store, severity=verdict.severity, component=component)
    assert classify_cause(incident, result) == CauseClass.LATENCY

    gate = SafetyPolicy(
        repair_enabled=True, auto_repair_severities=["HIGH", "MEDIUM"]
    ).may_attempt_repair(incident)
    assert not gate
    # The whole point: refused, and nobody is woken for it.
    assert gate.needs_human is False


def test_a_page_that_serves_nothing_is_still_critical():
    """The control. Nothing above may soften a real outage."""
    from openjarvis.reliability.severity import classify

    down = ProbeResult(
        probe_id="homepage-http",
        success=False,
        failure_kind="http",
        http_status=500,
        steps_completed=0,
    )
    verdict = classify(component="website", result=down, declared=Severity.CRITICAL)
    assert verdict.severity is Severity.CRITICAL
