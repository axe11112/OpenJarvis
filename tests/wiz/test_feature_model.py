"""The feature state machine: what it permits, and what it must never permit."""

from __future__ import annotations

import pytest

from openjarvis.wiz.features.model import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    FeatureRequest,
    FeatureState,
    InvalidFeatureTransition,
    Priority,
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


class TestResumingFromHumanRequired:
    """resume_from_human_required is deliberately not a LEGAL_TRANSITIONS
    entry — see openjarvis.wiz.features.recovery for why. These tests pin the
    behaviour of the separate method instead."""

    def test_it_only_works_from_human_required(self):
        feature = _feature(FeatureState.BUILDING)
        with pytest.raises(InvalidFeatureTransition):
            feature.resume_from_human_required(
                FeatureState.BUILDING, at="t", reason="x"
            )

    def test_it_only_targets_building(self):
        feature = _feature(FeatureState.HUMAN_REQUIRED)
        with pytest.raises(InvalidFeatureTransition):
            feature.resume_from_human_required(FeatureState.READY, at="t", reason="x")

    def test_a_successful_resume_is_marked_in_history(self):
        feature = _feature(FeatureState.HUMAN_REQUIRED)
        feature.resume_from_human_required(
            FeatureState.BUILDING, at="t", reason="evidence says this is fine"
        )
        assert feature.state is FeatureState.BUILDING
        assert feature.history[-1]["resumed"] is True
        assert feature.history[-1]["from"] == "HUMAN_REQUIRED"
        assert feature.history[-1]["to"] == "BUILDING"

    def test_it_does_not_widen_check_transition(self):
        with pytest.raises(InvalidFeatureTransition):
            feature = _feature(FeatureState.HUMAN_REQUIRED)
            feature.transition(FeatureState.BUILDING, at="t")


class TestReopeningPlanningFromHumanRequired:
    """resume_planning_from_human_required is the narrower sibling of
    resume_from_human_required: for a feature that crashed before any code
    was written, not one recovering real build progress. Also deliberately
    not a LEGAL_TRANSITIONS entry, for the same reason.
    """

    def test_it_only_works_from_human_required(self):
        feature = _feature(FeatureState.UNDERSTANDING)
        with pytest.raises(InvalidFeatureTransition):
            feature.resume_planning_from_human_required(at="t", reason="x")

    def test_a_feature_with_an_attempt_already_used_is_refused(self):
        feature = _feature(FeatureState.HUMAN_REQUIRED)
        feature.next_attempt(at="t")
        with pytest.raises(InvalidFeatureTransition):
            feature.resume_planning_from_human_required(at="t", reason="x")

    def test_a_feature_with_a_pull_request_already_open_is_refused(self):
        feature = _feature(FeatureState.HUMAN_REQUIRED)
        feature.pr_number = 42
        with pytest.raises(InvalidFeatureTransition):
            feature.resume_planning_from_human_required(at="t", reason="x")

    def test_a_successful_reopen_lands_on_received_and_is_marked_in_history(self):
        feature = _feature(FeatureState.HUMAN_REQUIRED)
        feature.resume_planning_from_human_required(
            at="t", reason="the usage limit that stopped this has since reset"
        )
        assert feature.state is FeatureState.RECEIVED
        assert feature.history[-1]["resumed"] is True
        assert feature.history[-1]["from"] == "HUMAN_REQUIRED"
        assert feature.history[-1]["to"] == "RECEIVED"

    def test_it_does_not_widen_check_transition(self):
        feature = _feature(FeatureState.HUMAN_REQUIRED)
        with pytest.raises(InvalidFeatureTransition):
            feature.transition(FeatureState.RECEIVED, at="t")


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
        second = feature.next_attempt(
            at="t2", hypothesis="the container is fixed width"
        )
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
