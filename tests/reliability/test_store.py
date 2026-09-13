"""Tests for the incident store and its hash-chained transition log."""

from __future__ import annotations

import sqlite3

import pytest

from openjarvis.core.events import EventBus
from openjarvis.reliability.events import (
    RELIABILITY_INCIDENT_OPENED,
    RELIABILITY_INCIDENT_TRANSITION,
)
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import (
    Correlation,
    Evidence,
    EvidenceKind,
    Incident,
    IncidentState,
    InvalidTransitionError,
    RepairAttempt,
    Severity,
    TrustLevel,
    VerificationResult,
)


@pytest.fixture
def store(tmp_path):
    s = IncidentStore(tmp_path / "reliability" / "incidents.db")
    yield s
    s.close()


def _incident(**overrides) -> Incident:
    defaults = dict(
        fingerprint="fp_abc",
        severity=Severity.HIGH,
        component="authentication",
        title="Login does not reach the dashboard",
    )
    defaults.update(overrides)
    return Incident(**defaults)


class TestIdAllocation:
    def test_ids_are_monotonic_and_padded(self, store):
        assert store.next_id() == "INC-00001"
        assert store.next_id() == "INC-00002"
        assert store.next_id() == "INC-00003"

    def test_create_assigns_id(self, store):
        incident = store.create(_incident())
        assert incident.id == "INC-00001"

    def test_create_preserves_explicit_id(self, store):
        incident = store.create(_incident(id="INC-99999"))
        assert incident.id == "INC-99999"

    def test_ids_not_reused_after_delete(self, store):
        first = store.create(_incident())
        store.delete(first.id)
        second = store.create(_incident())
        assert second.id != first.id

    def test_creates_parent_directory(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "incidents.db"
        s = IncidentStore(path)
        try:
            assert path.parent.is_dir()
        finally:
            s.close()


class TestCrud:
    def test_get_missing_returns_none(self, store):
        assert store.get("INC-00404") is None

    def test_round_trip(self, store):
        original = _incident(
            summary="Auth succeeds but the dashboard never loads.",
            repro_steps=["Open /login", "Submit credentials", "Observe redirect"],
            correlation=Correlation(commit_sha="abc123", confidence=0.6),
            metadata={"probe_run": 7},
        )
        original.add_evidence(
            Evidence(kind=EvidenceKind.CONSOLE_ERROR, summary="TypeError")
        )
        original.add_attempt(RepairAttempt(number=1, branch="jarvis/x"))
        store.create(original)

        loaded = store.get(original.id)
        assert loaded is not None
        assert loaded.title == original.title
        assert loaded.severity is Severity.HIGH
        assert loaded.repro_steps == original.repro_steps
        assert loaded.correlation.commit_sha == "abc123"
        assert loaded.metadata == {"probe_run": 7}
        assert len(loaded.evidence) == 1
        assert loaded.evidence[0].kind is EvidenceKind.CONSOLE_ERROR
        assert len(loaded.attempts) == 1
        assert loaded.attempts[0].branch == "jarvis/x"

    def test_count(self, store):
        assert store.count() == 0
        store.create(_incident())
        store.create(_incident(fingerprint="fp_other"))
        assert store.count() == 2

    def test_list_filters(self, store):
        store.create(_incident(severity=Severity.CRITICAL, fingerprint="fp_1"))
        medium = store.create(_incident(severity=Severity.MEDIUM, fingerprint="fp_2"))
        store.transition(medium, IncidentState.INVESTIGATING)

        assert len(store.list()) == 2
        assert len(store.list(severity=Severity.CRITICAL)) == 1
        assert len(store.list(state=IncidentState.INVESTIGATING)) == 1
        assert len(store.list(state=IncidentState.DETECTED)) == 1

    def test_list_open_only(self, store):
        resolved = store.create(_incident(fingerprint="fp_1"))
        store.transition(resolved, IncidentState.RESOLVED, reason="transient")
        store.create(_incident(fingerprint="fp_2"))
        assert len(store.list(open_only=True)) == 1
        assert len(store.list()) == 2

    def test_list_respects_limit(self, store):
        for index in range(5):
            store.create(_incident(fingerprint=f"fp_{index}"))
        assert len(store.list(limit=3)) == 3

    def test_add_evidence_persists(self, store):
        incident = store.create(_incident())
        store.add_evidence(
            incident,
            Evidence(
                kind=EvidenceKind.SCREENSHOT,
                artifact_path="/tmp/a.png",
                trust=TrustLevel.TRUSTED,
            ),
        )
        loaded = store.get(incident.id)
        assert len(loaded.evidence) == 1
        assert loaded.evidence[0].trust is TrustLevel.TRUSTED

    def test_add_and_update_attempt(self, store):
        incident = store.create(_incident())
        attempt = RepairAttempt(number=1, branch="jarvis/incident-1")
        store.add_attempt(incident, attempt)

        attempt.verification = VerificationResult(passed=True, probe_id="login")
        attempt.outcome = "verified"
        store.update_attempt(incident, attempt)

        loaded = store.get(incident.id)
        assert len(loaded.attempts) == 1
        assert loaded.attempts[0].verified
        assert loaded.attempts[0].outcome == "verified"

    def test_record_occurrence_persists(self, store):
        incident = store.create(_incident())
        assert store.record_occurrence(incident) == 2
        assert store.get(incident.id).occurrences == 2

    def test_delete_removes_incident_and_children(self, store):
        incident = store.create(_incident())
        store.add_evidence(incident, Evidence(kind=EvidenceKind.NOTE, summary="x"))
        store.delete(incident.id)
        assert store.get(incident.id) is None

    def test_malformed_json_column_does_not_crash(self, store):
        incident = store.create(_incident())
        store._conn.execute(
            "UPDATE incidents SET metadata = ? WHERE id = ?",
            ("not json", incident.id),
        )
        store._conn.commit()
        assert store.get(incident.id).metadata == {}


class TestFingerprintLookup:
    def test_finds_open_incident(self, store):
        store.create(_incident(fingerprint="fp_dup"))
        found = store.find_by_fingerprint("fp_dup")
        assert found is not None
        assert found.fingerprint == "fp_dup"

    def test_ignores_resolved_by_default(self, store):
        incident = store.create(_incident(fingerprint="fp_dup"))
        store.transition(incident, IncidentState.RESOLVED, reason="transient")
        assert store.find_by_fingerprint("fp_dup") is None

    def test_include_resolved(self, store):
        incident = store.create(_incident(fingerprint="fp_dup"))
        store.transition(incident, IncidentState.RESOLVED, reason="transient")
        assert store.find_by_fingerprint("fp_dup", include_resolved=True) is not None

    def test_unknown_fingerprint(self, store):
        assert store.find_by_fingerprint("fp_nope") is None


class TestTransitions:
    def test_transition_persists(self, store):
        incident = store.create(_incident())
        store.transition(incident, IncidentState.INVESTIGATING, reason="triage")
        loaded = store.get(incident.id)
        assert loaded.state is IncidentState.INVESTIGATING

    def test_illegal_transition_writes_nothing(self, store):
        incident = store.create(_incident())
        before = store.transitions_for(incident.id)
        with pytest.raises(InvalidTransitionError):
            store.transition(incident, IncidentState.FIXING)
        assert store.get(incident.id).state is IncidentState.DETECTED
        assert store.transitions_for(incident.id) == before

    def test_creation_is_recorded_in_history(self, store):
        incident = store.create(_incident())
        history = store.transitions_for(incident.id)
        assert len(history) == 1
        assert history[0].reason == "incident opened"

    def test_history_is_ordered_and_complete(self, store):
        incident = store.create(_incident())
        store.transition(incident, IncidentState.INVESTIGATING)
        store.transition(incident, IncidentState.REPRODUCING)
        store.transition(incident, IncidentState.FIXING)
        history = store.transitions_for(incident.id)
        assert [t.to_state for t in history] == [
            IncidentState.DETECTED,
            IncidentState.INVESTIGATING,
            IncidentState.REPRODUCING,
            IncidentState.FIXING,
        ]

    def test_history_survives_incident_delete(self, store):
        """The audit trail outlives the record it describes."""
        incident = store.create(_incident())
        store.transition(incident, IncidentState.INVESTIGATING)
        store.delete(incident.id)
        assert len(store.transitions_for(incident.id)) == 2


class TestAuditChain:
    def test_empty_chain_verifies(self, store):
        assert store.verify_chain() == (True, None)

    def test_chain_verifies_after_activity(self, store):
        for index in range(3):
            incident = store.create(_incident(fingerprint=f"fp_{index}"))
            store.transition(incident, IncidentState.INVESTIGATING)
            store.transition(incident, IncidentState.REPRODUCING)
        assert store.verify_chain() == (True, None)

    def test_tampering_with_a_reason_is_detected(self, store):
        incident = store.create(_incident())
        store.transition(incident, IncidentState.INVESTIGATING, reason="triage")
        store._conn.execute(
            "UPDATE incident_transitions SET reason = ? WHERE id = ?",
            ("something else entirely", 2),
        )
        store._conn.commit()
        intact, row = store.verify_chain()
        assert not intact
        assert row == 2

    def test_deleting_a_row_is_detected(self, store):
        incident = store.create(_incident())
        store.transition(incident, IncidentState.INVESTIGATING)
        store.transition(incident, IncidentState.REPRODUCING)
        store._conn.execute("DELETE FROM incident_transitions WHERE id = 2")
        store._conn.commit()
        intact, _ = store.verify_chain()
        assert not intact

    def test_tail_hash_advances(self, store):
        assert store.tail_hash() == ""
        incident = store.create(_incident())
        first = store.tail_hash()
        assert first
        store.transition(incident, IncidentState.INVESTIGATING)
        assert store.tail_hash() != first


class TestEvents:
    def test_publishes_incident_opened(self, store_with_bus):
        store, bus = store_with_bus
        store.create(_incident())
        types = [event.event_type for event in bus.history]
        assert RELIABILITY_INCIDENT_OPENED in types

    def test_publishes_transition(self, store_with_bus):
        store, bus = store_with_bus
        incident = store.create(_incident())
        store.transition(incident, IncidentState.INVESTIGATING, reason="triage")
        events = [
            event
            for event in bus.history
            if event.event_type == RELIABILITY_INCIDENT_TRANSITION
        ]
        assert len(events) == 1
        assert events[0].data["incident_id"] == incident.id
        assert events[0].data["state"] == "INVESTIGATING"
        assert events[0].data["reason"] == "triage"

    def test_subscriber_receives_string_keyed_event(self, store_with_bus):
        store, bus = store_with_bus
        seen = []
        bus.subscribe(RELIABILITY_INCIDENT_OPENED, seen.append)
        store.create(_incident())
        assert len(seen) == 1

    def test_bad_subscriber_does_not_break_the_store(self, store_with_bus):
        store, bus = store_with_bus

        def boom(_event):
            raise RuntimeError("subscriber exploded")

        bus.subscribe(RELIABILITY_INCIDENT_OPENED, boom)
        incident = store.create(_incident())
        assert store.get(incident.id) is not None


@pytest.fixture
def store_with_bus(tmp_path):
    bus = EventBus(record_history=True)
    s = IncidentStore(tmp_path / "incidents.db", bus=bus)
    yield s, bus
    s.close()


class TestPersistenceAcrossConnections:
    def test_reopen_sees_prior_data(self, tmp_path):
        path = tmp_path / "incidents.db"
        first = IncidentStore(path)
        incident = first.create(_incident())
        first.transition(incident, IncidentState.INVESTIGATING)
        first.close()

        second = IncidentStore(path)
        try:
            loaded = second.get(incident.id)
            assert loaded is not None
            assert loaded.state is IncidentState.INVESTIGATING
            assert second.verify_chain() == (True, None)
            # Sequence continues rather than restarting.
            assert second.next_id() == "INC-00002"
        finally:
            second.close()

    def test_schema_is_idempotent(self, tmp_path):
        path = tmp_path / "incidents.db"
        for _ in range(3):
            s = IncidentStore(path)
            s.close()
        conn = sqlite3.connect(str(path))
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        assert {
            "incidents",
            "incident_evidence",
            "incident_attempts",
            "incident_transitions",
            "incident_sequence",
        } <= names


def _escalate_in_another_process(db_path: str, incident_id: str, ready, go) -> None:
    """Escalate an incident from a genuinely separate process."""
    from openjarvis.reliability.store import IncidentStore
    from openjarvis.reliability.types import IncidentState

    other = IncidentStore(db_path)
    try:
        incident = other.get(incident_id)
        ready.set()
        go.wait(timeout=30.0)
        other.transition(
            incident, IncidentState.INVESTIGATING, reason="the other watcher"
        )
    finally:
        other.close()


class TestStaleWritesAreRefused:
    """A second writer must not overwrite a state it never saw.

    Every scalar write went through `INSERT OR REPLACE`, which is an
    unconditional clobber. Two watchers, a watcher and a CLI, a watcher and the
    Control Center -- any second process holding a read from a moment ago wrote
    its whole row back on top, state included. The expensive case is an
    escalation: a HUMAN_REQUIRED incident silently un-escalated by a process
    that still believed it was INVESTIGATING, and nobody told the person who was
    supposed to be asked.
    """

    def test_a_stale_transition_is_refused(self, store):
        from openjarvis.reliability.store import ConcurrentIncidentModification

        created = store.create(_incident())
        first = store.get(created.id)
        stale = store.get(created.id)

        store.transition(first, IncidentState.INVESTIGATING, reason="first")

        with pytest.raises(ConcurrentIncidentModification):
            store.transition(stale, IncidentState.INVESTIGATING, reason="stale")

        assert store.get(created.id).state is IncidentState.INVESTIGATING

    def test_a_stale_save_is_refused(self, store):
        from openjarvis.reliability.store import ConcurrentIncidentModification

        created = store.create(_incident())
        first = store.get(created.id)
        stale = store.get(created.id)

        first.title = "written by the first"
        store.save(first)

        stale.title = "written by the stale one"
        with pytest.raises(ConcurrentIncidentModification):
            store.save(stale)

        assert store.get(created.id).title == "written by the first"

    def test_an_escalation_survives_a_stale_writer(self, store):
        """The case this exists for, stated in its own terms."""
        from openjarvis.reliability.store import ConcurrentIncidentModification

        created = store.create(_incident())
        escalating = store.get(created.id)
        stale = store.get(created.id)

        store.transition(escalating, IncidentState.INVESTIGATING, reason="looking")
        store.transition(
            escalating, IncidentState.HUMAN_REQUIRED, reason="I need a person"
        )

        with pytest.raises(ConcurrentIncidentModification):
            store.transition(stale, IncidentState.INVESTIGATING, reason="stale view")

        assert store.get(created.id).state is IncidentState.HUMAN_REQUIRED, (
            "a stale writer un-escalated an incident a person was asked to look at"
        )

    def test_a_version_is_assigned_and_bumped(self, store):
        created = store.create(_incident())
        assert created.store_version == 1
        loaded = store.get(created.id)
        assert loaded.store_version == 1
        store.transition(loaded, IncidentState.INVESTIGATING, reason="move")
        assert loaded.store_version == 2
        assert store.get(created.id).store_version == 2

    def test_a_fresh_read_can_always_write(self, store):
        """Refusing a stale write must not refuse a correct one."""
        created = store.create(_incident())
        for target in (IncidentState.INVESTIGATING, IncidentState.REPRODUCING):
            current = store.get(created.id)
            store.transition(current, target, reason="in order")
        assert store.get(created.id).state is IncidentState.REPRODUCING

    def test_two_real_processes_cannot_both_win(self, store, tmp_path):
        """A threading lock proves nothing here; the writers are processes."""
        import multiprocessing

        from openjarvis.reliability.store import ConcurrentIncidentModification

        created = store.create(_incident())
        db_path = str(tmp_path / "reliability" / "incidents.db")
        ready = multiprocessing.Event()
        go = multiprocessing.Event()
        other = multiprocessing.Process(
            target=_escalate_in_another_process,
            args=(db_path, created.id, ready, go),
        )
        other.start()
        try:
            assert ready.wait(timeout=20.0), "the other process never read"
            mine = store.get(created.id)  # read at the same version it has
            go.set()
            other.join(timeout=20.0)
            assert not other.is_alive()
            assert other.exitcode == 0

            with pytest.raises(ConcurrentIncidentModification):
                store.transition(mine, IncidentState.INVESTIGATING, reason="mine")
        finally:
            if other.is_alive():  # pragma: no cover - defensive
                other.terminate()
                other.join(timeout=5.0)


class TestAppendsAdoptRatherThanClobberOrCrash:
    """Attaching evidence must neither overwrite a state nor break a repair.

    The payload of these calls is a row in a child table, already written by the
    time the scalars are touched. Raising would abort a repair *after* its
    evidence was made durable -- worse than the problem. Retrying the write
    would be the clobber itself. So the other writer's scalars win and this
    caller adopts them, keeping the child rows it just appended.
    """

    def test_evidence_from_a_stale_caller_does_not_clobber_the_state(self, store):
        created = store.create(_incident())
        escalating = store.get(created.id)
        stale = store.get(created.id)
        store.transition(escalating, IncidentState.INVESTIGATING, reason="looking")

        store.add_evidence(
            stale,
            Evidence(kind=EvidenceKind.NOTE, summary="a late note", content="body"),
        )

        reloaded = store.get(created.id)
        assert reloaded.state is IncidentState.INVESTIGATING, (
            "an evidence append overwrote a state change it never saw"
        )
        assert "a late note" in [e.summary for e in reloaded.evidence], (
            "the evidence was lost while avoiding the clobber"
        )

    def test_the_stale_caller_stops_being_stale(self, store):
        created = store.create(_incident())
        escalating = store.get(created.id)
        stale = store.get(created.id)
        store.transition(escalating, IncidentState.INVESTIGATING, reason="looking")

        store.add_evidence(
            stale, Evidence(kind=EvidenceKind.NOTE, summary="note", content="b")
        )

        assert stale.state is IncidentState.INVESTIGATING, (
            "the caller went on holding a state the database had already moved past"
        )
        assert stale.store_version == store.get(created.id).store_version

    def test_an_attempt_from_a_stale_caller_is_kept(self, store):
        created = store.create(_incident())
        escalating = store.get(created.id)
        stale = store.get(created.id)
        store.transition(escalating, IncidentState.INVESTIGATING, reason="looking")

        store.add_attempt(stale, RepairAttempt(number=1, claim="tried something"))

        reloaded = store.get(created.id)
        assert reloaded.state is IncidentState.INVESTIGATING
        assert [a.claim for a in reloaded.attempts] == ["tried something"]

    def test_recording_an_occurrence_does_not_undo_an_escalation(self, store):
        created = store.create(_incident())
        escalating = store.get(created.id)
        stale = store.get(created.id)
        store.transition(escalating, IncidentState.INVESTIGATING, reason="looking")
        store.transition(escalating, IncidentState.HUMAN_REQUIRED, reason="help")

        store.record_occurrence(stale)

        assert store.get(created.id).state is IncidentState.HUMAN_REQUIRED


class TestTheSchemaUpgradesInPlace:
    """The operator's incident database predates the version column.

    CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so
    without an explicit migration every write against the live database would
    fail on an unknown column.
    """

    def test_a_database_without_the_column_gains_it(self, tmp_path):
        db = tmp_path / "old" / "incidents.db"
        db.parent.mkdir(parents=True)
        legacy = sqlite3.connect(str(db))
        legacy.execute(
            "CREATE TABLE incidents ("
            " id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, severity TEXT NOT NULL,"
            " component TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',"
            " summary TEXT NOT NULL DEFAULT '', environment TEXT NOT NULL DEFAULT"
            " 'production', source TEXT NOT NULL DEFAULT 'probe', probe_id TEXT NOT"
            " NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'DETECTED', created_at TEXT"
            " NOT NULL, updated_at TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT"
            " 1, last_seen_at TEXT NOT NULL, repro_steps TEXT NOT NULL DEFAULT '[]',"
            " correlation TEXT NOT NULL DEFAULT '{}', resolution TEXT NOT NULL DEFAULT"
            " '{}', metadata TEXT NOT NULL DEFAULT '{}')"
        )
        legacy.execute(
            "INSERT INTO incidents (id, fingerprint, severity, created_at,"
            " updated_at, last_seen_at)"
            " VALUES ('INC-00001', 'fp', 'HIGH', 't', 't', 't')"
        )
        legacy.commit()
        legacy.close()

        upgraded = IncidentStore(db)
        try:
            columns = {
                row["name"]
                for row in upgraded._conn.execute(
                    "PRAGMA table_info(incidents)"
                ).fetchall()
            }
            assert "version" in columns

            # And a pre-existing row is writable rather than stuck at version 0.
            existing = upgraded.get("INC-00001")
            assert existing is not None
            assert existing.store_version >= 1
            existing.title = "still writable after the upgrade"
            upgraded.save(existing)
            assert upgraded.get("INC-00001").title == "still writable after the upgrade"
        finally:
            upgraded.close()
