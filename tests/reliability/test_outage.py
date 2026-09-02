"""Which failures are one problem, and which are not.

Correlation has an asymmetric failure mode. Grouping too little costs an extra
message; grouping too much hides a separate problem inside one the owner has
already been told about and dismissed. Every test that pushes towards grouping
here is matched by one that refuses it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openjarvis.reliability.outage import (
    Outage,
    OutageRegistry,
    classify_family,
    deployment_identity,
    failure_shape,
    group_incidents,
)
from openjarvis.reliability.types import Correlation, Incident, Severity

T0 = datetime(2026, 8, 22, 7, 12, tzinfo=timezone.utc)


class _FixedNow(datetime):
    """A ``datetime`` subclass whose ``.now()`` always answers with a time
    pinned just after T0, regardless of the real wall clock.

    Found live: OutageRegistry._prune_locked() read real wall-clock time
    directly rather than an injectable clock, so every one of these fixtures
    -- built around the fixed T0 above -- silently started failing the
    moment real time advanced far enough past T0 for DEFAULT_RETENTION to
    treat a just-created outage as already expired. Fixed at the source
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
    number=1,
    component="website",
    probe="homepage",
    *,
    offset=0,
    kind="navigation",
    severity=Severity.CRITICAL,
    sha="9f31c04",
    title="",
    status=0,
    source="probe",
    environment="production",
) -> Incident:
    at = (T0 + timedelta(seconds=offset)).isoformat()
    return Incident(
        fingerprint=f"fp_{component}_{kind}_{number}",
        severity=severity,
        component=component,
        title=title or f"{component} is unreachable",
        id=f"INC-{number:05d}",
        probe_id=probe,
        source=source,
        environment=environment,
        created_at=at,
        last_seen_at=at,
        updated_at=at,
        correlation=Correlation(deployment_id=sha, commit_sha=sha)
        if sha
        else Correlation(),
        metadata={"failure_kind": kind, "http_status": status},
    )


# -- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    "component", ["website", "authentication", "signup", "dashboard", "api"]
)
def test_the_web_surface_shares_an_availability_family(component):
    assert classify_family(make(component=component)) == "site_availability"


@pytest.mark.parametrize(
    "component,expected",
    [
        ("database", "database"),
        ("supabase", "database"),
        ("stripe", "external_provider"),
    ],
)
def test_other_systems_get_their_own_family(component, expected):
    assert classify_family(make(component=component)) == expected


def test_a_security_failure_is_its_own_family_whatever_the_component():
    breach = make(component="authentication", title="Unauthorized access was allowed")
    assert classify_family(breach) == "auth_security"


def test_an_unknown_component_gets_its_own_family():
    """An extra message is the safe answer to "I do not recognise this"."""
    assert classify_family(make(component="widgetron")).startswith("component:")


def test_two_unknown_components_never_merge_even_on_one_deployment():
    registry = OutageRegistry()
    a = registry.assign(make(1, "widgetron", "widgets", sha="9f31c04"))
    b = registry.assign(make(2, "sprocketry", "sprockets", offset=30, sha="9f31c04"))
    assert a.key != b.key


@pytest.mark.parametrize(
    "kind,status,expected",
    [
        ("timeout", 0, "availability"),
        ("navigation", 0, "availability"),
        ("http_error", 503, "availability"),
        ("http_error", 401, "contract"),
        ("assertion", 0, "contract"),
        ("duration", 0, "availability"),
    ],
)
def test_failure_shape_separates_not_serving_from_serving_badly(kind, status, expected):
    assert failure_shape(make(kind=kind, status=status)) == expected


def test_a_contract_failure_does_not_group_across_components():
    """A wrong assertion on one page is a claim about that page."""
    registry = OutageRegistry()
    first = registry.assign(make(1, "signup", "signup", kind="assertion", sha=""))
    second = registry.assign(
        make(2, "dashboard", "dashboard", offset=30, kind="assertion", sha="")
    )
    assert first.key != second.key


def test_a_shared_deployment_does_group_contract_failures():
    """Evidence, not proximity: one broken artifact is one problem."""
    registry = OutageRegistry()
    first = registry.assign(
        make(1, "signup", "signup", kind="assertion", sha="9f31c04")
    )
    second = registry.assign(
        make(2, "dashboard", "dashboard", offset=30, kind="assertion", sha="9f31c04")
    )
    assert first.key == second.key


# -- grouping ---------------------------------------------------------------


def test_five_probes_on_one_deployment_are_one_outage():
    registry, grouped = group_incidents(
        [
            make(41, "website", "homepage", offset=0),
            make(42, "authentication", "login", offset=60),
            make(43, "signup", "signup", offset=120),
            make(44, "dashboard", "dashboard", offset=180),
            make(45, "api", "api-health", offset=240),
        ]
    )
    assert len(grouped) == 1
    outage = registry.open_outages()[0]
    assert len(outage.incident_ids) == 5
    assert len(outage.probes) == 5


