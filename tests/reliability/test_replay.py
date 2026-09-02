"""Measuring the policy against history that actually happened.

The replay exists to answer one question with a number rather than a claim:
how many messages would each policy have sent? These tests check the two
properties that make the number worth quoting — that the "after" count comes
from the real router rather than a model of it, and that reading history never
changes it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openjarvis.reliability.replay import (
    load_incidents,
    replay,
    replay_new,
    replay_old,
)
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import (
    Correlation,
    Incident,
    IncidentState,
    IncidentTransition,
    Severity,
)

T0 = datetime(2026, 8, 22, 7, 12, tzinfo=timezone.utc)


class _FixedNow(datetime):
    """A ``datetime`` subclass whose ``.now()`` always answers with a time
    pinned just after T0, regardless of the real wall clock.

    Found live: OutageRegistry._prune_locked() read real wall-clock time
    directly rather than an injectable clock, so every fixture here -- built
    around the fixed T0 above -- silently started failing the moment real
    time advanced far enough past T0 for DEFAULT_RETENTION to treat a
    just-created outage as already expired. Fixed at the source
    (OutageRegistry.clock is now injectable); this pins these tests so the
    same class of failure cannot recur as real time keeps moving.
    """

    @classmethod
    def now(cls, tz=None):
        return T0 + timedelta(hours=1)


@pytest.fixture(autouse=True)
def _pin_the_outage_clock(monkeypatch):
    import openjarvis.reliability.outage as outage_module

    monkeypatch.setattr(outage_module, "datetime", _FixedNow)


def make(
    number,
    component,
    probe,
    offset,
    *,
    kind="navigation",
    sha="9f31c04",
    final=IncidentState.HUMAN_REQUIRED,
    reason="3 repair attempts did not produce a verified fix",
):
    at = (T0 + timedelta(seconds=offset)).isoformat()
    incident = Incident(
        fingerprint=f"fp_{component}_{kind}",
        severity=Severity.CRITICAL,
        component=component,
        title=f"{component} is unreachable",
        id=f"INC-{number:05d}",
        probe_id=probe,
        created_at=at,
        last_seen_at=at,
        updated_at=at,
        correlation=Correlation(deployment_id=sha, commit_sha=sha),
        metadata={"failure_kind": kind, "http_status": 0},
    )
    incident.transitions.append(
        IncidentTransition(
            from_state=IncidentState.DETECTED,
            to_state=final,
            actor="jarvis",
            reason=reason,
        )
    )
    incident.state = final
    return incident


def the_morning():
    return [
        make(41, "website", "homepage", 0),
        make(42, "authentication", "login", 95),
        make(43, "signup", "signup", 150),
        make(44, "authentication", "login", 420, kind="timeout"),
        make(45, "dashboard", "dashboard", 480),
    ]


def test_the_old_policy_sends_two_messages_per_incident():
    """A CRITICAL detection, then the escalation minutes later."""
    result = replay_old(the_morning())
    assert result.count == 10
    kinds = [entry["kind"] for entry in result.messages]
    assert kinds.count("alert") == 5
    assert kinds.count("human_required") == 5


def test_the_new_policy_sends_one():
    result = replay_new(the_morning())
    assert result.count == 1
    assert result.outages == 1


def test_both_counts_come_from_the_same_history():
    outcome = replay(the_morning())
    assert outcome["before"].incidents == outcome["after"].incidents == 5


def test_the_replay_never_writes_to_the_incidents_it_reads():
    incidents = the_morning()
    replay(incidents)
    for incident in incidents:
        assert "outage_key" not in (incident.metadata or {})
        assert "owner_ask" not in (incident.metadata or {})


def test_a_resolution_the_owner_heard_about_counts_as_one_message():
    incidents = [
        make(41, "website", "homepage", 0),
        make(41, "website", "homepage", 300, final=IncidentState.RESOLVED),
    ]
    result = replay_new(incidents)
    assert result.count == 2  # the ask, then "it's fixed"


def test_an_incident_nobody_was_told_about_recovering_says_nothing():
    result = replay_new(
        [
            make(
                90,
                "billing",
                "invoice",
                0,
                kind="assertion",
                final=IncidentState.RESOLVED,
            )
        ]
    )
    assert result.count == 0


def test_reading_a_real_store_leaves_it_unchanged(tmp_path):
    path = tmp_path / "incidents.db"
    store = IncidentStore(path)
    try:
        for incident in the_morning():
            incident.id = ""
            store.create(incident)
        before = store.count()
    finally:
        store.close()

    loaded = load_incidents(path)
    replay(loaded)

    store = IncidentStore(path)
    try:
        assert store.count() == before
        assert store.verify_chain()[0] is True
    finally:
        store.close()


def test_incidents_come_back_oldest_first(tmp_path):
    path = tmp_path / "incidents.db"
    store = IncidentStore(path)
    try:
        for incident in the_morning():
            incident.id = ""
            store.create(incident)
    finally:
        store.close()

    loaded = load_incidents(path)
    assert [i.created_at for i in loaded] == sorted(i.created_at for i in loaded)
