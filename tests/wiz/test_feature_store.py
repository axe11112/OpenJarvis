"""Feature persistence."""

from __future__ import annotations

import pytest

from openjarvis.wiz.features.model import FeatureRequest, FeatureState
from openjarvis.wiz.features.store import FeatureStore


@pytest.fixture
def store(tmp_path):
    store = FeatureStore(tmp_path / "features.db")
    yield store
    store.close()


def _feature(**kwargs) -> FeatureRequest:
    defaults = dict(
        title="Add a coach dashboard",
        operator_request="Add a coach dashboard.",
        source="control_center",
        target="wize",
        created_at="2026-08-17T10:00:00+00:00",
    )
    defaults.update(kwargs)
    return FeatureRequest(**defaults)


class TestIdentity:
    def test_ids_start_at_one_and_are_padded(self, store):
        assert store.next_id() == "FEAT-00001"

    def test_ids_increment(self, store):
        store.create(_feature())
        assert store.next_id() == "FEAT-00002"

    def test_a_deleted_id_is_never_reused(self, store):
        store.create(_feature())
        second = store.create(_feature())
        store.delete(second.id)
        # FEAT-00002 has existed. It may have named a branch or a pull request,
        # so the next request must not be able to mean it too.
        assert store.next_id() == "FEAT-00003"

    def test_create_assigns_an_id_when_absent(self, store):
        feature = store.create(_feature())
        assert feature.id == "FEAT-00001"

    def test_asking_twice_never_yields_the_same_id(self, store):
        # next_id reserves rather than peeks, so two callers racing cannot both
        # be told FEAT-00001.
        assert len({store.next_id() for _ in range(20)}) == 20


class TestRoundTrip:
    def test_a_stored_request_comes_back_whole(self, store):
        feature = _feature(acceptance=["/coach/dashboard renders"])
        feature.transition(FeatureState.UNDERSTANDING, at="t1", reason="picked up")
        attempt = feature.next_attempt(at="t2", hypothesis="the grid is fixed width")
        attempt.changed_files = ["src/app/coach/page.tsx"]
        store.create(feature)

        loaded = store.get(feature.id)
        assert loaded is not None
        assert loaded.state is FeatureState.UNDERSTANDING
        assert loaded.acceptance == ["/coach/dashboard renders"]
        assert loaded.attempts[0].hypothesis == "the grid is fixed width"
        assert loaded.history[0]["reason"] == "picked up"

    def test_a_missing_request_is_none_not_an_error(self, store):
        assert store.get("FEAT-99999") is None

    def test_saving_an_uncreated_request_is_refused(self, store):
        # Otherwise a typo in an id silently creates a second record and the
        # first one stops being updated.
        with pytest.raises(KeyError):
            store.save(_feature(id="FEAT-00042"))

    def test_saving_persists_a_transition(self, store):
        feature = store.create(_feature())
        feature.transition(FeatureState.UNDERSTANDING, at="t1")
        store.save(feature)
        assert store.get(feature.id).state is FeatureState.UNDERSTANDING


class TestQuerying:
    def test_listing_filters_by_state(self, store):
        first = store.create(_feature(created_at="2026-08-17T10:00:00+00:00"))
        second = store.create(_feature(created_at="2026-08-17T11:00:00+00:00"))
        second.transition(FeatureState.UNDERSTANDING, at="t")
        store.save(second)

        received = store.list(states=[FeatureState.RECEIVED])
        assert [f.id for f in received] == [first.id]

    def test_active_excludes_finished_work(self, store):
        done = store.create(_feature())
        for target in (
            FeatureState.UNDERSTANDING,
            FeatureState.PLANNING,
            FeatureState.HUMAN_REQUIRED,
        ):
            done.transition(target, at="t")
        store.save(done)
        open_one = store.create(_feature(created_at="2026-08-17T12:00:00+00:00"))

        assert [f.id for f in store.active()] == [open_one.id]

    def test_listing_is_newest_first(self, store):
        store.create(_feature(created_at="2026-08-17T10:00:00+00:00"))
        newer = store.create(_feature(created_at="2026-08-17T12:00:00+00:00"))
        assert store.list()[0].id == newer.id

    def test_an_empty_state_filter_returns_nothing_rather_than_everything(self, store):
        store.create(_feature())
        assert store.list(states=[]) == []

    def test_count(self, store):
        assert store.count() == 0
        store.create(_feature())
        assert store.count() == 1


class TestDurability:
    def test_a_reopened_store_sees_what_was_written(self, tmp_path):
        path = tmp_path / "features.db"
        store = FeatureStore(path)
        feature = store.create(_feature())
        store.close()

        reopened = FeatureStore(path)
        try:
            assert reopened.get(feature.id) is not None
            assert reopened.next_id() == "FEAT-00002"
        finally:
            reopened.close()

    def test_the_incident_database_is_not_touched(self, tmp_path):
        # Features live in their own file. A feature-schema change must not be
        # able to take the incident store with it.
        store = FeatureStore(tmp_path / "features.db")
        try:
            assert store.path.name == "features.db"
            assert not (tmp_path / "incidents.db").exists()
        finally:
            store.close()
