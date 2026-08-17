"""Tests for detection: confirmation, dedup, recovery and noise suppression."""

from __future__ import annotations

import pytest

from openjarvis.reliability.detector import Detector
from openjarvis.reliability.probes.spec import parse_probe
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import (
    Evidence,
    EvidenceKind,
    IncidentState,
    ProbeResult,
    Severity,
    Signal,
)


@pytest.fixture
def store(tmp_path):
    s = IncidentStore(tmp_path / "incidents.db")
    yield s
    s.close()


def _spec(**overrides):
    probe = {
        "id": "auth-login",
        "component": "authentication",
        "severity": "high",
        "steps": [{"action": "goto", "url": "/login"}],
        "expect": [{"kind": "url", "matches": "/dashboard"}],
        "retry": {"confirm_runs": 2},
    }
    probe.update(overrides)
    return parse_probe({"probe": probe})


def _fail(**overrides):
    base = dict(
        probe_id="auth-login",
        success=False,
        failure_kind="assertion",
        error="expected the URL to match /dashboard, got /login",
        steps_completed=4,
    )
    base.update(overrides)
    return ProbeResult(**base)


def _pass():
    return ProbeResult(probe_id="auth-login", success=True, steps_completed=4)


class TestConfirmation:
    def test_first_failure_does_not_open_an_incident(self, store):
        detector = Detector(store)
        detection = detector.from_probe(_spec(), _fail())
        assert detection.suppressed
        assert "awaiting confirmation (1/2)" in detection.reason
        assert store.count() == 0

    def test_second_consecutive_failure_opens_one(self, store):
        detector = Detector(store)
        detector.from_probe(_spec(), _fail())
        detection = detector.from_probe(_spec(), _fail())
        assert detection.opened
        assert store.count() == 1

    def test_a_pass_between_failures_resets_the_count(self, store):
        """An intermittent blip must never accumulate into an incident."""
        detector = Detector(store)
        detector.from_probe(_spec(), _fail())
        detector.from_probe(_spec(), _pass())
        detector.from_probe(_spec(), _fail())
        assert store.count() == 0

    def test_confirm_runs_of_one_opens_immediately(self, store):
        detector = Detector(store)
        spec = _spec(retry={"confirm_runs": 1})
        assert detector.from_probe(spec, _fail()).opened


class TestSelfFailures:
    @pytest.mark.parametrize("kind", ["misconfigured", "runner_error"])
    def test_jarvis_problems_never_look_like_site_problems(self, store, kind):
        """Saying "the website is broken" because a credential is missing would
        be a lie, and an alarming one."""
        detector = Detector(store)
        spec = _spec(retry={"confirm_runs": 1})
        detection = detector.from_probe(spec, _fail(failure_kind=kind, error="no $X"))
        assert detection.suppressed
        assert store.count() == 0

    def test_self_failures_do_not_advance_confirmation(self, store):
        detector = Detector(store)
        spec = _spec(retry={"confirm_runs": 2})
        detector.from_probe(spec, _fail(failure_kind="misconfigured"))
        detector.from_probe(spec, _fail(failure_kind="misconfigured"))
        assert store.count() == 0


class TestIncidentContent:
    def _open(self, store, **result_kwargs):
        detector = Detector(store)
        spec = _spec(retry={"confirm_runs": 1})
        return detector.from_probe(spec, _fail(**result_kwargs)).incident

    def test_carries_reproduction_steps(self, store):
        incident = self._open(store)
        assert incident.repro_steps
        assert incident.repro_steps[0] == "Open /login"

    def test_records_expected_and_actual(self, store):
        incident = self._open(store)
        assert "/dashboard" in incident.metadata["expected"]
        assert "got /login" in incident.metadata["actual"]

    def test_carries_evidence(self, store):
        detector = Detector(store)
        spec = _spec(retry={"confirm_runs": 1})
        result = _fail(
            evidence=[Evidence(kind=EvidenceKind.CONSOLE_ERROR, summary="TypeError")]
        )
        incident = detector.from_probe(spec, result).incident
        assert len(incident.evidence) == 1

    def test_severity_is_escalated_by_impact(self, store):
        """A site that will not load at all outranks its declared severity."""
        incident = self._open(store, failure_kind="navigation", steps_completed=0)
        assert incident.severity is Severity.CRITICAL

    def test_declared_severity_is_preserved_in_metadata(self, store):
        incident = self._open(store, failure_kind="navigation", steps_completed=0)
        assert incident.metadata["declared_severity"] == "HIGH"

    @pytest.mark.parametrize(
        ("kind", "fragment"),
        [
            ("timeout", "timed out"),
            ("navigation", "unreachable"),
            ("console_error", "JavaScript errors"),
            ("assertion", "failed"),
        ],
    )
    def test_titles_describe_the_failure(self, store, kind, fragment):
        incident = self._open(store, failure_kind=kind)
        assert fragment in incident.title


