"""The feature state machine: what it permits, and what it must never permit."""

from __future__ import annotations

import pytest

from openjarvis.wiz.features.model import (
    FeatureRequest,
    FeatureState,
    InvalidFeatureTransition,
    LEGAL_TRANSITIONS,
    Priority,
    TERMINAL_STATES,
)


def _feature(state=FeatureState.RECEIVED) -> FeatureRequest:
    return FeatureRequest(
        id="FEAT-00001",
        title="Add a coach dashboard",
        operator_request="Add a coach dashboard.",
        state=state,
        created_at="2026-08-17T10:00:00+00:00",
    )


class TestNoStepCanBeSkipped:
    def test_a_new_request_cannot_jump_to_complete(self):
        feature = _feature()
        with pytest.raises(InvalidFeatureTransition):
            feature.transition(FeatureState.COMPLETE, at="t")

    def test_a_request_cannot_be_built_before_it_is_approved(self):
        feature = _feature(FeatureState.PLANNING)
        with pytest.raises(InvalidFeatureTransition):
            feature.transition(FeatureState.BUILDING, at="t")

    def test_a_request_cannot_merge_without_verifying(self):
        feature = _feature(FeatureState.TESTING)
        with pytest.raises(InvalidFeatureTransition):
            feature.transition(FeatureState.MERGING, at="t")

    def test_nothing_reaches_complete_without_production_verification(self):
        reaching_complete = [
            state
            for state, allowed in LEGAL_TRANSITIONS.items()
            if FeatureState.COMPLETE in allowed
        ]
        assert reaching_complete == [FeatureState.PRODUCTION_VERIFYING]

    def test_the_happy_path_is_walkable(self):
        feature = _feature()
        for target in (
            FeatureState.UNDERSTANDING,
            FeatureState.PLANNING,
            FeatureState.APPROVED_FOR_BUILD,
            FeatureState.BUILDING,
            FeatureState.TESTING,
            FeatureState.PREVIEWING,
            FeatureState.VERIFYING,
            FeatureState.READY,
            FeatureState.MERGING,
            FeatureState.DEPLOYING,
            FeatureState.PRODUCTION_VERIFYING,
            FeatureState.COMPLETE,
        ):
            feature.transition(target, at="t")
        assert feature.state is FeatureState.COMPLETE
        assert feature.terminal


class TestStoppingIsAlwaysAllowed:
    @pytest.mark.parametrize(
        "state", [s for s in FeatureState if s not in TERMINAL_STATES]
    )
    def test_any_state_can_ask_for_a_human(self, state):
        feature = _feature(state)
        feature.transition(FeatureState.HUMAN_REQUIRED, at="t", reason="stuck")
        assert feature.state is FeatureState.HUMAN_REQUIRED

    @pytest.mark.parametrize(
        "state", [s for s in FeatureState if s not in TERMINAL_STATES]
    )
    def test_any_state_can_be_cancelled(self, state):
        feature = _feature(state)
        feature.transition(FeatureState.CANCELLED, at="t")
        assert feature.terminal

    def test_a_terminal_state_progresses_nowhere(self):
        for state in TERMINAL_STATES:
            assert LEGAL_TRANSITIONS.get(state, frozenset()) == frozenset()


class TestTheIterativeLoop:
    def test_a_failed_check_sends_the_work_back_to_building(self):
        feature = _feature(FeatureState.TESTING)
        feature.transition(FeatureState.BUILDING, at="t", reason="tests failed")
        assert feature.state is FeatureState.BUILDING

    def test_failed_verification_sends_the_work_back_to_building(self):
        feature = _feature(FeatureState.VERIFYING)
        feature.transition(FeatureState.BUILDING, at="t", reason="mobile broken")
        assert feature.state is FeatureState.BUILDING

    def test_attempts_are_numbered_and_remember_their_hypothesis(self):
        feature = _feature()
        first = feature.next_attempt(at="t1", hypothesis="the grid is not responsive")
        second = feature.next_attempt(at="t2", hypothesis="the container is fixed width")
        assert (first.number, second.number) == (1, 2)
        assert feature.attempts_used == 2
        assert second.hypothesis == "the container is fixed width"


class TestHistory:
    def test_every_transition_is_recorded(self):
        feature = _feature()
        feature.transition(FeatureState.UNDERSTANDING, at="t1", reason="picked up")
        feature.transition(FeatureState.PLANNING, at="t2")
        assert [h["to"] for h in feature.history] == ["UNDERSTANDING", "PLANNING"]
        assert feature.history[0]["reason"] == "picked up"
        assert feature.history[0]["from"] == "RECEIVED"

    def test_a_refused_transition_leaves_no_trace(self):
        feature = _feature()
        with pytest.raises(InvalidFeatureTransition):
            feature.transition(FeatureState.COMPLETE, at="t")
        assert feature.history == []
        assert feature.state is FeatureState.RECEIVED


class TestPriority:
    def test_production_priorities_sort_before_product_ones(self):
        assert Priority.P0.rank < Priority.P1.rank < Priority.P2.rank
        assert Priority.P2.rank < Priority.P3.rank < Priority.P4.rank


class TestSerialisation:
    def test_a_request_survives_a_round_trip(self):
        feature = _feature()
        feature.transition(FeatureState.UNDERSTANDING, at="t1")
        attempt = feature.next_attempt(at="t2", hypothesis="h")
        attempt.changed_files = ["src/app/page.tsx"]
        attempt.claim = "I added the dashboard"
        feature.acceptance = ["/coach/dashboard renders"]

        restored = FeatureRequest.from_dict(feature.to_dict())
        assert restored.state is FeatureState.UNDERSTANDING
        assert restored.attempts[0].changed_files == ["src/app/page.tsx"]
        assert restored.attempts[0].claim == "I added the dashboard"
        assert restored.acceptance == ["/coach/dashboard renders"]
        assert restored.history == feature.history
