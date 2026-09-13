"""Tests for the repair loop, policy gate and independent verification.

The single most important test in this suite is
``test_agent_claims_success_but_verification_fails``: it is the whole
architectural point of JARVIS.
"""

from __future__ import annotations

import pytest

from openjarvis.core.proclock import LeaseTimeout
from openjarvis.reliability.code_agent import CodeAgentResult, FakeCodeAgent
from openjarvis.reliability.policy import SafetyPolicy
from openjarvis.reliability.probes.spec import parse_probe
from openjarvis.reliability.repair import (
    OUTCOME_NO_DIFF,
    OUTCOME_TESTS_FAILED,
    OUTCOME_VERIFICATION_FAILED,
    OUTCOME_VERIFIED,
    RepairLoop,
    run_tests,
)
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import (
    Incident,
    IncidentState,
    ProbeResult,
    Severity,
    VerificationResult,
)
from openjarvis.reliability.verify import Verifier

# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------


class _ScriptedExecutor:
    """Stands in for a ProbeExecutor: returns scripted probe outcomes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def run(self, spec):
        self.calls += 1
        success = self.outcomes.pop(0) if self.outcomes else False
        return ProbeResult(
            probe_id=spec.id,
            success=success,
            failure_kind="" if success else "assertion",
            error="" if success else "expected the URL to match /dashboard, got /login",
            final_url="https://preview.example/login",
            steps_completed=4,
        )


def _verifier(outcomes):
    executor = _ScriptedExecutor(outcomes)
    verifier = Verifier(executor_factory=lambda _url: executor)
    verifier.executor = executor  # exposed for assertions
    return verifier


def _spec():
    return parse_probe(
        {
            "probe": {
                "id": "auth-login",
                "component": "authentication",
                "severity": "high",
                "steps": [{"action": "goto", "url": "/login"}],
                "expect": [{"kind": "url", "matches": "/dashboard"}],
            }
        }
    )


@pytest.fixture
def store(tmp_path):
    s = IncidentStore(tmp_path / "incidents.db")
    yield s
    s.close()


@pytest.fixture
def incident(store):
    return store.create(
        Incident(
            fingerprint="fp_x",
            severity=Severity.HIGH,
            component="authentication",
            title="Login redirects back to /login",
            probe_id="auth-login",
            repro_steps=["Open /login", "Click Sign In"],
        )
    )


def _policy(**overrides) -> SafetyPolicy:
    defaults = dict(repair_enabled=True, max_attempts=3)
    defaults.update(overrides)
    return SafetyPolicy(**defaults)


def _loop(store, agent, *, verifier, policy=None, **kwargs) -> RepairLoop:
    return RepairLoop(
        agent=agent,
        policy=policy or _policy(),
        verifier=verifier,
        store=store,
        workspace=".",
        preview_lookup=kwargs.pop(
            "preview_lookup", lambda _b: "https://preview.example"
        ),
        sleep=lambda _s: None,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Test-suite runner
# ---------------------------------------------------------------------------


class TestRunTests:
    def test_no_command_is_not_a_pass(self, tmp_path):
        """ "Did not run" and "passed" must never be conflated."""
        result = run_tests("", workspace=str(tmp_path))
        assert not result.ran
        assert not result.passed

    def test_success(self, tmp_path):
        result = run_tests("exit 0", workspace=str(tmp_path))
        assert result.ran and result.passed

    def test_failure_captures_output(self, tmp_path):
        result = run_tests("echo boom >&2; exit 1", workspace=str(tmp_path))
        assert result.ran and not result.passed
        assert "boom" in result.output


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class TestVerifier:
    def test_pass(self):
        result = _verifier([True]).verify(_spec(), target_url="https://preview")
        assert result.passed
        assert "every expectation held" in result.actual

    def test_fail(self):
        result = _verifier([False]).verify(_spec(), target_url="https://preview")
        assert not result.passed
        assert "/dashboard" in result.expected
        assert "still reproduces" in result.notes

    def test_no_target_is_a_failure_not_an_error(self):
        """An unverifiable repair must never count as verified."""
        result = _verifier([True]).verify(_spec(), target_url="")
        assert not result.passed
        assert "no deployment was available" in result.actual

    def test_runner_exception_is_a_failure(self):
        class _Boom:
            def run(self, spec):
                raise RuntimeError("kaboom")

        verifier = Verifier(executor_factory=lambda _u: _Boom())
        result = verifier.verify(_spec(), target_url="https://preview")
        assert not result.passed
        assert "kaboom" in result.actual

    def test_summarize_for_retry(self):
        result = _verifier([False]).verify(_spec(), target_url="https://preview")
        summary = Verifier.summarize_for_retry(result)
        assert "Expected:" in summary
        assert "Observed:" in summary

    def test_summarize_none(self):
        assert Verifier.summarize_for_retry(None) == ""


# ---------------------------------------------------------------------------
# The repair loop
# ---------------------------------------------------------------------------


class TestRepairLoop:
    def test_happy_path(self, store, incident):
        agent = FakeCodeAgent(
            [
                CodeAgentResult(
                    claim="Fixed the redirect.", changed_files=["app/auth.ts"]
                )
            ]
        )
        loop = _loop(store, agent, verifier=_verifier([True]))
        outcome = loop.run(incident, _spec())

        assert outcome.resolved
        assert outcome.attempts == 1
        assert incident.state is IncidentState.RESOLVED
        assert incident.attempts[0].outcome == OUTCOME_VERIFIED
        assert incident.attempts[0].verified

    def test_agent_claims_success_but_verification_fails(self, store, incident):
        """THE test. The agent is confident and wrong; JARVIS must not believe it.

        Three attempts, each claiming success, each failing verification, and the
        incident must end at HUMAN_REQUIRED rather than RESOLVED.
        """
        agent = FakeCodeAgent(
            [
                CodeAgentResult(
                    claim="Fixed! The login redirect now works correctly.",
                    changed_files=["app/auth.ts"],
                )
            ]
        )
        loop = _loop(store, agent, verifier=_verifier([False, False, False]))
        outcome = loop.run(incident, _spec())

        assert not outcome.resolved
        assert outcome.attempts == 3
        assert incident.state is IncidentState.HUMAN_REQUIRED
        assert incident.state is not IncidentState.RESOLVED
        for attempt in incident.attempts:
            assert attempt.claim.startswith("Fixed!")
            assert attempt.outcome == OUTCOME_VERIFICATION_FAILED
            assert not attempt.verified

    def test_succeeds_on_the_second_attempt(self, store, incident):
        agent = FakeCodeAgent([CodeAgentResult(claim="try 1", changed_files=["a.ts"])])
        loop = _loop(store, agent, verifier=_verifier([False, True]))
        outcome = loop.run(incident, _spec())

        assert outcome.resolved
        assert outcome.attempts == 2
        assert incident.attempts[0].outcome == OUTCOME_VERIFICATION_FAILED
        assert incident.attempts[1].outcome == OUTCOME_VERIFIED

    def test_failed_verification_is_fed_back_to_the_next_attempt(self, store, incident):
        """A retry must be a *better* attempt, not the same one again."""
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(store, agent, verifier=_verifier([False, True]))
        loop.run(incident, _spec())

        assert len(agent.calls) == 2
        assert "Previous attempt failed verification" not in agent.calls[0]
        assert "Previous attempt failed verification" in agent.calls[1]
        assert "/dashboard" in agent.calls[1]

    def test_no_diff_is_a_failed_attempt(self, store, incident):
        """A confident claim with no changes is not a fix."""
        agent = FakeCodeAgent([CodeAgentResult(claim="All good!", changed_files=[])])
        loop = _loop(store, agent, verifier=_verifier([True, True, True]))
        outcome = loop.run(incident, _spec())

        assert not outcome.resolved
        assert incident.state is IncidentState.HUMAN_REQUIRED
        assert all(a.outcome == OUTCOME_NO_DIFF for a in incident.attempts)

    def test_failing_tests_stop_the_attempt_before_verification(self, store, incident):
        verifier = _verifier([True, True, True])
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(store, agent, verifier=verifier, test_command="exit 1")
        outcome = loop.run(incident, _spec())

        assert not outcome.resolved
        assert all(a.outcome == OUTCOME_TESTS_FAILED for a in incident.attempts)
        assert verifier.executor.calls == 0  # never got as far as verifying

    def test_passing_tests_proceed_to_verification(self, store, incident):
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(store, agent, verifier=_verifier([True]), test_command="exit 0")
        outcome = loop.run(incident, _spec())

        assert outcome.resolved
        assert incident.attempts[0].tests_passed is True

    def test_no_preview_means_not_verified(self, store, incident):
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True, True, True]),
            preview_lookup=lambda _b: "",
        )
        outcome = loop.run(incident, _spec())

        assert not outcome.resolved
        assert incident.state is IncidentState.HUMAN_REQUIRED

    def test_protected_path_stops_the_loop(self, store, incident):
        agent = FakeCodeAgent(
            [CodeAgentResult(claim="c", changed_files=[".github/workflows/ci.yml"])]
        )
        loop = _loop(store, agent, verifier=_verifier([True]))
        outcome = loop.run(incident, _spec())

        assert not outcome.resolved
        assert "protected path" in outcome.reason
        assert incident.state is IncidentState.HUMAN_REQUIRED

    def test_agent_error_is_recorded_and_retried(self, store, incident):
        agent = FakeCodeAgent(
            [
                CodeAgentResult(succeeded=False, error="crashed"),
                CodeAgentResult(claim="ok", changed_files=["a.ts"]),
            ]
        )
        loop = _loop(store, agent, verifier=_verifier([True]))
        outcome = loop.run(incident, _spec())

        assert outcome.resolved
        assert outcome.attempts == 2

    def test_attempts_are_capped(self, store, incident):
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        policy = _policy(max_attempts=2)
        loop = _loop(store, agent, verifier=_verifier([False] * 10), policy=policy)
        outcome = loop.run(incident, _spec())

        assert outcome.attempts == 2
        assert len(agent.calls) == 2
        assert "2 repair attempts" in outcome.reason

    def test_repair_disabled_is_refused_without_waking_anybody(self, store, incident):
        """Repair being switched off is a setting, not an emergency.

        This used to escalate to HUMAN_REQUIRED, which paged the owner for a
        decision they had already made. The incident stays open and watched
        instead, and closes by itself if the check recovers.
        """
        agent = FakeCodeAgent()
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            policy=_policy(repair_enabled=False),
        )
        outcome = loop.run(incident, _spec())

        assert not outcome.resolved
        assert agent.calls == []
        assert incident.state is not IncidentState.HUMAN_REQUIRED
        assert "disabled" in outcome.reason

    def test_severity_outside_the_allowlist_is_refused(self, store):
        critical = store.create(
            Incident(
                fingerprint="fp_c",
                severity=Severity.CRITICAL,
                component="auth",
                title="down",
            )
        )
        agent = FakeCodeAgent()
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            policy=_policy(auto_repair_severities=["MEDIUM"]),
        )
        outcome = loop.run(critical, _spec())

        assert not outcome.resolved
        assert agent.calls == []
        assert "not in the auto-repair list" in outcome.reason

    def test_state_history_records_the_whole_journey(self, store, incident):
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(store, agent, verifier=_verifier([True]), test_command="exit 0")
        loop.run(incident, _spec())

        states = [t.to_state.value for t in store.transitions_for(incident.id)]
        assert "FIXING" in states
        assert "TESTING" in states
        assert "VERIFYING" in states
        assert states[-1] == "RESOLVED"
        assert store.verify_chain() == (True, None)

    def test_pull_request_is_opened_on_success(self, store, incident):
        class _GitHub:
            base_branch = "main"

            def __init__(self):
                self.pull_requests = []

            def branch_name_for(self, incident_id):
                return f"jarvis/incident-{incident_id}"

            def create_branch(self, branch, **kwargs):
                return "sha"

            def create_pull_request(self, **kwargs):
                self.pull_requests.append(kwargs)
                return {"number": 1, "url": "https://github.com/x/y/pull/1"}

        github = _GitHub()
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(store, agent, verifier=_verifier([True]), github=github)
        outcome = loop.run(incident, _spec())

        assert outcome.pull_request_url.endswith("/pull/1")
        body = github.pull_requests[0]["body"]
        assert "Verified: **yes**" in body
        assert "does not rely on the coding agent's own account" in body


# ---------------------------------------------------------------------------
# Post-merge production verification
# ---------------------------------------------------------------------------


class _MergingGitHub:
    """A GitHub double whose pull request always merges."""

    base_branch = "main"

    def __init__(self):
        self.pull_requests = []

    def branch_name_for(self, incident_id):
        return f"jarvis/incident-{incident_id}"

    def create_branch(self, branch, **kwargs):
        return "sha"

    def create_pull_request(self, **kwargs):
        self.pull_requests.append(kwargs)
        return {"number": 7, "url": "https://github.com/x/y/pull/7"}


class _FakeMerger:
    """Stands in for AutoMerger: reports a merge without performing one."""

    def __init__(self, *, merged=True, merge_sha="e" * 40):
        from openjarvis.reliability.merge import MergeRecord

        self.merged = merged
        self._record = MergeRecord(
            pr_number=7,
            verified_head_sha="a" * 40,
            merge_commit_sha=merge_sha if merged else "",
            method="squash",
            merged=merged,
        )
        self.calls = 0

    def merge_for(self, incident):
        self.calls += 1
        self._record.incident_id = incident.id
        return self._record


class _FakePostMerge:
    """Returns a scripted production verdict."""

    def __init__(self, *, verified=True, reason="production verified", rule="verified"):
        from openjarvis.reliability.postmerge import (
            DeploymentObservation,
            PostMergeResult,
        )

        self.result = PostMergeResult(
            verified=verified,
            reason=reason,
            rule=rule,
            deployment=DeploymentObservation(
                matched=True, ready=verified, deployment_id="dpl_1", state="READY"
            ),
        )
        self.calls = 0

    def verify(self, incident, *, merge_record, spec=None):
        self.calls += 1
        return self.result


class _PostMergeNotifier:
    def __init__(self):
        self.critical = []
        self.verified = []

    def __getattr__(self, _name):
        # Every other notification is irrelevant here and must not blow up.
        return lambda *a, **kw: True

    def post_merge_failed(self, incident, *, record, result):
        self.critical.append((incident.id, result.reason))
        return True

    def production_verified(self, incident, *, record, result):
        self.verified.append(incident.id)
        return True


def _states(store, incident_id):
    return [t.to_state.value for t in store.transitions_for(incident_id)]


class TestPostMergeStateFlow:
    """Where RESOLVED happens, and what it now costs to get there."""

    def test_without_a_merger_the_pull_request_flow_is_untouched(self, store, incident):
        """Requirement: auto-merge disabled preserves the old behaviour exactly."""
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(store, agent, verifier=_verifier([True]), github=_MergingGitHub())
        outcome = loop.run(incident, _spec())

        assert outcome.resolved
        assert outcome.final_state is IncidentState.RESOLVED
        assert "MERGED" not in _states(store, incident.id)

    def test_a_merged_repair_is_not_resolved_before_production_is_proved(
        self, store, incident
    ):
        """The defect this whole stage exists for: RESOLVED used to be recorded
        before the merge was even attempted."""
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        merger = _FakeMerger()
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            auto_merger=merger,
            post_merge_verifier=_FakePostMerge(verified=True),
        )
        loop.run(incident, _spec())

        states = _states(store, incident.id)
        assert "MERGED" in states
        assert states.index("MERGED") < states.index("RESOLVED"), (
            "RESOLVED must come after MERGED, never before it"
        )

    def test_production_verified_resolves(self, store, incident):
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        notifier = _PostMergeNotifier()
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            auto_merger=_FakeMerger(),
            post_merge_verifier=_FakePostMerge(verified=True),
            notifier=notifier,
        )
        outcome = loop.run(incident, _spec())

        assert outcome.resolved
        assert outcome.final_state is IncidentState.RESOLVED
        assert notifier.verified == [incident.id]
        assert not notifier.critical

    @pytest.mark.parametrize(
        "rule,reason",
        [
            ("deployment_missing", "no production deployment appeared"),
            ("deployment_not_ready", "the production deployment ended in ERROR"),
            ("reproduction_failed", "the original probe still fails in production"),
            ("fleet_failed", "production probe signup failed after the merge"),
        ],
    )
    def test_any_production_failure_escalates(self, store, incident, rule, reason):
        """Deployment missing, dead, reproduction red, fleet red — one answer."""
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        notifier = _PostMergeNotifier()
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            auto_merger=_FakeMerger(),
            post_merge_verifier=_FakePostMerge(
                verified=False, reason=reason, rule=rule
            ),
            notifier=notifier,
        )
        outcome = loop.run(incident, _spec())

        assert not outcome.resolved
        reloaded = store.get(incident.id)
        assert reloaded.state is IncidentState.HUMAN_REQUIRED
        assert "RESOLVED" not in _states(store, incident.id)
        assert notifier.critical, "a post-merge failure must send CRITICAL"

    def test_a_merge_with_no_verifier_configured_escalates(self, store, incident):
        """Merging without production verification is not something to fake."""
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            auto_merger=_FakeMerger(),
            post_merge_verifier=None,
        )
        outcome = loop.run(incident, _spec())

        assert not outcome.resolved
        assert store.get(incident.id).state is IncidentState.HUMAN_REQUIRED

    def test_a_verifier_that_raises_escalates(self, store, incident):
        class _Exploding:
            def verify(self, *_a, **_kw):
                raise RuntimeError("the verifier died")

        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            auto_merger=_FakeMerger(),
            post_merge_verifier=_Exploding(),
        )
        outcome = loop.run(incident, _spec())

        assert not outcome.resolved
        assert store.get(incident.id).state is IncidentState.HUMAN_REQUIRED

    def test_a_refused_merge_still_resolves_on_the_pull_request(self, store, incident):
        """The gates refusing is the system working, not a failed repair."""
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            auto_merger=_FakeMerger(merged=False),
            post_merge_verifier=_FakePostMerge(verified=True),
        )
        outcome = loop.run(incident, _spec())

        assert outcome.resolved
        assert "MERGED" not in _states(store, incident.id)

    def test_the_agents_claim_never_reaches_the_merge(self, store, incident):
        """Verification fails, the agent insists it worked: no merge is attempted
        and production verification never runs."""
        agent = FakeCodeAgent(
            [
                CodeAgentResult(claim="Fixed! All tests pass.", changed_files=["a.ts"])
                for _ in range(3)
            ]
        )
        merger = _FakeMerger()
        post = _FakePostMerge()
        loop = _loop(
            store,
            agent,
            verifier=_verifier([False, False, False]),
            github=_MergingGitHub(),
            auto_merger=merger,
            post_merge_verifier=post,
        )
        outcome = loop.run(incident, _spec())

        assert not outcome.resolved
        assert merger.calls == 0
        assert post.calls == 0

    def test_the_audit_chain_survives_a_post_merge_failure(self, store, incident):
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            auto_merger=_FakeMerger(),
            post_merge_verifier=_FakePostMerge(
                verified=False, reason="production stayed red", rule="fleet_failed"
            ),
            notifier=_PostMergeNotifier(),
        )
        loop.run(incident, _spec())

        intact, bad_row = store.verify_chain()
        assert intact, f"audit chain broken at row {bad_row}"


class TestWhatReachesTheOwner:
    """End to end, through the real router: how many messages does a repair send?

    The unit tests prove each event is silent; these prove the whole run is. A
    policy that suppresses six notifications individually and still sends four
    from somewhere else has not achieved anything.
    """

    def _router(self):
        from openjarvis.reliability.notify import ConsoleNotifier, NotificationRouter
        from openjarvis.reliability.types import Severity

        notifier = ConsoleNotifier()
        return (
            NotificationRouter(
                notifier=notifier,
                min_severity=Severity.LOW,
                dedup_window_seconds=0.0,
            ),
            notifier,
        )

    def test_a_successful_pr_repair_sends_exactly_one_message(self, store, incident):
        router, sent = self._router()
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            notifier=router,
        )
        loop.run(incident, _spec())

        assert len(sent.sent) == 1, f"expected one message, got:\n{sent.sent}"
        assert sent.sent[0].startswith("Sir, I fixed the issue.")

    def test_a_failed_repair_sends_exactly_one_message(self, store, incident):
        """Three attempts, three failures — one escalation, not three updates."""
        router, sent = self._router()
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(
            store,
            agent,
            verifier=_verifier([False, False, False]),
            github=_MergingGitHub(),
            notifier=router,
        )
        loop.run(incident, _spec())

        assert len(sent.sent) == 1, f"expected one message, got:\n{sent.sent}"
        assert sent.sent[0].startswith("Sir, I need your help.")

    def test_a_live_repair_sends_exactly_one_message(self, store, incident):
        router, sent = self._router()
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            auto_merger=_FakeMerger(),
            post_merge_verifier=_FakePostMerge(verified=True),
            notifier=router,
        )
        loop.run(incident, _spec())

        assert len(sent.sent) == 1, f"expected one message, got:\n{sent.sent}"
        assert sent.sent[0].startswith("Sir, it's fixed.")

    def test_a_post_merge_failure_sends_exactly_one_critical_message(
        self, store, incident
    ):
        """It must arrive, and it must arrive once. The escalation and the
        post-merge alert are the same bad news; the owner hears it one way."""
        router, sent = self._router()
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            auto_merger=_FakeMerger(),
            post_merge_verifier=_FakePostMerge(
                verified=False, reason="production stayed red", rule="fleet_failed"
            ),
            notifier=router,
        )
        loop.run(incident, _spec())

        assert len(sent.sent) == 1, f"expected one message, got:\n{sent.sent}"
        assert sent.sent[0].startswith("Sir, I need your help.")
        assert "I stopped making changes." in sent.sent[0]

    def test_no_message_mentions_the_bot_or_internal_states(self, store, incident):
        router, sent = self._router()
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            notifier=router,
        )
        loop.run(incident, _spec())

        for message in sent.sent:
            assert message.startswith("Sir,")
            assert not message.startswith("JARVIS")
            for jargon in ("RESOLVED", "VERIFYING", "fingerprint", "probe"):
                assert jargon not in message


class TestRetryStormGuard:
    """A merge that broke production must not be answered with another merge."""

    def _fail_once(self, store, incident):
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            auto_merger=_FakeMerger(),
            post_merge_verifier=_FakePostMerge(
                verified=False, reason="production stayed red", rule="fleet_failed"
            ),
            notifier=_PostMergeNotifier(),
        )
        loop.run(incident, _spec())

    def test_the_marker_is_written_durably(self, store, incident):
        from openjarvis.reliability.postmerge import POST_MERGE_FAILURE_KEY

        self._fail_once(store, incident)
        reloaded = store.get(incident.id)
        marker = reloaded.metadata.get(POST_MERGE_FAILURE_KEY)
        assert marker and marker["reason"].startswith("production stayed red")

    def test_a_fresh_incident_for_the_same_fingerprint_is_not_repaired(
        self, store, incident
    ):
        """Production is still broken, so the probe opens a new incident with a
        clean attempt count. It must not be repaired, let alone merged."""
        self._fail_once(store, incident)

        recurrence = store.create(
            Incident(
                fingerprint="fp_x",
                severity=Severity.HIGH,
                component="authentication",
                title="Login redirects back to /login",
                probe_id="auth-login",
            )
        )
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        merger = _FakeMerger()
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            auto_merger=merger,
            post_merge_verifier=_FakePostMerge(verified=True),
        )
        outcome = loop.run(recurrence, _spec())

        assert not outcome.resolved
        assert merger.calls == 0, "no second merge for a fingerprint that broke prod"
        assert agent.calls == [], "the coding agent must not even be started"
        assert store.get(recurrence.id).state is IncidentState.HUMAN_REQUIRED

    def test_clearing_the_escalated_incident_lifts_the_block(self, store, incident):
        self._fail_once(store, incident)
        failed = store.get(incident.id)
        store.transition(failed, IncidentState.RESOLVED, reason="human dealt with it")

        recurrence = store.create(
            Incident(
                fingerprint="fp_x",
                severity=Severity.HIGH,
                component="authentication",
                title="Login redirects back to /login",
                probe_id="auth-login",
            )
        )
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        merger = _FakeMerger()
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            auto_merger=merger,
            post_merge_verifier=_FakePostMerge(verified=True),
        )
        outcome = loop.run(recurrence, _spec())

        assert outcome.resolved
        assert merger.calls == 1


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class TestSafetyPolicy:
    def _incident(self, severity=Severity.HIGH, attempts=0):
        incident = Incident(
            fingerprint="f", severity=severity, component="c", title="t", id="INC-1"
        )
        from openjarvis.reliability.types import RepairAttempt

        for index in range(attempts):
            incident.add_attempt(RepairAttempt(number=index + 1))
        return incident

    def test_repair_disabled_by_default(self):
        assert not SafetyPolicy().may_attempt_repair(self._incident())

    def test_attempts_exhausted(self):
        policy = _policy(max_attempts=2)
        assert not policy.may_attempt_repair(self._incident(attempts=2))

    def test_severity_gate(self):
        policy = _policy(auto_repair_severities=["MEDIUM"])
        assert not policy.may_attempt_repair(self._incident(Severity.HIGH))
        assert policy.may_attempt_repair(self._incident(Severity.MEDIUM))

    def test_deploy_defaults_to_pr_only(self):
        policy = _policy()
        decision = policy.may_deploy(self._incident(), VerificationResult(passed=True))
        assert not decision
        assert decision.rule == "pr_only"

    def test_deploy_never(self):
        policy = _policy(deploy_mode="never")
        assert not policy.may_deploy(self._incident(), VerificationResult(passed=True))

    def test_deploy_requires_verification(self):
        policy = _policy(
            deploy_mode="auto_deploy_allowlisted", auto_deploy_fix_classes=["dep_bump"]
        )
        decision = policy.may_deploy(self._incident(), None, fix_class="dep_bump")
        assert not decision
        assert decision.rule == "unverified"

    def test_deploy_refuses_failed_verification(self):
        policy = _policy(
            deploy_mode="auto_deploy_allowlisted", auto_deploy_fix_classes=["dep_bump"]
        )
        decision = policy.may_deploy(
            self._incident(), VerificationResult(passed=False), fix_class="dep_bump"
        )
        assert decision.rule == "unverified"

    def test_critical_is_never_auto_deployed(self):
        """No allowlist entry overrides this."""
        policy = _policy(
            deploy_mode="auto_deploy_allowlisted",
            auto_deploy_fix_classes=["dep_bump"],
        )
        decision = policy.may_deploy(
            self._incident(Severity.CRITICAL),
            VerificationResult(passed=True),
            fix_class="dep_bump",
        )
        assert not decision
        assert decision.rule == "critical_never_auto"

    def test_empty_allowlist_blocks_everything(self):
        policy = _policy(deploy_mode="auto_deploy_allowlisted")
        decision = policy.may_deploy(
            self._incident(), VerificationResult(passed=True), fix_class="dep_bump"
        )
        assert decision.rule == "no_allowlist"

    def test_unlisted_fix_class_is_blocked(self):
        policy = _policy(
            deploy_mode="auto_deploy_allowlisted", auto_deploy_fix_classes=["dep_bump"]
        )
        decision = policy.may_deploy(
            self._incident(), VerificationResult(passed=True), fix_class="rewrite_auth"
        )
        assert decision.rule == "class_not_allowlisted"

    @pytest.mark.parametrize(
        "path",
        [
            "app/auth/session.ts",
            "middleware.ts",
            "supabase/rls_policies.sql",
            "package-lock.json",
            "pyproject.toml",
        ],
    )
    def test_security_sensitive_paths_block_auto_deploy(self, path):
        policy = _policy(
            deploy_mode="auto_deploy_allowlisted", auto_deploy_fix_classes=["dep_bump"]
        )
        decision = policy.may_deploy(
            self._incident(),
            VerificationResult(passed=True),
            fix_class="dep_bump",
            changed_paths=[path],
        )
        assert not decision
        assert decision.rule in ("security_sensitive", "protected_path")

    def test_ordinary_path_allows_auto_deploy(self):
        policy = _policy(
            deploy_mode="auto_deploy_allowlisted", auto_deploy_fix_classes=["dep_bump"]
        )
        decision = policy.may_deploy(
            self._incident(),
            VerificationResult(passed=True),
            fix_class="dep_bump",
            changed_paths=["app/components/Button.tsx"],
        )
        assert decision

    def test_default_branch_push_is_refused(self):
        assert not SafetyPolicy().may_push_to("main", "main")

    def test_feature_branch_push_is_fine(self):
        assert SafetyPolicy().may_push_to("jarvis/incident-1", "main")

    def test_default_branch_push_with_override(self):
        policy = SafetyPolicy(allow_push_to_default_branch=True)
        assert policy.may_push_to("main", "main")


# ---------------------------------------------------------------------------
# Evidence before escalation
#
# Every one of the first nine escalations in this system's history was for a
# fault that had already cleared by the time anybody could have looked at it.
# Waking somebody for a problem that no longer exists is the most expensive
# false alarm there is, because it teaches them the next one is probably
# nothing too.
#
# The rule is asymmetric on purpose: only a probe that *passes* against
# production can prevent an escalation. No URL, no spec, a crash, or any
# unreadable verdict all escalate.
# ---------------------------------------------------------------------------


class TestRecheckBeforeEscalating:
    def test_a_fault_that_cleared_is_closed_not_escalated(self, store, incident):
        """Keyed on the target URL, not on call order.

        What matters is *where* the probe passed: a preview that never came good
        and a production site that did. Scripting by position would pass for the
        wrong reason the moment the loop changed how many times it verifies.
        """

        class _PreviewFailsProductionPasses:
            def __init__(self):
                self.targets = []

            def verify(self, spec, *, target_url, incident_id=""):
                self.targets.append(target_url)
                production = target_url == "https://www.example.com"
                return VerificationResult(
                    passed=production,
                    probe_id=spec.id,
                    target_url=target_url,
                    actual="the probe passes here" if production else "still broken",
                )

            @staticmethod
            def summarize_for_retry(v):
                return ""

        verifier = _PreviewFailsProductionPasses()
        loop = _loop(
            store,
            # Real changes, so the loop reaches VERIFYING — the only state from
            # which RESOLVED is legal, which is the point of the guard.
            FakeCodeAgent(
                [CodeAgentResult(claim="Fixed it.", changed_files=["app/auth.ts"])]
            ),
            verifier=verifier,
            production_url="https://www.example.com",
        )
        outcome = loop.run(incident, _spec())

        assert incident.state is IncidentState.RESOLVED
        assert not outcome.resolved  # JARVIS did not fix it; it recovered
        assert "https://www.example.com" in verifier.targets
        notes = " ".join(e.summary or "" for e in store.get(incident.id).evidence)
        assert "Recovered before escalation" in notes

    def test_it_never_widens_the_path_to_resolved(self, store, incident):
        """The structural guarantee is not traded for this convenience.

        RESOLVED is reachable autonomously only through VERIFYING — that is what
        makes "never trust the agent's claim that it fixed something" a property
        of the state machine rather than of a code path. An agent that produces
        no diff never reaches VERIFYING, so even with production demonstrably
        healthy this escalates rather than closing.
        """

        class _AlwaysPasses:
            def verify(self, spec, *, target_url, incident_id=""):
                return VerificationResult(passed=True, probe_id=spec.id)

            @staticmethod
            def summarize_for_retry(v):
                return ""

        loop = _loop(
            store,
            FakeCodeAgent(),  # no changed files, so the loop stops at FIXING
            verifier=_AlwaysPasses(),
            production_url="https://www.example.com",
        )
        loop.run(incident, _spec())
        assert incident.state is IncidentState.HUMAN_REQUIRED

    def test_a_fault_that_persists_still_escalates(self, store, incident):
        loop = _loop(
            store,
            FakeCodeAgent(),
            verifier=_verifier([False, False, False, False]),
            production_url="https://www.example.com",
        )
        loop.run(incident, _spec())
        assert incident.state is IncidentState.HUMAN_REQUIRED

    def test_without_a_production_url_the_check_is_skipped(self, store, incident):
        """Absence of the re-check is never less safe, only noisier."""
        loop = _loop(store, FakeCodeAgent(), verifier=_verifier([False, False, False]))
        loop.run(incident, _spec())
        assert incident.state is IncidentState.HUMAN_REQUIRED

    def test_a_re_check_that_crashes_escalates(self, store, incident):
        """Fails closed. An unverifiable production is not a healthy one."""

        class _Exploding:
            def __init__(self):
                self.calls = 0

            def verify(self, spec, *, target_url, incident_id=""):
                self.calls += 1
                if self.calls <= 3:
                    return VerificationResult(passed=False, probe_id=spec.id)
                raise RuntimeError("the browser died")

            @staticmethod
            def summarize_for_retry(v):
                return ""

        loop = _loop(
            store,
            FakeCodeAgent(),
            verifier=_Exploding(),
            production_url="https://www.example.com",
        )
        loop.run(incident, _spec())
        assert incident.state is IncidentState.HUMAN_REQUIRED

    def test_a_policy_refusal_that_needs_a_human_is_re_checked_too(self, store):
        """Not only the exhausted-attempts path."""
        critical = store.create(
            Incident(
                fingerprint="fp_c",
                severity=Severity.CRITICAL,
                component="auth",
                title="down",
                probe_id="auth-login",
            )
        )
        loop = _loop(
            store,
            FakeCodeAgent(),
            verifier=_verifier([True]),
            policy=_policy(max_attempts=0),
            production_url="https://www.example.com",
        )
        loop.run(critical, _spec())
        # attempts_exhausted needs a human, but production is fine now.
        assert critical.state is IncidentState.RESOLVED


# ---------------------------------------------------------------------------
# The production-change lease: repair merges and feature ships are one queue
# ---------------------------------------------------------------------------


def _hold_production_lease(path_str, ready_event, release_event):
    """Hold the production-change lease in a genuinely separate process.

    A thread would prove nothing here: the whole point is that the guard has
    to be the kernel's, not this interpreter's.
    """
    from openjarvis.core.proclock import ProcessLease

    lease = ProcessLease(path_str, owner="wiz@feature-ship")
    with lease.acquire(timeout=10.0):
        ready_event.set()
        release_event.wait(timeout=30.0)


class TestProductionChangeLease:
    """One production change at a time, across processes -- not across threads.

    Before this, the repair loop's only concurrency guard was a
    ``threading.RLock`` in ``watch.py``. A watcher merging a repair and a Wiz
    shipper merging a feature therefore raced by construction, and each then
    read one shared production to decide whether *its own* change was good.
    """

    def test_a_repair_does_not_merge_while_a_feature_ship_holds_the_lease(
        self, store, incident, tmp_path
    ):
        import multiprocessing

        from openjarvis.core.proclock import ProcessLease

        lease_path = tmp_path / "production-change.lock"
        ready = multiprocessing.Event()
        release = multiprocessing.Event()
        holder = multiprocessing.Process(
            target=_hold_production_lease,
            args=(str(lease_path), ready, release),
        )
        holder.start()
        try:
            assert ready.wait(timeout=10.0), "the other process never took the lease"

            merger = _FakeMerger()
            post_merge = _FakePostMerge(verified=True)
            agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
            loop = _loop(
                store,
                agent,
                verifier=_verifier([True]),
                github=_MergingGitHub(),
                auto_merger=merger,
                post_merge_verifier=post_merge,
                production_lease=ProcessLease(lease_path, owner="repair-watcher"),
                production_lease_timeout=0.4,
            )

            outcome = loop.run(incident, _spec())

            # The merge was not attempted at all -- not attempted and refused
            # by the gates, which would have been recorded as a gate decision
            # this loop never actually reached.
            assert merger.calls == 0, "merged while another process held the lease"
            # And production was never consulted, so nothing was concluded
            # about it from somebody else's deployment.
            assert post_merge.calls == 0
            states = _states(store, incident.id)
            assert "MERGED" not in states
            # The repair itself still succeeded: the pull request is the
            # deliverable, exactly as when merging is not configured.
            assert outcome.resolved
            # And the reason is on the incident, for whoever retries it.
            evidence = " ".join(
                (e.summary or "") + " " + (e.content or "")
                for e in store.get(incident.id).evidence
            )
            assert "another production change is in progress" in evidence
            assert "wiz@feature-ship" in evidence
        finally:
            release.set()
            holder.join(timeout=10.0)
            if holder.is_alive():  # pragma: no cover - defensive
                holder.terminate()
                holder.join(timeout=5.0)

    def test_a_repair_merges_normally_once_the_lease_is_free(
        self, store, incident, tmp_path
    ):
        """The refusal above is contention, not a new permanent gate."""
        from openjarvis.core.proclock import ProcessLease

        merger = _FakeMerger()
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            auto_merger=merger,
            post_merge_verifier=_FakePostMerge(verified=True),
            production_lease=ProcessLease(
                tmp_path / "production-change.lock", owner="repair-watcher"
            ),
            production_lease_timeout=5.0,
        )

        outcome = loop.run(incident, _spec())

        assert merger.calls == 1
        assert outcome.resolved
        assert outcome.final_state is IncidentState.RESOLVED

    def test_the_lease_is_released_after_a_repair_merge(
        self, store, incident, tmp_path
    ):
        """A merge must not leave production locked behind it."""
        from openjarvis.core.proclock import ProcessLease

        lease_path = tmp_path / "production-change.lock"
        agent = FakeCodeAgent([CodeAgentResult(claim="c", changed_files=["a.ts"])])
        loop = _loop(
            store,
            agent,
            verifier=_verifier([True]),
            github=_MergingGitHub(),
            auto_merger=_FakeMerger(),
            post_merge_verifier=_FakePostMerge(verified=True),
            production_lease=ProcessLease(lease_path, owner="repair-watcher"),
            production_lease_timeout=5.0,
        )
        loop.run(incident, _spec())

        # Whoever wants production next can have it immediately.
        with ProcessLease(lease_path, owner="next").acquire(timeout=0.5):
            pass

    def test_wiz_and_the_repair_loop_resolve_the_same_lock_file(self, tmp_path):
        """The two callers must not each get their own private lease.

        This is the failure the whole change exists to prevent, and it is a
        one-line mistake to reintroduce: a lease per subsystem serialises each
        subsystem against itself and leaves the dangerous pairing unguarded.
        """
        from openjarvis.core.proclock import (
            PRODUCTION_CHANGE_LOCK,
            production_change_lease,
        )

        wiz_side = production_change_lease(owner="wiz@target", root=tmp_path)
        repair_side = production_change_lease(owner="repair-watcher", root=tmp_path)

        assert wiz_side.path == repair_side.path
        assert wiz_side.path.name == PRODUCTION_CHANGE_LOCK
        # And it really is mutual exclusion, not two objects agreeing on a name.
        with wiz_side.acquire(timeout=1.0):
            with pytest.raises(LeaseTimeout):
                with repair_side.acquire(timeout=0.3):
                    pass


class TestProductionLeaseIsActuallyWired:
    """A lease nothing passes is a lease nothing holds.

    This suite exists because of a sibling bug found in the same audit:
    ``DevelopmentQueue(production_busy=...)`` and ``_reliability_busy()`` form a
    complete, tested, plumbed-looking production-deferral gate in which no
    caller ever supplies ``production_busy``, so it defaults to
    ``lambda: False`` and the entire chain is a permanent no-op. Its unit tests
    pass, because they inject a fake pipeline rather than build a real one.
    Nothing here should be able to fail the same way quietly.
    """

    def test_the_repair_factory_passes_a_production_lease(self):
        """The real construction site, not a hand-made RepairLoop."""
        from openjarvis.cli import reliability_cmd

        assert _keyword_is_wired(
            reliability_cmd._build_repair_loop, "RepairLoop", "production_lease"
        ), (
            "the repair loop is constructed without a production-change lease, "
            "so repair merges are again unserialised against feature ships"
        )

    def test_wiz_ships_under_the_shared_lease_not_a_private_one(self):
        from openjarvis.wiz import assemble as assemble_mod

        assert _keyword_is_wired(
            assemble_mod.assemble, "FeaturePipeline", "ship_lease"
        ), "the Wiz pipeline is built with no ship lease at all"

        # And that the lease it is built with is the *shared* one. What has to
        # be true is that both subsystems open the same file, so this checks
        # which function builds the value, not how the line is spelled.
        import ast
        import inspect
        import textwrap

        from openjarvis.core.proclock import production_change_lease

        tree = ast.parse(textwrap.dedent(inspect.getsource(assemble_mod.assemble)))
        builders = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "ship_lease" or not isinstance(kw.value, ast.Call):
                    continue
                inner = kw.value.func
                builders.add(getattr(inner, "id", None) or getattr(inner, "attr", "?"))
        assert "production_change_lease" in builders, (
            f"ship_lease is built by {builders or 'something unrecognised'}, not "
            "production_change_lease -- a Wiz-private lock serialises Wiz "
            "against itself and leaves ship-versus-repair-merge unguarded"
        )
        assert production_change_lease(owner="x").path.name == "production-change.lock"


def _keyword_is_wired(func, callee: str, keyword: str) -> bool:
    """Whether *func* calls *callee* passing *keyword* a non-None value.

    Parsed, not grepped. A substring search over the source is satisfied by the
    keyword appearing in a comment -- including the comment explaining why the
    wiring matters -- and by `keyword=None`, which is the one value that turns
    the mechanism off. Both were true of the guards this replaces, so they
    would have passed with the wiring deleted: exactly the "test that cannot
    fail" this file exists to prevent.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func
        target = getattr(name, "id", None) or getattr(name, "attr", None)
        if target != callee:
            continue
        for kw in node.keywords:
            if kw.arg != keyword:
                continue
            if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                return False
            return True
    return False