def test_a_different_environment_is_a_different_outage():
    registry = OutageRegistry()
    a = registry.assign(make(1, environment="production"))
    b = registry.assign(make(2, offset=30, environment="staging"))
    assert a.key != b.key


def test_a_different_deployment_is_a_different_outage():
    registry = OutageRegistry()
    a = registry.assign(make(1, sha="9f31c04"))
    b = registry.assign(make(2, "signup", "signup", offset=30, sha="ab77e19"))
    assert a.key != b.key


def test_an_abbreviated_sha_matches_the_full_one():
    registry = OutageRegistry()
    a = registry.assign(make(1, sha="9f31c04"))
    b = registry.assign(make(2, "signup", "signup", offset=30, sha="9f31c04ff8821a"))
    assert a.key == b.key


def test_a_failure_outside_the_window_is_a_new_outage():
    registry = OutageRegistry()
    a = registry.assign(make(1))
    b = registry.assign(make(2, "signup", "signup", offset=3600))
    assert a.key != b.key


def test_assignment_is_idempotent():
    registry = OutageRegistry()
    incident = make(1)
    first = registry.assign(incident)
    for _ in range(5):
        registry.assign(incident)
    assert registry.get(first.key).incident_ids == ["INC-00001"]


def test_a_new_incident_with_a_known_fingerprint_rejoins_its_outage():
    registry = OutageRegistry()
    first = registry.assign(make(1))
    later = make(1)
    later.id = "INC-00099"
    assert registry.assign(later).key == first.key


# -- persistence ------------------------------------------------------------


def test_the_registry_survives_a_restart(tmp_path):
    path = tmp_path / "outages.json"
    first = OutageRegistry(path=path).assign(make(41, "website", "homepage"))
    reopened = OutageRegistry(path=path)
    joined = reopened.assign(make(42, "authentication", "login", offset=60))
    assert joined.key == first.key


def test_a_second_process_sees_the_first_ones_writes(tmp_path):
    path = tmp_path / "outages.json"
    writer, reader = OutageRegistry(path=path), OutageRegistry(path=path)
    key = writer.assign(make(41, "website", "homepage")).key
    # The reader loaded before the write. It must not answer from that snapshot.
    assert reader.assign(make(42, "authentication", "login", offset=60)).key == key


def test_a_corrupt_registry_is_empty_not_fatal(tmp_path):
    path = tmp_path / "outages.json"
    path.write_text("not json at all", encoding="utf-8")
    registry = OutageRegistry(path=path)
    assert registry.assign(make(1)).key


def test_an_unwritable_registry_does_not_stop_correlation(tmp_path):
    directory = tmp_path / "locked"
    directory.mkdir()
    registry = OutageRegistry(path=directory / "nested" / "outages.json")
    directory.chmod(0o500)
    try:
        assert registry.assign(make(1)).key  # must not raise
    finally:
        directory.chmod(0o700)


# -- lifecycle --------------------------------------------------------------


def test_resolving_keeps_the_record():
    registry = OutageRegistry()
    key = registry.assign(make(1)).key
    registry.resolve(key)
    assert registry.get(key).resolved is True
    assert registry.open_outages() == []


def test_a_resolved_outage_does_not_absorb_a_new_failure():
    registry = OutageRegistry()
    key = registry.assign(make(1)).key
    registry.resolve(key)
    assert registry.assign(make(2, "signup", "signup", offset=60)).key != key


def test_severity_is_the_worst_of_the_members():
    registry = OutageRegistry()
    registry.assign(make(1, severity=Severity.HIGH))
    outage = registry.assign(
        make(2, "signup", "signup", offset=30, severity=Severity.CRITICAL)
    )
    assert outage.severity is Severity.CRITICAL


def test_correlation_notes_explain_why_probes_were_merged():
    registry = OutageRegistry()
    registry.assign(make(41, "website", "homepage"))
    outage = registry.assign(make(42, "authentication", "login", offset=60))
    assert any("joined" in note for note in outage.notes)


def test_deployment_identity_prefers_recorded_correlation():
    assert deployment_identity(make(sha="9f31c04")) == "9f31c04"
    bare = make(sha="")
    bare.metadata["deployment_sha"] = "ab77e19"
    assert deployment_identity(bare) == "ab77e19"


def test_an_outage_round_trips_through_a_dict():
    registry = OutageRegistry()
    outage = registry.assign(make(1))
    assert Outage.from_dict(outage.to_dict()).to_dict() == outage.to_dict()
