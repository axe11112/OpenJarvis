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
