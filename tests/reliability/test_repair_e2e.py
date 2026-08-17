"""End-to-end repair against a controlled broken repository.

Unlike ``test_repair.py``, which exercises the loop's control flow with scripted
results, this drives the whole machine against **real git worktrees**, a **real
project test suite**, and a **real reproduction that executes the repaired
code**. Nothing here asserts a boolean someone handed it.

What these tests are meant to establish, in the terms of the Phase 12 brief:

* a coding agent can modify code in isolation and reach a pull request;
* a *plausible but wrong* fix is caught, retried, and ends at HUMAN_REQUIRED;
* neither path touches the operator's checkout, the default branch, or anything
  resembling production.

The preview deployment is stood in for by the worktree itself: the reproduction
runs against the repaired code rather than against a URL. That is a genuine
independent check of behaviour, but it is **not** a Vercel preview — see
``docs/JARVIS_REPAIR_LOOP.md`` for what remains unproven until the loop is run
against real infrastructure.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from openjarvis.reliability.checks import CheckSuite
from openjarvis.reliability.code_agent import CodeAgentResult, FakeCodeAgent
from openjarvis.reliability.policy import SafetyPolicy
from openjarvis.reliability.probes.spec import parse_probe
from openjarvis.reliability.repair import (
    OUTCOME_NO_DIFF,
    OUTCOME_PROTECTED_PATH,
    OUTCOME_SCOPE_VIOLATION,
    OUTCOME_TESTS_FAILED,
    OUTCOME_VERIFICATION_FAILED,
    OUTCOME_VERIFIED,
    RepairLoop,
)
from openjarvis.reliability.scope import ScopeLimits
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import (
    Incident,
    IncidentState,
    ProbeResult,
    Severity,
)
from openjarvis.reliability.verify import Verifier
from openjarvis.reliability.workspace import RepairWorkspace
from tests.reliability import fixture_repo

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _ReproductionExecutor:
    """Runs the incident's reproduction against a candidate build.

    Stands in for :class:`ProbeExecutor`. It executes the repaired code and
    reports what it observed — it never consults the coding agent's claim.
    """

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.calls = 0

    def run(self, spec):
        self.calls += 1
        ok = fixture_repo.reproduction_passes(self.workspace)
        return ProbeResult(
            probe_id=spec.id,
            success=ok,
            failure_kind="" if ok else "assertion",
            error=(
                ""
                if ok
                else "apply_discount(200, 10) did not return 180 — the original "
                "failure still reproduces"
            ),
            steps_completed=1,
        )


class _RecordingGitHub:
    """Captures pull requests and merges instead of contacting GitHub.

    ``merge_calls`` is the assertion surface for the merge tests below: proving
    a merge did not happen means proving the request was never made, not that
    the result was falsy.
    """

    base_branch = "main"
    repo = "acme/site"

    def __init__(self) -> None:
        self.pull_requests = []
        self.branches = []
        self.merge_calls = []
        #: Set by a test to control what the PR looks like when re-read.
        self.pr_facts = {}
        self.base_sha = "b" * 40
        self.client = self

    def branch_name_for(self, incident_id):
        return f"jarvis/incident-{incident_id}"

    def create_branch(self, branch, **_kwargs):
        self.branches.append(branch)
        return "sha"

    def create_pull_request(self, **kwargs):
        self.pull_requests.append(kwargs)
        return {"number": 7, "url": "https://github.com/acme/site/pull/7"}

    # -- read side used by the merge gates --------------------------------
    def get_pull_request(self, number):
        facts = {
            "number": number,
            "state": "open",
            "draft": False,
            "head_ref": "",
            "head_sha": "",
            "base_ref": "main",
            "base_sha": self.base_sha,
            "mergeable": True,
            "mergeable_state": "clean",
            "merged": False,
            "author": "jarvis-bot",
            "url": f"https://github.com/acme/site/pull/{number}",
        }
        facts.update(self.pr_facts)
        return facts

    def combined_status(self, _sha):
        return {"state": "success", "count": 1, "contexts": ["ci"]}

    def get_json(self, path, default=None, **_kw):
        if "/git/ref/heads/" in path:
            return {"object": {"sha": self.base_sha}}
        return default

    def merge_pull_request(self, **kwargs):
        self.merge_calls.append(kwargs)
        return {"merged": True, "sha": "m" * 40, "message": "Pull Request merged"}


def _spec():
    return parse_probe(
        {
            "probe": {
                "id": "pricing-discount",
                "component": "pricing",
                "severity": "high",
                "steps": [{"action": "goto", "url": "/checkout"}],
                "expect": [{"kind": "text", "value": "180"}],
            }
        }
    )


@pytest.fixture
def broken_repo(tmp_path):
    return fixture_repo.build_broken_repo(tmp_path / "target")


@pytest.fixture
def store(tmp_path):
    s = IncidentStore(tmp_path / "incidents.db")
    yield s
    s.close()


@pytest.fixture
def incident(store):
    return store.create(
        Incident(
            fingerprint="fp_discount",
            severity=Severity.HIGH,
            component="pricing",
            title="Percentage discounts are subtracted as amounts",
            summary="A 10% discount on 200 gives 190 instead of 180.",
            probe_id="pricing-discount",
            repro_steps=["Add a 200 item to the basket", "Apply a 10% discount"],
        )
    )


@pytest.fixture
def harness(broken_repo, store, tmp_path):
    """Everything wired the way production wires it, minus the network."""

    manager = RepairWorkspace(
        repo_path=str(broken_repo),
        root=str(tmp_path / "worktrees"),
        keep_on_failure=False,
    )
    github = _RecordingGitHub()

    def build(on_run, **overrides):
        # The "preview deployment" is the incident's worktree: verification runs
        # the reproduction against the code the agent actually produced.
        def preview_lookup(_branch):
            path = Path(manager.root) / incident_id["value"]
            return str(path) if path.is_dir() else ""

        verifier = Verifier(executor_factory=_ReproductionExecutor)
        agent = FakeCodeAgent(
            [CodeAgentResult(claim="I fixed the discount calculation.")],
            on_run=on_run,
        )
        active_github = overrides.pop("github", github)
        settings = dict(
            policy=SafetyPolicy(
                repair_enabled=True,
                max_attempts=3,
                protected_paths=[".github/workflows/"],
            ),
            checks=CheckSuite.from_config(
                test_command=f"{fixture_repo.PYTHON} -m pytest tests -q", timeout=120
            ),
            preview_lookup=preview_lookup,
            protected_paths=[".github/workflows/"],
            push_branch=False,  # no remote in the fixture
            sleep=lambda _s: None,
        )
        settings.update(overrides)
        loop = RepairLoop(
            agent=agent,
            verifier=verifier,
            store=store,
            workspace_manager=manager,
            github=active_github,
            **settings,
        )
        return loop, agent, active_github, manager

    incident_id = {"value": ""}
    build.incident_id = incident_id
    build.manager = manager
    build.github = github
    return build


# ---------------------------------------------------------------------------
# The two headline scenarios (§30)
# ---------------------------------------------------------------------------


class TestSuccessfulRepairReachesAPullRequest:
    def test_correct_fix_is_verified_and_pr_is_opened(
        self, harness, incident, broken_repo
    ):
        harness.incident_id["value"] = incident.id
        loop, agent, github, _ = harness(fixture_repo.agent_correct_fix)

        outcome = loop.run(incident, _spec())

        assert outcome.resolved is True
        assert incident.state is IncidentState.RESOLVED
        assert outcome.attempts == 1
        assert incident.attempts[0].outcome == OUTCOME_VERIFIED
        assert outcome.pull_request_url.endswith("/pull/7")
        assert len(github.pull_requests) == 1

    def test_the_diff_is_read_from_git_not_from_the_agent(self, harness, incident):
        """The agent declared no changed files; git says otherwise."""
        harness.incident_id["value"] = incident.id
        loop, _, _, _ = harness(fixture_repo.agent_correct_fix)
        loop.run(incident, _spec())

        changed = incident.attempts[0].changed_files
        assert "app.py" in changed
        assert "tests/test_discount_regression.py" in changed

    def test_the_base_commit_is_recorded(self, harness, incident, broken_repo):
        harness.incident_id["value"] = incident.id
        loop, _, _, _ = harness(fixture_repo.agent_correct_fix)
        loop.run(incident, _spec())

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(broken_repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        attempt = incident.attempts[0]
        assert attempt.base_commit == head
        assert len(attempt.commit_sha) == 40

    def test_the_regression_test_is_recorded_and_surfaced(self, harness, incident):
        harness.incident_id["value"] = incident.id
        loop, _, github, _ = harness(fixture_repo.agent_correct_fix)
        loop.run(incident, _spec())

        assert incident.attempts[0].regression_tests == [
            "tests/test_discount_regression.py"
        ]
        assert "test_discount_regression.py" in github.pull_requests[0]["body"]

    def test_the_pull_request_says_a_human_still_decides(self, harness, incident):
        harness.incident_id["value"] = incident.id
        loop, _, github, _ = harness(fixture_repo.agent_correct_fix)
        loop.run(incident, _spec())

        body = github.pull_requests[0]["body"]
        assert "Verified: **yes**" in body
        assert "not merged" in body.lower()
        assert "a human decides" in body.lower()

    def test_checks_are_recorded_on_the_attempt(self, harness, incident):
        harness.incident_id["value"] = incident.id
        loop, _, _, _ = harness(fixture_repo.agent_correct_fix)
        loop.run(incident, _spec())

        checks = incident.attempts[0].checks
        assert checks["passed"] is True
        assert any(r["name"] == "tests" and r["passed"] for r in checks["results"])

    def test_the_audit_chain_survives_the_whole_repair(self, harness, incident, store):
        harness.incident_id["value"] = incident.id
        loop, _, _, _ = harness(fixture_repo.agent_correct_fix)
        loop.run(incident, _spec())

        assert store.verify_chain() == (True, None)
        states = [t.to_state.value for t in store.transitions_for(incident.id)]
        assert states[-1] == "RESOLVED"
        assert "FIXING" in states and "TESTING" in states and "VERIFYING" in states


class TestFalseSuccessIsCaught:
    """§24: the agent saying "fixed" must never be enough."""

    def test_plausible_but_wrong_fix_never_resolves(self, harness, incident):
        harness.incident_id["value"] = incident.id
        loop, agent, github, _ = harness(fixture_repo.agent_plausible_but_wrong)

        outcome = loop.run(incident, _spec())

        assert outcome.resolved is False
        assert incident.state is IncidentState.HUMAN_REQUIRED
        assert github.pull_requests == []

    def test_it_retries_three_times_before_escalating(self, harness, incident):
        harness.incident_id["value"] = incident.id
        loop, agent, _, _ = harness(fixture_repo.agent_plausible_but_wrong)

        outcome = loop.run(incident, _spec())

        assert outcome.attempts == 3
        assert len(agent.calls) == 3
        assert all(a.outcome == OUTCOME_VERIFICATION_FAILED for a in incident.attempts)

    def test_the_wrong_fix_did_pass_the_projects_own_tests(self, harness, incident):
        """Which is exactly why local checks cannot be the final authority."""
        harness.incident_id["value"] = incident.id
        loop, _, _, _ = harness(fixture_repo.agent_plausible_but_wrong)
        loop.run(incident, _spec())

        assert incident.attempts[0].tests_passed is True
        assert incident.attempts[0].outcome == OUTCOME_VERIFICATION_FAILED

    def test_failure_evidence_reaches_the_next_attempt(self, harness, incident):
        harness.incident_id["value"] = incident.id
        loop, agent, _, _ = harness(fixture_repo.agent_plausible_but_wrong)
        loop.run(incident, _spec())

        assert "Previous attempt failed verification" in agent.calls[1]
        assert "still reproduces" in agent.calls[1]

    def test_a_wrong_then_right_agent_succeeds_on_the_second_attempt(
        self, harness, incident
    ):
        harness.incident_id["value"] = incident.id
        loop, _, github, _ = harness(fixture_repo.agent_wrong_then_right)

        outcome = loop.run(incident, _spec())

        assert outcome.resolved is True
        assert outcome.attempts == 2
        assert len(github.pull_requests) == 1


# ---------------------------------------------------------------------------
# Negative paths (§23)
# ---------------------------------------------------------------------------


class TestNegativePaths:
    def test_editing_ci_configuration_aborts_the_repair(self, harness, incident):
        harness.incident_id["value"] = incident.id
        loop, _, github, _ = harness(fixture_repo.agent_edits_ci)

        outcome = loop.run(incident, _spec())

        assert outcome.resolved is False
        assert incident.attempts[0].outcome == OUTCOME_PROTECTED_PATH
        assert incident.state is IncidentState.HUMAN_REQUIRED
        assert github.pull_requests == []

    def test_writing_a_credential_file_aborts_the_repair(self, harness, incident):
        harness.incident_id["value"] = incident.id
        loop, _, github, _ = harness(fixture_repo.agent_writes_a_secret)

        outcome = loop.run(incident, _spec())

        assert outcome.resolved is False
        assert incident.attempts[0].outcome == OUTCOME_SCOPE_VIOLATION
        assert ".env" in incident.attempts[0].scope["secret_like"]
        assert github.pull_requests == []

    def test_a_runaway_diff_aborts_the_repair(self, harness, incident):
        """Even though the bug itself was fixed correctly."""
        harness.incident_id["value"] = incident.id
        loop, _, github, _ = harness(
            fixture_repo.agent_runs_away, scope_limits=ScopeLimits(max_files=20)
        )

        outcome = loop.run(incident, _spec())

        assert outcome.resolved is False
        assert incident.attempts[0].outcome == OUTCOME_SCOPE_VIOLATION
        assert "files changed" in outcome.reason
        assert github.pull_requests == []

    def test_failing_the_projects_tests_stops_before_verification(
        self, harness, incident
    ):
        harness.incident_id["value"] = incident.id
        loop, _, github, _ = harness(fixture_repo.agent_breaks_the_tests)

        outcome = loop.run(incident, _spec())

        assert outcome.resolved is False
        assert all(a.outcome == OUTCOME_TESTS_FAILED for a in incident.attempts)
        assert github.pull_requests == []

    def test_test_failures_are_fed_back_to_the_agent(self, harness, incident):
        harness.incident_id["value"] = incident.id
        loop, agent, _, _ = harness(fixture_repo.agent_breaks_the_tests)
        loop.run(incident, _spec())

        assert len(agent.calls) == 3  # it kept trying, with feedback
        assert "failed" in agent.calls[1].lower()

    def test_an_agent_that_changes_nothing_is_not_a_success(self, harness, incident):
        harness.incident_id["value"] = incident.id
        loop, _, github, _ = harness(fixture_repo.agent_does_nothing)

        outcome = loop.run(incident, _spec())

        assert outcome.resolved is False
        assert all(a.outcome == OUTCOME_NO_DIFF for a in incident.attempts)
        assert github.pull_requests == []

    def test_no_preview_means_not_verified(self, harness, incident):
        harness.incident_id["value"] = incident.id
        loop, _, github, _ = harness(
            fixture_repo.agent_correct_fix, preview_lookup=lambda _b: ""
        )

        outcome = loop.run(incident, _spec())

        assert outcome.resolved is False
        assert incident.state is IncidentState.HUMAN_REQUIRED
        assert github.pull_requests == []

    def test_deployment_logs_reach_the_agent_when_no_preview_appears(
        self, harness, incident
    ):
        harness.incident_id["value"] = incident.id
        loop, agent, _, _ = harness(
            fixture_repo.agent_correct_fix,
            preview_lookup=lambda _b: "",
            preview_logs=lambda _b: "Build error: Module not found './pricing'",
        )
        loop.run(incident, _spec())

        assert "Module not found" in agent.calls[1]

    def test_github_being_unavailable_does_not_lose_the_repair(self, harness, incident):
        """A PR that cannot be opened is reported, not silently dropped."""

        class _BrokenGitHub(_RecordingGitHub):
            def create_pull_request(self, **kwargs):
                raise RuntimeError("502 Bad Gateway")

        harness.incident_id["value"] = incident.id
        loop, _, _, _ = harness(fixture_repo.agent_correct_fix, github=_BrokenGitHub())

        outcome = loop.run(incident, _spec())

        # Verification still passed, so the incident is resolved and the branch
        # exists; the missing PR is visible as an empty URL rather than a crash.
        assert outcome.resolved is True
        assert outcome.pull_request_url == ""


# ---------------------------------------------------------------------------
# Production safety (§30: "no production modification may occur")
# ---------------------------------------------------------------------------


class TestNothingOutsideTheWorktreeIsTouched:
    def _head_tree(self, repo: Path) -> str:
        return subprocess.run(
            ["git", "rev-parse", "main^{tree}"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    @pytest.mark.parametrize(
        "behaviour",
        [
            "agent_correct_fix",
            "agent_plausible_but_wrong",
            "agent_edits_ci",
            "agent_runs_away",
        ],
    )
    def test_the_default_branch_is_never_modified(
        self, harness, incident, broken_repo, behaviour
    ):
        before = self._head_tree(broken_repo)
        harness.incident_id["value"] = incident.id
        loop, _, _, _ = harness(getattr(fixture_repo, behaviour))
        loop.run(incident, _spec())

        assert self._head_tree(broken_repo) == before

    def test_the_operators_checkout_keeps_the_bug(self, harness, incident, broken_repo):
        """A successful repair changes the branch, not the working tree."""
        harness.incident_id["value"] = incident.id
        loop, _, _, _ = harness(fixture_repo.agent_correct_fix)
        loop.run(incident, _spec())

        assert (broken_repo / "app.py").read_text() == fixture_repo.BUGGY_SOURCE

    def test_the_agent_never_sees_the_operators_checkout(self, harness, incident):
        seen = []

        def spy(workspace, attempt):
            seen.append(workspace)
            fixture_repo.agent_correct_fix(workspace, attempt)

        harness.incident_id["value"] = incident.id
        loop, _, _, manager = harness(spy)
        loop.run(incident, _spec())

        assert seen
        for workspace in seen:
            assert workspace.startswith(manager.root)
            assert workspace != manager.repo_path

    def test_deploy_is_refused_even_after_verification(self, harness, incident):
        """pr_only is the ceiling: a verified fix is still only a pull request."""
        harness.incident_id["value"] = incident.id
        loop, _, _, _ = harness(fixture_repo.agent_correct_fix)

        outcome = loop.run(incident, _spec())

        assert outcome.resolved is True
        assert "pr_only" in outcome.reason

    def test_the_repair_loop_merges_nothing_unless_it_is_given_a_merger(
        self, harness, incident
    ):
        """This used to assert that no merge method existed anywhere.

        It cannot any more — merging is implemented. The property that replaces
        it is narrower and is the one that actually matters: the repair loop's
        default is still a pull request and nothing else. ``auto_merger``
        defaults to ``None``, and a loop built without one has no path to the
        default branch regardless of what else is configured.
        """
        harness.incident_id["value"] = incident.id
        loop, _, github, _ = harness(fixture_repo.agent_correct_fix)

        assert loop.auto_merger is None
        outcome = loop.run(incident, _spec())

        assert outcome.resolved is True
        assert github.pull_requests, "a pull request should still be opened"
        assert github.merge_calls == []

    def test_the_default_configuration_builds_no_merger(self, tmp_path):
        """Off by default, checked where the wiring actually happens."""
        from openjarvis.cli.reliability_cmd import _build_auto_merger
        from openjarvis.core.config import JarvisConfig

        config = JarvisConfig()
        assert config.reliability.merge.enabled is False
        assert _build_auto_merger(config, object(), _RecordingGitHub()) is None


class TestAClaimOfSuccessNeverReachesTheDefaultBranch:
    """§21, end to end through the real repair loop.

    The coding agent reports success in the same words it always uses. The fix
    it produces is plausible, passes the project's own shipped tests, and does
    not fix the bug — so independent verification, which re-runs the original
    reproduction, fails.

    Everything downstream must stay untouched: no pull request, no merge, and an
    incident that ends up with a human rather than marked resolved. The merger
    is wired in and enabled for this test precisely so that "no merge" is a
    result rather than a consequence of nothing being connected.
    """

    def _merger(self, store, github):
        from openjarvis.reliability.merge import AutoMerger

        return AutoMerger(github=github, store=store, enabled=True, base_branch="main")

    def test_agent_claims_success_verifier_disagrees_nothing_is_merged(
        self, harness, incident, store
    ):
        harness.incident_id["value"] = incident.id
        github = _RecordingGitHub()
        loop, agent, _, _ = harness(
            fixture_repo.agent_plausible_but_wrong,
            github=github,
            auto_merger=self._merger(store, github),
        )

        outcome = loop.run(incident, _spec())

        # The agent really was run, and really did claim success — otherwise
        # this test would prove only that an agent which says nothing merges
        # nothing, which is not the property under test. Read the claim off the
        # persisted attempt rather than by invoking the agent again: its
        # ``on_run`` hook writes files, and a test that re-runs it to inspect it
        # edits the working tree it is running in.
        assert agent.calls, "the agent should have been run"
        reloaded = store.get(incident.id)
        assert reloaded.attempts, "an attempt should have been recorded"
        assert all("fixed" in a.claim.lower() for a in reloaded.attempts)

        assert outcome.resolved is False
        assert outcome.final_state is IncidentState.HUMAN_REQUIRED
        assert github.merge_calls == [], "a refuted repair must not be merged"
        assert github.pull_requests == [], "a refuted repair must not open a PR"

    def test_the_projects_own_tests_passed_which_is_why_this_matters(
        self, harness, incident, store
    ):
        """The wrong fix is not caught by the local suite.

        If it were, this would only prove that failing tests block a merge —
        which is a different, weaker property already covered elsewhere. The
        point here is that everything cheap said yes and verification said no.
        """
        harness.incident_id["value"] = incident.id
        github = _RecordingGitHub()
        loop, _, _, _ = harness(
            fixture_repo.agent_plausible_but_wrong,
            github=github,
            auto_merger=self._merger(store, github),
        )

        loop.run(incident, _spec())

        reloaded = store.get(incident.id)
        attempt = reloaded.attempts[0]
        assert attempt.tests_passed is True
        assert attempt.verification is not None
        assert attempt.verification.passed is False
        assert github.merge_calls == []

    def test_the_refusal_is_in_the_audit_chain(self, harness, incident, store):
        """A merge that did not happen still leaves a record that it was judged."""
        harness.incident_id["value"] = incident.id
        github = _RecordingGitHub()
        loop, _, _, _ = harness(
            fixture_repo.agent_plausible_but_wrong,
            github=github,
            auto_merger=self._merger(store, github),
        )
        loop.run(incident, _spec())

        assert store.verify_chain()[0] is True
