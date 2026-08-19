"""Tests for post-merge production verification.

The stage this covers exists because everything before it reasons about a
preview deployment of a commit that stops existing the moment the merge lands.
So the tests are built around one question: can a repair reach ``RESOLVED``
without production itself having said so? Every case below is an attempt to
make that happen — a deployment that never arrives, one that arrives for
somebody else's commit, one that fails, a fleet probe that goes red — and the
suite passes only if each of them ends somewhere other than ``RESOLVED``.

Nothing here touches the network, Vercel, or a browser.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from openjarvis.reliability.merge import MergeRecord
from openjarvis.reliability.postmerge import (
    POST_MERGE_FAILURE_KEY,
    PostMergeVerifier,
    post_merge_failure_for,
)
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import (
    Incident,
    IncidentState,
    Severity,
    VerificationResult,
)

MERGE_SHA = "e" * 40
VERIFIED_SHA = "a" * 40
OTHER_SHA = "f" * 40


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Spec:
    """The minimum of a ProbeSpec that this stage touches."""

    def __init__(self, probe_id: str, *, enabled: bool = True):
        self.id = probe_id
        self.enabled = enabled


class _FakeVercel:
    """Serves a scripted sequence of deployment listings, one per poll."""

    def __init__(self, pages: List[List[Dict[str, Any]]]):
        self._pages = list(pages)
        self.calls = 0

    def list_deployments(self, *, limit=20, target="", state=""):
        self.calls += 1
        assert target == "production", "only production deployments may be accepted"
        if not self._pages:
            return []
        page = self._pages[0]
        if len(self._pages) > 1:
            self._pages.pop(0)
        return page


class _FakeVerifier:
    """Passes every probe except those named in *failing*."""

    def __init__(self, *, failing=(), raising=()):
        self._failing = set(failing)
        self._raising = set(raising)
        self.ran: List[tuple] = []

    def verify(self, spec, *, target_url, incident_id=""):
        self.ran.append((spec.id, target_url))
        if spec.id in self._raising:
            raise RuntimeError("the browser died")
        passed = spec.id not in self._failing
        return VerificationResult(
            passed=passed,
            probe_id=spec.id,
            target_url=target_url,
            actual="ok" if passed else "still broken",
        )


class _RecordingNotifier:
    def __init__(self):
        self.events: List[tuple] = []

    def production_deployment(self, incident, *, observation):
        self.events.append(("deployment", observation.deployment_id))
        return True

    def production_verification_started(self, incident, *, observation, target_url=""):
        self.events.append(("verification_started", target_url))
        return True

    def production_verified(self, incident, *, record, result):
        self.events.append(("verified", result.deployment.deployment_id))
        return True

    def post_merge_failed(self, incident, *, record, result):
        self.events.append(("CRITICAL", result.reason))
        return True


def _deployment(**overrides) -> Dict[str, Any]:
    facts = {
        "id": "dpl_ready",
        "state": "READY",
        "target": "production",
        "url": "https://www.example.com",
        "commit_sha": MERGE_SHA,
        "created_at": "2026-08-16T12:00:00+00:00",
    }
    facts.update(overrides)
    return facts


def _record(**overrides) -> MergeRecord:
    record = MergeRecord(
        incident_id="INC-00001",
        pr_number=42,
        verified_head_sha=VERIFIED_SHA,
        merge_commit_sha=MERGE_SHA,
        method="squash",
        merged=True,
    )
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


@pytest.fixture
def store(tmp_path):
    return IncidentStore(tmp_path / "incidents.db")


def _incident(store=None, **overrides) -> Incident:
    fields = dict(
        fingerprint="fp_homepage",
        severity=Severity.HIGH,
        component="website",
        title="Homepage returns 500",
        probe_id="homepage",
    )
    fields.update(overrides)
    incident = Incident(**fields)
    if store is not None:
        incident = store.create(incident)
    return incident


def _verifier(
    *,
    pages=None,
    failing=(),
    raising=(),
    fleet=("homepage", "signup"),
    store=None,
    notifier=None,
    timeout=100.0,
) -> PostMergeVerifier:
    ticks = iter(range(0, 100000, 10))
    return PostMergeVerifier(
        vercel=_FakeVercel(pages if pages is not None else [[_deployment()]]),
        verifier=_FakeVerifier(failing=failing, raising=raising),
        store=store,
        fleet_provider=lambda: [_Spec(p) for p in fleet],
        production_url="https://www.example.com",
        notifier=notifier,
        sleep=lambda _s: None,
        clock=lambda: next(ticks),
        deployment_timeout_seconds=timeout,
        poll_interval_seconds=1.0,
    )


# ---------------------------------------------------------------------------
# Deployment lineage
# ---------------------------------------------------------------------------


class TestDeploymentLineage:
    def test_the_deployment_for_the_merge_sha_is_accepted(self, store):
        incident = _incident(store)
        verifier = _verifier(store=store)
        result = verifier.verify(
            incident, merge_record=_record(), spec=_Spec("homepage")
        )
        assert result.verified
        assert result.deployment.deployment_id == "dpl_ready"
        assert result.deployment.commit_sha == MERGE_SHA

    def test_a_deployment_for_another_commit_is_refused(self, store):
        """The newest production deployment is whatever shipped last. If it is
        not this merge, it says nothing about this repair however green it is."""
        incident = _incident(store)
        verifier = _verifier(
            pages=[[_deployment(id="dpl_someone_else", commit_sha=OTHER_SHA)]],
            timeout=30.0,
        )
        result = verifier.verify(
            incident, merge_record=_record(), spec=_Spec("homepage")
        )
        assert not result.verified
        assert result.rule == "deployment_missing"
        assert not result.deployment.matched

    def test_a_preview_deployment_of_the_merge_commit_is_refused(self, store):
        """Same commit, different build, different domain — not production."""
        incident = _incident(store)
        verifier = _verifier(
            pages=[[_deployment(id="dpl_preview", target="preview")]], timeout=30.0
        )
        result = verifier.verify(
            incident, merge_record=_record(), spec=_Spec("homepage")
        )
        assert not result.verified
        assert result.rule == "deployment_missing"

    def test_a_deployment_that_never_appears_times_out(self, store):
        incident = _incident(store)
        verifier = _verifier(pages=[[]], timeout=50.0)
        result = verifier.verify(
            incident, merge_record=_record(), spec=_Spec("homepage")
        )
        assert not result.verified
        assert result.rule == "deployment_missing"
        assert "within" in result.reason

    def test_a_building_deployment_is_waited_for_then_accepted(self, store):
        incident = _incident(store)
        verifier = _verifier(
            pages=[
                [_deployment(state="BUILDING")],
                [_deployment(state="BUILDING")],
                [_deployment(state="READY")],
            ]
        )
        result = verifier.verify(
            incident, merge_record=_record(), spec=_Spec("homepage")
        )
        assert result.verified
        assert result.deployment.polls >= 3

    @pytest.mark.parametrize("dead", ["ERROR", "CANCELED"])
    def test_a_failed_deployment_fails_immediately(self, store, dead):
        """A terminal state answers now. Burning the timeout to say the same
        thing later would only delay the escalation."""
        incident = _incident(store)
        verifier = _verifier(pages=[[_deployment(state=dead)]], timeout=10_000.0)
        result = verifier.verify(
            incident, merge_record=_record(), spec=_Spec("homepage")
        )
        assert not result.verified
        assert result.rule == "deployment_not_ready"
        assert result.deployment.polls == 1

    def test_a_merge_without_a_sha_cannot_be_matched(self, store):
        incident = _incident(store)
        verifier = _verifier()
        result = verifier.verify(
            incident, merge_record=_record(merge_commit_sha=""), spec=_Spec("homepage")
        )
        assert not result.verified
        assert result.rule == "no_merge_sha"

    def test_an_unreadable_vercel_is_not_a_pass(self, store):
        class _Broken:
            def list_deployments(self, **_kw):
                raise RuntimeError("Vercel is down")

        incident = _incident(store)
        verifier = _verifier(timeout=30.0)
        verifier.vercel = _Broken()
        result = verifier.verify(
            incident, merge_record=_record(), spec=_Spec("homepage")
        )
        assert not result.verified


# ---------------------------------------------------------------------------
# Production probes
# ---------------------------------------------------------------------------


class TestProductionVerification:
    def test_the_original_reproduction_runs_against_production(self, store):
        incident = _incident(store)
        verifier = _verifier(store=store)
        verifier.verify(incident, merge_record=_record(), spec=_Spec("homepage"))
        ran = verifier.verifier.ran
        assert ran[0] == ("homepage", "https://www.example.com")

    def test_a_still_failing_reproduction_refuses(self, store):
        """The probe that opened the incident is the one that must come good."""
        incident = _incident(store)
        verifier = _verifier(failing=("homepage",), store=store)
        result = verifier.verify(
            incident, merge_record=_record(), spec=_Spec("homepage")
        )
        assert not result.verified
        assert result.rule == "reproduction_failed"

    def test_a_failing_fleet_probe_refuses(self, store):
        """A fix that repairs one page and breaks another is not a fix."""
        incident = _incident(store)
        verifier = _verifier(
            failing=("signup",), fleet=("homepage", "signup"), store=store
        )
        result = verifier.verify(
            incident, merge_record=_record(), spec=_Spec("homepage")
        )
        assert not result.verified
        assert result.rule == "fleet_failed"
        assert "signup" in result.reason

    def test_the_reproduction_is_not_run_twice(self, store):
        incident = _incident(store)
        verifier = _verifier(fleet=("homepage", "signup"), store=store)
        verifier.verify(incident, merge_record=_record(), spec=_Spec("homepage"))
        assert [p for p, _ in verifier.verifier.ran].count("homepage") == 1

    def test_disabled_probes_are_not_part_of_the_fleet(self, store):
        incident = _incident(store)
        verifier = _verifier(store=store)
        verifier.fleet_provider = lambda: [
            _Spec("signup"),
            _Spec("legacy", enabled=False),
        ]
        result = verifier.verify(
            incident, merge_record=_record(), spec=_Spec("homepage")
        )
        assert [p.probe_id for p in result.fleet] == ["signup"]

    def test_a_probe_that_cannot_run_counts_as_failed(self, store):
        """Unverifiable is not verified — the rule the whole system rests on."""
        incident = _incident(store)
        verifier = _verifier(raising=("signup",), fleet=("signup",), store=store)
        result = verifier.verify(
            incident, merge_record=_record(), spec=_Spec("homepage")
        )
        assert not result.verified
        assert result.rule == "fleet_failed"

    def test_evidence_and_audit_are_written(self, store):
        incident = _incident(store)
        before = len(store.transitions_for(incident.id))
        verifier = _verifier(store=store)
        verifier.verify(incident, merge_record=_record(), spec=_Spec("homepage"))
        reloaded = store.get(incident.id)
        assert any(e.source == "postmerge" for e in reloaded.evidence)
        assert len(store.transitions_for(incident.id)) > before
        assert store.verify_chain()[0], "the audit chain must stay intact"


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class TestNotifications:
    def test_the_happy_path_announces_deployment_then_verification(self, store):
        notifier = _RecordingNotifier()
        incident = _incident(store)
        verifier = _verifier(store=store, notifier=notifier)
        verifier.verify(incident, merge_record=_record(), spec=_Spec("homepage"))
        kinds = [k for k, _ in notifier.events]
        assert kinds == ["deployment", "verification_started"]

    def test_nothing_is_announced_for_a_deployment_that_never_appeared(self, store):
        notifier = _RecordingNotifier()
        incident = _incident(store)
        verifier = _verifier(pages=[[]], timeout=30.0, store=store, notifier=notifier)
        verifier.verify(incident, merge_record=_record(), spec=_Spec("homepage"))
        assert notifier.events == []


# ---------------------------------------------------------------------------
# The retry-storm guard
# ---------------------------------------------------------------------------


class TestPostMergeFailureMarker:
    def test_no_marker_means_no_block(self, store):
        _incident(store)
        assert post_merge_failure_for(store, "fp_homepage") is None

    def test_a_marked_incident_blocks_its_fingerprint(self, store):
        incident = _incident(store)
        incident.metadata[POST_MERGE_FAILURE_KEY] = {"reason": "production stayed red"}
        store.save(incident)
        store.transition(incident, IncidentState.HUMAN_REQUIRED, reason="post-merge")
        found = post_merge_failure_for(store, "fp_homepage")
        assert found and found["incident_id"] == incident.id

    def test_a_newer_clean_incident_does_not_hide_the_marker(self, store):
        """The failure mode this guard exists for: production stays broken, a
        fresh incident opens with a clean slate, and the newest-incident lookup
        would answer about that one instead."""
        marked = _incident(store)
        marked.metadata[POST_MERGE_FAILURE_KEY] = {"reason": "production stayed red"}
        store.save(marked)
        store.transition(marked, IncidentState.HUMAN_REQUIRED, reason="post-merge")
        _incident(store)  # the recurrence, same fingerprint, no marker
        found = post_merge_failure_for(store, "fp_homepage")
        assert found and found["incident_id"] == marked.id

    def test_resolving_the_escalated_incident_clears_the_block(self, store):
        """A human resolving it is what "clear it" means; the guard must lift."""
        incident = _incident(store)
        incident.metadata[POST_MERGE_FAILURE_KEY] = {"reason": "production stayed red"}
        store.save(incident)
        store.transition(incident, IncidentState.HUMAN_REQUIRED, reason="post-merge")
        store.transition(incident, IncidentState.RESOLVED, reason="human fixed it")
        assert post_merge_failure_for(store, "fp_homepage") is None

    def test_a_store_that_cannot_answer_abstains(self):
        class _Broken:
            def list_by_fingerprint(self, *_a, **_kw):
                raise RuntimeError("database is gone")

        assert post_merge_failure_for(_Broken(), "fp_homepage") is None

    def test_an_older_store_still_answers(self, store):
        """Duck-typed stores without the history query fall back, not crash."""

        class _Old:
            def __init__(self, incident):
                self._incident = incident

            def find_by_fingerprint(self, _fp, **_kw):
                return self._incident

        incident = _incident()
        incident.id = "INC-00009"
        incident.metadata[POST_MERGE_FAILURE_KEY] = {"reason": "red"}
        found = post_merge_failure_for(_Old(incident), "fp_homepage")
        assert found and found["incident_id"] == "INC-00009"


# ---------------------------------------------------------------------------
# Slow versus broken, after the merge
#
# These matter more once auto-merge is on than they did before it. These probes
# run on the operator's laptop; a laptop that is compiling something serves a
# correct page slowly. Without confirmation, a good merge becomes "production
# probe(s) failed", HUMAN_REQUIRED and a CRITICAL notification. Three real
# incidents in one night were exactly that, before anything merged at all.
#
# The line held here: only a duration-budget failure is ever retried. A wrong
# status, a missing element or an unreachable page fails on the first run.
# ---------------------------------------------------------------------------


class _SlowThenFastVerifier:
    """Fails on the clock alone the first *slow_runs* times, then passes."""

    def __init__(self, probe_id: str, slow_runs: int):
        self.probe_id = probe_id
        self.slow_runs = slow_runs
        self.runs: Dict[str, int] = {}

    def verify(self, spec, *, target_url, incident_id=""):
        self.runs[spec.id] = self.runs.get(spec.id, 0) + 1
        if spec.id == self.probe_id and self.runs[spec.id] <= self.slow_runs:
            return VerificationResult(
                passed=False,
                probe_id=spec.id,
                target_url=target_url,
                actual="took 41.30s, over the 30.00s budget",
                failure_kind="slow",
            )
        return VerificationResult(
            passed=True, probe_id=spec.id, target_url=target_url, actual="ok"
        )


def _slow_verifier(inner, *, store=None, notifier=None, fleet=("homepage", "signup")):
    ticks = iter(range(0, 100000, 10))
    return PostMergeVerifier(
        vercel=_FakeVercel([[_deployment()]]),
        verifier=inner,
        store=store,
        fleet_provider=lambda: [_Spec(p) for p in fleet],
        production_url="https://www.example.com",
        notifier=notifier,
        sleep=lambda _s: None,
        clock=lambda: next(ticks),
        deployment_timeout_seconds=100.0,
        poll_interval_seconds=1.0,
        latency_confirmations=2,
        latency_confirmation_delay_seconds=0.0,
    )


class TestLatencyConfirmation:
    def test_a_transient_slow_page_is_confirmed_and_passes(self, store):
        """One slow run, then a good one. Production is verified."""
        inner = _SlowThenFastVerifier("signup", slow_runs=1)
        verifier = _slow_verifier(inner, store=store)
        incident = _incident(store)
        result = verifier.verify(
            incident, merge_record=_record(), spec=_Spec("homepage")
        )

        assert result.verified, result.reason
        assert inner.runs["signup"] == 2
        signup = next(p for p in result.fleet if p.probe_id == "signup")
        assert signup.passed
        assert signup.confirmations == 1

    def test_a_page_that_stays_slow_is_not_verified(self, store):
        """Confirmation bounded: it does not retry until it gets the answer it
        wants."""
        inner = _SlowThenFastVerifier("signup", slow_runs=99)
        verifier = _slow_verifier(inner, store=store)
        result = verifier.verify(
            _incident(store), merge_record=_record(), spec=_Spec("homepage")
        )

        assert not result.verified
        assert inner.runs["signup"] == 3  # first run plus two confirmations
        assert result.rule == "fleet_slow_unconfirmed"

    def test_a_persistent_slow_page_says_so_accurately(self, store):
        """Not "production failed" when every page served correct content."""
        verifier = _slow_verifier(_SlowThenFastVerifier("signup", 99), store=store)
        result = verifier.verify(
            _incident(store), merge_record=_record(), spec=_Spec("homepage")
        )
        assert "served correct content" in result.reason
        assert "monitoring machine being busy" in result.reason

    def test_a_broken_page_is_never_retried(self, store):
        """The line. A site that is down does not get better by being asked
        twice, and retrying a real failure would be weakening the gate."""
        inner = _FakeVerifier(failing=("signup",))
        verifier = _slow_verifier(inner, store=store)
        result = verifier.verify(
            _incident(store), merge_record=_record(), spec=_Spec("homepage")
        )

        assert not result.verified
        assert result.rule == "fleet_failed"
        assert len([r for r in inner.ran if r[0] == "signup"]) == 1

    def test_a_probe_that_worsens_on_retry_is_believed_at_its_worst(self, store):
        class _SlowThenBroken:
            def __init__(self):
                self.runs = 0

            def verify(self, spec, *, target_url, incident_id=""):
                if spec.id != "signup":
                    return VerificationResult(passed=True, probe_id=spec.id)
                self.runs += 1
                if self.runs == 1:
                    return VerificationResult(
                        passed=False,
                        probe_id=spec.id,
                        failure_kind="slow",
                        actual="over budget",
                    )
                return VerificationResult(
                    passed=False,
                    probe_id=spec.id,
                    failure_kind="assertion",
                    actual="the signup form is gone",
                )

        verifier = _slow_verifier(_SlowThenBroken(), store=store)
        result = verifier.verify(
            _incident(store), merge_record=_record(), spec=_Spec("homepage")
        )
        assert not result.verified
        assert result.rule == "fleet_failed"

    def test_the_reproduction_gets_the_same_confirmation(self, store):
        inner = _SlowThenFastVerifier("homepage", slow_runs=1)
        verifier = _slow_verifier(inner, store=store)
        result = verifier.verify(
            _incident(store), merge_record=_record(), spec=_Spec("homepage")
        )
        assert result.verified, result.reason
        assert result.reproduction.confirmations == 1

    def test_a_reproduction_that_stays_slow_names_the_clock(self, store):
        verifier = _slow_verifier(_SlowThenFastVerifier("homepage", 99), store=store)
        result = verifier.verify(
            _incident(store), merge_record=_record(), spec=_Spec("homepage")
        )
        assert not result.verified
        assert result.rule == "reproduction_slow_unconfirmed"


class TestNothingCheckedIsNotVerified:
    """Production cannot be proved by checks that did not run.

    Both cases below used to reach ``verified=True`` — one reporting
    "reproduction and 0 production probe(s) all pass" for a site nothing had
    looked at. That verdict resolves the incident and sends the owner "it's
    fixed", so a vacuous pass here is worse than a crash.
    """

    def test_a_missing_reproduction_refuses(self, store):
        """``spec`` defaults to None the whole way up from ``RepairEngine``."""
        incident = _incident(store)
        verifier = _verifier(store=store, fleet=())
        result = verifier.verify(incident, merge_record=_record(), spec=None)
        assert not result.verified
        assert result.rule == "reproduction_unavailable"

    def test_a_fleet_that_could_not_be_loaded_refuses(self, store):
        """Unknown is not empty: only one of the two is evidence."""

        def _explodes():
            raise RuntimeError("probe registry unavailable")

        incident = _incident(store)
        verifier = _verifier(store=store)
        verifier.fleet_provider = _explodes
        result = verifier.verify(
            incident, merge_record=_record(), spec=_Spec("homepage")
        )
        assert not result.verified
        assert result.rule == "fleet_unavailable"

    def test_a_genuinely_empty_fleet_still_verifies(self, store):
        """A single-probe target is a configuration, not a failure."""
        incident = _incident(store)
        verifier = _verifier(store=store, fleet=())
        result = verifier.verify(
            incident, merge_record=_record(), spec=_Spec("homepage")
        )
        assert result.verified
