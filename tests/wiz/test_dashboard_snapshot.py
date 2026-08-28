"""build_engineering_snapshot: the Control Center's Wiz-side payload."""

from __future__ import annotations

from openjarvis.wiz.dashboard_snapshot import build_engineering_snapshot
from openjarvis.wiz.features.model import FeatureRequest, FeatureState
from openjarvis.wiz.features.store import FeatureStore


def feature(store, *, title, state, risk="LOW", history=None, **kw):
    request = FeatureRequest(
        title=title,
        operator_request=title,
        state=FeatureState.RECEIVED,
        risk=risk,
        created_at="2026-08-19T10:00:00+00:00",
        updated_at="2026-08-19T10:00:00+00:00",
        **kw,
    )
    store.create(request)
    request.state = state
    if history is not None:
        request.history = list(history)
    store.save(request)
    return request


class FakePipeline:
    def __init__(self, store):
        self.store = store


class FakeProduct:
    def __init__(self, store):
        self.pipeline = FakePipeline(store)


class FakeRuntime:
    def __init__(self, store, *, health=None):
        self.product = FakeProduct(store) if store is not None else None
        self._health = health or {"overall": "HEALTHY"}

    def describe_health(self, request):
        return self._health


class TestAbsence:
    def test_no_runtime_is_reported_unavailable(self):
        assert build_engineering_snapshot(None) == {
            "available": False,
            "detail": "no engineering target configured",
        }

    def test_no_pipeline_still_reports_health(self):
        runtime = FakeRuntime(None)
        snapshot = build_engineering_snapshot(runtime)
        assert snapshot["available"] is True
        assert snapshot["metrics"]["sample_size"] == 0
        assert snapshot["needs_you"] == []
        assert snapshot["engineering"] == []


class TestRealData:
    def test_needs_you_names_the_reason(self, tmp_path):
        store = FeatureStore(tmp_path / "features.db")
        feature(
            store,
            title="Change permissions",
            state=FeatureState.HUMAN_REQUIRED,
            risk="HIGH",
            history=[{"reason": "it is a high-risk change"}],
        )
        runtime = FakeRuntime(store)

        snapshot = build_engineering_snapshot(runtime)

        assert snapshot["needs_you"][0]["title"] == "Change permissions"
        assert snapshot["needs_you"][0]["reason"] == "it is a high-risk change"

    def test_in_progress_excludes_terminal_states(self, tmp_path):
        store = FeatureStore(tmp_path / "features.db")
        feature(store, title="Building now", state=FeatureState.BUILDING)
        feature(store, title="Already done", state=FeatureState.COMPLETE)
        runtime = FakeRuntime(store)

        snapshot = build_engineering_snapshot(runtime)

        titles = [f["title"] for f in snapshot["engineering"]]
        assert titles == ["Building now"]

    def test_metrics_reflect_the_real_store(self, tmp_path):
        store = FeatureStore(tmp_path / "features.db")
        feature(store, title="a", state=FeatureState.COMPLETE, risk="LOW")
        feature(store, title="b", state=FeatureState.CANCELLED)
        runtime = FakeRuntime(store)

        snapshot = build_engineering_snapshot(runtime)

        assert snapshot["metrics"]["sample_size"] == 2
        assert snapshot["metrics"]["completed"] == 1
        assert snapshot["metrics"]["cancelled"] == 1

    def test_no_metadata_field_is_ever_included(self, tmp_path):
        # Built from FeatureRequest's own public summary fields, not a raw
        # dump of metadata — which can carry a raw exception string from a
        # GitHub/Vercel client failure.
        store = FeatureStore(tmp_path / "features.db")
        feature(store, title="x", state=FeatureState.HUMAN_REQUIRED)
        runtime = FakeRuntime(store)

        snapshot = build_engineering_snapshot(runtime)

        assert "metadata" not in snapshot["needs_you"][0]

    def test_a_broken_health_call_still_returns_metrics(self, tmp_path):
        class BrokenHealth(FakeRuntime):
            def describe_health(self, request):
                raise RuntimeError("gone")

        store = FeatureStore(tmp_path / "features.db")
        feature(store, title="a", state=FeatureState.COMPLETE)
        runtime = BrokenHealth(store)

        snapshot = build_engineering_snapshot(runtime)

        assert snapshot["health"] == {"overall": "UNKNOWN"}
        assert snapshot["metrics"]["sample_size"] == 1
