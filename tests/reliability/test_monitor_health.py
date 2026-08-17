"""Telling a slow site from a slow observer.

Written from measurement, not intuition. On the machine that produced these
incidents every active probe ran 6-40x inside its duration budget even at load
average 27 — browser probes p95 4.6-5.5s against 30s, HTTP probes p95 0.14-0.46s
against 5-8s. Nine of the first twenty-five incidents nonetheless opened on a
duration overrun at 33-140s. Those were stalls on the observer, and the shape
that gives them away is several unrelated pages going slow at once while every
assertion still passes.
"""

from __future__ import annotations

import pytest

from openjarvis.reliability.detector import LATENCY_EXTRA_CONFIRMATIONS, Detector
from openjarvis.reliability.monitor_health import (
    MonitorHealth,
    latency_only_failure,
)
from openjarvis.reliability.probes.spec import parse_probe
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import ProbeResult


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def store(tmp_path):
    s = IncidentStore(tmp_path / "incidents.db")
    yield s
    s.close()


def _spec(probe_id="homepage", *, severity="critical", confirm_runs=2):
    return parse_probe(
        {
            "probe": {
                "id": probe_id,
                "component": "website",
                "severity": severity,
                "runner": "http",
                "url": "/",
                "retry": {"confirm_runs": confirm_runs},
                "expect": [{"kind": "status", "matches": "200"}],
            }
        }
    )


def _slow(probe_id="homepage"):
    """A page that answered correctly, over its budget."""
    return ProbeResult(
        probe_id=probe_id,
        success=False,
        failure_kind="slow",
        http_status=200,
        steps_completed=1,
        error="took 41.30s, over the 30.00s budget",
        duration_seconds=41.3,
    )


def _broken(probe_id="homepage"):
    """A page that served the wrong thing. Never softened, never retried."""
    return ProbeResult(
        probe_id=probe_id,
        success=False,
        failure_kind="assertion",
        http_status=500,
        steps_completed=1,
        error="expected HTTP 200, got 500",
    )


# ---------------------------------------------------------------------------
# The signal
# ---------------------------------------------------------------------------


def test_one_slow_probe_is_not_enough_to_blame_the_machine():
    """The ambiguous case this exists to resolve must stay ambiguous."""
    health = MonitorHealth()
    health.record("homepage", latency_only=True)
    assert not health.verdict(exclude="homepage").degraded


def test_two_other_slow_probes_do_blame_the_machine():
    health = MonitorHealth()
    for probe in ("sitemap", "api-health"):
        health.record(probe, latency_only=True)
    verdict = health.verdict(exclude="homepage")
    assert verdict.degraded
    assert "sitemap" in verdict.reason and "api-health" in verdict.reason


def test_a_probe_cannot_corroborate_itself():
    """Otherwise one slow page becomes its own excuse."""
    health = MonitorHealth(corroboration=1)
    health.record("homepage", latency_only=True)
    assert not health.verdict(exclude="homepage").degraded
    assert health.verdict(exclude="other").degraded


def test_recovering_clears_a_probe_from_the_evidence():
    health = MonitorHealth()
    for probe in ("sitemap", "api-health"):
        health.record(probe, latency_only=True)
    health.record("sitemap", latency_only=False)
    assert not health.verdict(exclude="homepage").degraded


def test_a_probe_serving_the_wrong_thing_is_not_evidence_about_the_clock():
    health = MonitorHealth()
    health.record("sitemap", latency_only=True)
    health.record("api-health", latency_only=False)  # broken, not slow
    assert not health.verdict(exclude="homepage").degraded


def test_stale_observations_expire():
    """A stall three minutes ago does not excuse a regression now."""
    clock = _Clock()
    health = MonitorHealth(window_seconds=180.0, clock=clock)
    for probe in ("sitemap", "api-health"):
        health.record(probe, latency_only=True)
    assert health.verdict(exclude="homepage").degraded
    clock.advance(181)
    assert not health.verdict(exclude="homepage").degraded


def test_latency_only_failure_reads_the_probe_result():
    assert latency_only_failure(_slow())
    assert not latency_only_failure(_broken())
    assert not latency_only_failure(ProbeResult(probe_id="p", success=True))


def test_snapshot_reports_what_it_is_holding():
    health = MonitorHealth()
    health.record("sitemap", latency_only=True)
    snap = health.snapshot()
    assert snap["slow_probes"] == ["sitemap"]
    assert snap["corroboration_required"] == 2


# ---------------------------------------------------------------------------
# What the detector does with it
# ---------------------------------------------------------------------------


def test_a_corroborated_stall_opens_no_incident(store):
    """INC-00020/21/22, prevented at the source."""
    health = MonitorHealth()
    for probe in ("sitemap", "api-health"):
        health.record(probe, latency_only=True)
    detector = Detector(store, monitor_health=health)

    detection = detector.from_probe(_spec(), _slow())
    assert detection.suppressed
    assert detection.incident is None
    assert "monitoring host" in detection.reason


def test_an_uncorroborated_slow_page_still_needs_extra_confirmation(store):
    """Suppression is not the only guard: a lone slow page is confirmed harder."""
    detector = Detector(store, monitor_health=MonitorHealth())
    spec = _spec(confirm_runs=2)

    for _ in range(2 + LATENCY_EXTRA_CONFIRMATIONS - 1):
        detection = detector.from_probe(spec, _slow())
        assert detection.suppressed, detection.reason
        assert "time budget" in detection.reason

    final = detector.from_probe(spec, _slow())
    assert final.incident is not None


def test_a_broken_page_is_never_suppressed_by_a_busy_machine(store):
    """The line that must not move.

    Every corroborating signal in the world says this laptop is struggling. The
    page is returning 500. That is an outage and it opens on schedule.
    """
    health = MonitorHealth()
    for probe in ("sitemap", "api-health", "login", "signup"):
        health.record(probe, latency_only=True)
    detector = Detector(store, monitor_health=health)
    spec = _spec(confirm_runs=2)

    first = detector.from_probe(spec, _broken())
    assert first.suppressed  # only because confirm_runs = 2
    assert "time budget" not in first.reason

    second = detector.from_probe(spec, _broken())
    assert second.incident is not None
    assert second.incident.severity.value == "CRITICAL"


def test_without_monitor_health_behaviour_is_unchanged(store):
    """The wiring is optional and its absence is never less safe."""
    detector = Detector(store)
    spec = _spec(confirm_runs=1)
    assert detector.from_probe(spec, _broken()).incident is not None
