"""Feature persistence."""

from __future__ import annotations

import sqlite3

import pytest

from openjarvis.wiz.features.model import FeatureRequest, FeatureState
from openjarvis.wiz.features.store import ConcurrentModificationError, FeatureStore


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


class TestOptimisticConcurrency:
    """save() must never let a stale write silently clobber a fresher one —
    the ordinary shape of two processes (or two threads) both loading the
    same feature and both saving what they each independently decided."""

    def test_two_saves_from_the_same_load_the_second_is_refused(self, store):
        created = store.create(_feature())
        first_view = store.get(created.id)
        second_view = store.get(created.id)

        first_view.transition(FeatureState.UNDERSTANDING, at="t1")
        store.save(first_view)  # succeeds, bumps the row's version

        second_view.transition(FeatureState.UNDERSTANDING, at="t1")
        with pytest.raises(ConcurrentModificationError) as excinfo:
            store.save(second_view)
        assert excinfo.value.feature_id == created.id
        assert excinfo.value.expected_version == excinfo.value.actual_version - 1

        # The first writer's work is intact - not silently overwritten by
        # the second, stale writer.
        assert store.get(created.id).state is FeatureState.UNDERSTANDING

    def test_a_losing_saver_can_reload_and_retry(self, store):
        created = store.create(_feature())
        stale = store.get(created.id)

        fresh = store.get(created.id)
        fresh.transition(FeatureState.UNDERSTANDING, at="t1")
        store.save(fresh)

        stale.transition(FeatureState.UNDERSTANDING, at="t1")
        with pytest.raises(ConcurrentModificationError):
            store.save(stale)

        # The documented recovery: reload (picking up the current version),
        # redecide, and save again - this must succeed.
        reloaded = store.get(created.id)
        reloaded.transition(FeatureState.PLANNING, at="t2")
        store.save(reloaded)
        assert store.get(created.id).state is FeatureState.PLANNING

    def test_repeated_saves_on_the_same_object_keep_working(self, store):
        # save() bumps feature.store_version on success, so the *same*
        # Python object can be saved again and again without a reload -
        # this is how the pipeline actually uses it (one FeatureRequest
        # object, mutated and saved many times across a run).
        feature = store.create(_feature())
        for i, target in enumerate(
            [FeatureState.UNDERSTANDING, FeatureState.PLANNING], start=1
        ):
            feature.transition(target, at=f"t{i}")
            store.save(feature)
        assert store.get(feature.id).state is FeatureState.PLANNING

    def test_create_starts_at_version_one(self, store):
        feature = store.create(_feature())
        assert feature.store_version == 1

    def test_store_version_is_not_part_of_the_document(self, store):
        # It is store bookkeeping, not domain state - it must never leak
        # into the JSON document, the journal, or to_dict()/from_dict().
        feature = store.create(_feature())
        assert "store_version" not in feature.to_dict()
        from_scratch = FeatureRequest.from_dict(feature.to_dict())
        assert from_scratch.store_version == 0

    def test_a_feature_never_loaded_through_the_store_cannot_bypass_the_check(
        self, store
    ):
        # store_version defaults to 0 on a bare FeatureRequest. Saving one
        # against an id that was already created (and is therefore at
        # version >= 1) must be refused like any other stale write, not
        # silently accepted because it never went through get().
        created = store.create(_feature())
        store.save(created)  # version now 2

        bare = FeatureRequest(id=created.id, title="x", created_at="t")
        with pytest.raises(ConcurrentModificationError):
            store.save(bare)


class TestSchemaMigration:
    def test_a_database_written_before_the_version_column_existed_still_opens(
        self, tmp_path
    ):
        path = tmp_path / "features.db"
        # Simulate a features.db created by an older version of this store,
        # which had no version column at all.
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE features (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                priority TEXT NOT NULL,
                risk TEXT NOT NULL DEFAULT 'LOW',
                target TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                document TEXT NOT NULL
            );
            """
        )
        legacy_feature = _feature(id="FEAT-00001")
        conn.execute(
            "INSERT INTO features (id, title, state, priority, risk, target, "
            "source, created_at, updated_at, document) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                legacy_feature.id,
                legacy_feature.title,
                legacy_feature.state.value,
                legacy_feature.priority.value,
                legacy_feature.risk,
                legacy_feature.target,
                legacy_feature.source,
                legacy_feature.created_at,
                legacy_feature.created_at,
                __import__("json").dumps(legacy_feature.to_dict()),
            ),
        )
        conn.commit()
        conn.close()

        store = FeatureStore(path)
        try:
            loaded = store.get("FEAT-00001")
            assert loaded is not None
            assert loaded.store_version == 1  # DEFAULT 1 from the migration
            loaded.transition(FeatureState.UNDERSTANDING, at="t1")
            store.save(loaded)  # must not raise - the migrated row is usable
            assert store.get("FEAT-00001").state is FeatureState.UNDERSTANDING
        finally:
            store.close()


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