class TestDeduplication:
    def test_repeat_failure_increments_rather_than_duplicating(self, store):
        detector = Detector(store)
        spec = _spec(retry={"confirm_runs": 1})
        first = detector.from_probe(spec, _fail()).incident
        detection = detector.from_probe(spec, _fail())

        assert detection.recurred
        assert store.count() == 1
        assert store.get(first.id).occurrences == 2

    def test_volatile_detail_still_deduplicates(self, store):
        """Timestamps and durations differ every run; the incident must not."""
        detector = Detector(store)
        spec = _spec(retry={"confirm_runs": 1})
        detector.from_probe(spec, _fail(error="Timeout 30011ms at 10:00:01"))
        detector.from_probe(spec, _fail(error="Timeout 29855ms at 10:05:44"))
        assert store.count() == 1

    def test_a_genuinely_different_failure_opens_a_new_incident(self, store):
        detector = Detector(store)
        spec = _spec(retry={"confirm_runs": 1})
        detector.from_probe(spec, _fail(failure_kind="assertion"))
        detector.from_probe(spec, _fail(failure_kind="timeout", error="timed out"))
        assert store.count() == 2

    def test_recurrence_adds_evidence(self, store):
        detector = Detector(store)
        spec = _spec(retry={"confirm_runs": 1})
        incident = detector.from_probe(spec, _fail()).incident
        detector.from_probe(
            spec,
            _fail(evidence=[Evidence(kind=EvidenceKind.LOG, summary="more")]),
        )
        assert len(store.get(incident.id).evidence) == 1


class TestRecovery:
    def test_passing_probe_resolves_its_incident(self, store):
        detector = Detector(store)
        spec = _spec(retry={"confirm_runs": 1})
        incident = detector.from_probe(spec, _fail()).incident

        detection = detector.from_probe(spec, _pass())
        assert detection.recovered
        assert store.get(incident.id).state is IncidentState.RESOLVED

    def test_recovery_leaves_a_note(self, store):
        detector = Detector(store)
        spec = _spec(retry={"confirm_runs": 1})
        incident = detector.from_probe(spec, _fail()).incident
        detector.from_probe(spec, _pass())
        summaries = [e.summary for e in store.get(incident.id).evidence]
        assert any("no longer reproduces" in s for s in summaries)

    def test_recovery_does_not_race_an_in_flight_repair(self, store):
        """A mid-repair incident must be left for the repair loop to finish."""
        detector = Detector(store)
        spec = _spec(retry={"confirm_runs": 1})
        incident = detector.from_probe(spec, _fail()).incident
        store.transition(incident, IncidentState.INVESTIGATING)
        store.transition(incident, IncidentState.REPRODUCING)
        store.transition(incident, IncidentState.FIXING)

        detection = detector.from_probe(spec, _pass())
        assert not detection.recovered
        assert store.get(incident.id).state is IncidentState.FIXING

    def test_passing_probe_with_no_incident_is_a_noop(self, store):
        detector = Detector(store)
        detection = detector.from_probe(_spec(), _pass())
        assert not detection.recovered
        assert not detection.opened


class TestSignals:
    def _signal(self, **overrides):
        base = dict(
            source="vercel",
            kind="deployment_failed",
            title="production deployment error",
            detail="Build failed: cannot find module",
            severity=Severity.HIGH,
            component="deployment",
            external_id="dpl_1",
        )
        base.update(overrides)
        return Signal(**base)

    def test_opens_an_incident(self, store):
        detection = Detector(store).from_signal(self._signal())
        assert detection.opened
        assert detection.incident.source == "vercel"
        assert detection.incident.severity is Severity.HIGH

    def test_detail_becomes_untrusted_evidence(self, store):
        incident = Detector(store).from_signal(self._signal()).incident
        assert incident.evidence[0].is_external

    def test_repeat_signal_deduplicates(self, store):
        detector = Detector(store)
        detector.from_signal(self._signal())
        detection = detector.from_signal(self._signal())
        assert detection.recurred
        assert store.count() == 1

    def test_different_deployment_opens_a_new_incident(self, store):
        detector = Detector(store)
        detector.from_signal(self._signal(external_id="dpl_1"))
        detector.from_signal(self._signal(external_id="dpl_2"))
        assert store.count() == 2

    def test_batch(self, store):
        detections = Detector(store).from_signals(
            [self._signal(external_id="a"), self._signal(external_id="b")]
        )
        assert all(d.opened for d in detections)


class TestNotification:
    def test_alerts_on_a_new_incident(self, store):
        sent = []

        class _Notifier:
            def alert(self, incident):
                sent.append(incident.id)
                return True

        detector = Detector(store, notifier=_Notifier())
        detector.from_probe(_spec(retry={"confirm_runs": 1}), _fail())
        assert len(sent) == 1

    def test_does_not_alert_on_a_recurrence(self, store):
        sent = []

        class _Notifier:
            def alert(self, incident):
                sent.append(incident.id)
                return True

        detector = Detector(store, notifier=_Notifier())
        spec = _spec(retry={"confirm_runs": 1})
        detector.from_probe(spec, _fail())
        detector.from_probe(spec, _fail())
        assert len(sent) == 1

    def test_notifier_failure_does_not_break_detection(self, store):
        class _Notifier:
            def alert(self, incident):
                raise RuntimeError("telegram down")

        detector = Detector(store, notifier=_Notifier())
        detection = detector.from_probe(_spec(retry={"confirm_runs": 1}), _fail())
        assert detection.opened
