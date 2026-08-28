"""summarize_features: real counts, honest about small samples."""

from __future__ import annotations

import pytest

from openjarvis.wiz.features.metrics import FeatureMetrics, summarize_features
from openjarvis.wiz.features.model import FeatureRequest, FeatureState
from openjarvis.wiz.features.store import FeatureStore


@pytest.fixture
def store(tmp_path):
    return FeatureStore(tmp_path / "features.db")


def feature(store, *, state, risk="LOW", attempts=0, history=None, **kw):
    request = FeatureRequest(
        title=kw.pop("title", "Add a download button"),
        operator_request="Add a download button",
        state=FeatureState.RECEIVED,
        risk=risk,
        created_at="2026-08-19T10:00:00+00:00",
        updated_at="2026-08-19T10:00:00+00:00",
        **kw,
    )
    store.create(request)
    for _ in range(attempts):
        request.next_attempt(at="2026-08-19T10:00:00+00:00")
    request.state = state
    if history is not None:
        request.history = list(history)
    store.save(request)
    return request


class TestEmptyStore:
    def test_no_store_configured_is_zero_everything(self):
        metrics = summarize_features(None)
        assert metrics.sample_size == 0
        assert metrics.render() == "No features have been recorded yet."

    def test_an_empty_store_is_zero_everything(self, store):
        metrics = summarize_features(store)
        assert metrics.sample_size == 0

    def test_a_broken_store_does_not_raise(self):
        class Broken:
            def list(self, **kwargs):
                raise RuntimeError("gone")

        metrics = summarize_features(Broken())
        assert metrics.sample_size == 0


class TestCounting:
    def test_every_feature_is_counted_once(self, store):
        feature(store, state=FeatureState.COMPLETE)
        feature(store, state=FeatureState.HUMAN_REQUIRED)
        feature(store, state=FeatureState.CANCELLED)
        feature(store, state=FeatureState.BUILDING)

        metrics = summarize_features(store)

        assert metrics.sample_size == 4
        assert metrics.completed == 1
        assert metrics.human_required == 1
        assert metrics.cancelled == 1
        assert metrics.in_progress == 1

    def test_low_risk_completion_is_autonomous(self, store):
        feature(store, state=FeatureState.COMPLETE, risk="LOW")
        metrics = summarize_features(store)
        assert metrics.low_autonomous_completions == 1
        assert metrics.operator_approved_completions == 0

    def test_medium_or_high_completion_required_an_operator(self, store):
        feature(store, state=FeatureState.COMPLETE, risk="MEDIUM")
        feature(store, state=FeatureState.COMPLETE, risk="HIGH")
        metrics = summarize_features(store)
        assert metrics.low_autonomous_completions == 0
        assert metrics.operator_approved_completions == 2

    def test_a_feature_stuck_mid_pipeline_is_neither_done_nor_stopped(self, store):
        feature(store, state=FeatureState.PREVIEWING)
        metrics = summarize_features(store)
        assert metrics.in_progress == 1
        assert metrics.completed == 0
        assert metrics.human_required == 0
        assert metrics.cancelled == 0


class TestAttempts:
    def test_attempts_are_summed_and_averaged(self, store):
        feature(store, state=FeatureState.COMPLETE, attempts=1)
        feature(store, state=FeatureState.COMPLETE, attempts=3)
        metrics = summarize_features(store)
        assert metrics.total_claude_attempts == 4
        assert metrics.average_attempts == 2.0


class TestRiskEscalation:
    def test_a_diff_triggered_escalation_is_counted(self, store):
        feature(
            store,
            state=FeatureState.HUMAN_REQUIRED,
            risk="HIGH",
            history=[
                {
                    "at": "t",
                    "from": "BUILDING",
                    "to": "HUMAN_REQUIRED",
                    "reason": (
                        "Sir, this turned out to change something sensitive — "
                        "auth/session.ts — so I have stopped and left it for "
                        "you to look at."
                    ),
                }
            ],
        )
        metrics = summarize_features(store)
        assert metrics.risk_escalations == 1

    def test_a_feature_that_was_always_high_risk_is_not_counted_as_escalated(
        self, store
    ):
        feature(store, state=FeatureState.HUMAN_REQUIRED, risk="HIGH")
        metrics = summarize_features(store)
        assert metrics.risk_escalations == 0


class TestRender:
    def test_render_shows_the_count_not_just_a_rate(self, store):
        feature(store, state=FeatureState.COMPLETE, risk="LOW")
        feature(store, state=FeatureState.HUMAN_REQUIRED)
        text = summarize_features(store).render()
        assert "2 feature(s) recorded" in text
        assert "1 completed" in text

    def test_to_dict_round_trips_every_field(self, store):
        feature(store, state=FeatureState.COMPLETE)
        d = summarize_features(store).to_dict()
        assert d.keys() == FeatureMetrics().to_dict().keys()
