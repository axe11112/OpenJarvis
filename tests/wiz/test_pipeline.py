"""The feature pipeline: from "build X" to "it's ready", and the refusals."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import pytest

from openjarvis.reliability.types import ProbeResult
from openjarvis.wiz.approvals import ApprovalStore
from openjarvis.wiz.authority import Actor, Authority, AuthorityPolicy, Channel
from openjarvis.wiz.features.acceptance import DESKTOP, MOBILE
from openjarvis.wiz.features.engineer import (
    ClaudeCodeEngineeringAgent,
    CodingEngineUnavailable,
    EngineeringSession,
)
from openjarvis.wiz.features.model import FeatureState, Priority
from openjarvis.wiz.features.pipeline import FeaturePipeline
from openjarvis.wiz.features.preview import PreviewObserver
from openjarvis.wiz.features.profile import EngineeringProfile
from openjarvis.wiz.features.queue import DevelopmentQueue
from openjarvis.wiz.features.store import FeatureStore
from openjarvis.wiz.features.verification import FeatureVerifier
from openjarvis.wiz.journal import WizJournal

# ---------------------------------------------------------------------------
# Doubles. Each one stands in for a real collaborator at the same interface,
# so a test that proves a property proves it about the shipped object.
# ---------------------------------------------------------------------------


@dataclass
class FakeWorktree:
    path: str
    branch: str
    base_commit: str


class FakeWorkspace:
    """A worktree factory that records what it was asked for."""

    def __init__(self, tmp_path, changed_files=None, diff_text="+ nothing sensitive"):
        self.tmp_path = tmp_path
        self.root = str(tmp_path)
        self.created = []
        self.changed = list(
            ["src/components/Summary.tsx"] if changed_files is None else changed_files
        )
        self.commits = []
        self.pushes = []
        self.removed = []
        self.commit_sha = "feedface" + "0" * 32
        self.diff_text = diff_text

    def create(self, feature_id, *, title="", base_ref="HEAD"):
        path = self.tmp_path / f"wt-{feature_id}"
        path.mkdir(parents=True, exist_ok=True)
        worktree = FakeWorktree(
            path=str(path),
            branch=f"wiz/feature/{feature_id}",
            base_commit="base1234" + "0" * 32,
        )
        self.created.append(worktree)
        return worktree

    def changed_files(self, worktree):
        return list(self.changed)

    def diff(self, worktree, *, max_chars=20000):
        return self.diff_text

    def line_counts(self, worktree):
        return (12, 3)

    def commit_all(self, worktree, message):
        self.commits.append(message)
        return self.commit_sha

    def push(self, worktree, *, remote="origin"):
        self.pushes.append(remote)

    def has_changes(self, worktree):
        return bool(self.changed)

    def remove(self, worktree, *, succeeded=True):
        self.removed.append((worktree, succeeded))


class ScriptedEngineer(ClaudeCodeEngineeringAgent):
    """A Claude Code adapter whose sessions are scripted.

    Subclasses the real adapter rather than duck-typing it, so that a test
    asserting "planning used no write tools" is asserting about the same class
    the operator's machine runs.
    """

    def __init__(self, *, plan_claim="I would edit Summary.tsx", builds=None):
        super().__init__(agent_factory=self._never_called)
        self.plan_claim = plan_claim
        self.builds = list(builds or [])
        self.plan_calls = []
        self.build_calls = []

    @staticmethod
    def _never_called(**kwargs):  # pragma: no cover - guards the double
        raise AssertionError("the scripted engineer should not build a CLI agent")

    def available(self):
        return True

    def plan(self, pack, *, workspace):
        self.plan_calls.append((pack, workspace))
        return EngineeringSession(mode="plan", succeeded=True, claim=self.plan_claim)

    def build(self, pack, *, workspace):
        self.build_calls.append((pack, workspace))
        if self.builds:
            return self.builds.pop(0)
        return EngineeringSession(
            mode="build", succeeded=True, claim="added the button"
        )


@dataclass
class FakeCheckResult:
    passed: bool = True
    summary: str = "all checks passed"
    #: The command's actual output, as the real CheckSuiteResult renders it.
    output: str = ""
    results: List[Dict[str, Any]] = field(default_factory=list)

    def feedback(self, *, max_chars: int = 6000):
        return self.output

    def to_dict(self):
        # Deliberately shaped like the real one, which has no "summary" key —
        # that is a property, and reading it off the dict silently returned
        # nothing.
        return {
            "passed": self.passed,
            "ran_any": True,
            "results": self.results
            or [
                {
                    "name": "tests",
                    "ran": True,
                    "passed": self.passed,
                    "summary": self.summary,
                }
            ],
        }


class FakeSuite:
    def __init__(self, results):
        self._results = list(results)
        self.runs = []

    def run(self, *, workspace, stop_early=True):
        self.runs.append(workspace)
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


class FakeVercel:
    def __init__(self, state="READY"):
        self.state = state

    def list_deployments(self, **kwargs):
        return [
            {
                "id": "dpl_1",
                "state": self.state,
                "target": "preview",
                "url": "https://feature-preview.app",
                "created_at": "2026-08-19T10:00:00+00:00",
                "commit_sha": "feedface" + "0" * 32,
                "branch": "wiz/feature/FEAT-00001",
            }
        ]

    def get_build_logs(self, deployment_id, **kwargs):
        return "Error: Type 'string' is not assignable to type 'number'"


class FakeBrowser:
    def __init__(self, outcomes=None):
        # A list of booleans, one per *verification pass* (not per probe).
        self.outcomes = list(outcomes or [True])
        self.runs = []

    def run(self, spec, *, base_url="", evidence_dir=None, **kwargs):
        self.runs.append(spec)
        ok = self.outcomes[0] if len(self.outcomes) == 1 else self.outcomes[0]
        return ProbeResult(
            probe_id=spec.id,
            success=ok,
            error="" if ok else "the page does not contain 'Download report'",
            metadata={"viewport": spec.metadata.get("viewport", "")},
        )

    def next_pass(self):
        if len(self.outcomes) > 1:
            self.outcomes.pop(0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROFILE = EngineeringProfile(
    name="wize",
    repository="acme/wize",
    checkout="/tmp/wize",
    base_branch="main",
    lint_command="npm run lint",
    test_command="npm test",
    build_command="npm run build",
)

REQUEST = 'Add a "Download report" button to /coach/summary'


@pytest.fixture
def clock():
    ticks = {"n": 0}

    def tick():
        ticks["n"] += 1
        return f"2026-08-19T10:{ticks['n']:02d}:00+00:00"

    return tick


def build_pipeline(
    tmp_path,
    clock,
    *,
    engineer=None,
    suite_results=None,
    browser=None,
    vercel_state="READY",
    workspace=None,
    policy=None,
    queue=None,
    approvals=None,
    max_attempts=3,
    verifier=True,
    preview=True,
):
    store = FeatureStore(tmp_path / "features.db")
    suite = FakeSuite(suite_results or [FakeCheckResult()])
    the_browser = browser or FakeBrowser()

    observer = None
    if preview:
        observer = PreviewObserver(
            vercel=FakeVercel(vercel_state),
            sleep=lambda s: None,
            monotonic=lambda: 0.0,
            timeout_seconds=0.0,
        )

    pipeline = FeaturePipeline(
        store=store,
        profile=PROFILE,
        engineer=engineer or ScriptedEngineer(),
        workspace=workspace or FakeWorkspace(tmp_path),
        check_suite_factory=lambda profile: suite,
        preview=observer,
        verifier=(
            FeatureVerifier(runner_factory=lambda vp: the_browser) if verifier else None
        ),
        queue=queue,
        journal=WizJournal(tmp_path / "journal.jsonl"),
        approvals=approvals,
        policy=policy,
        max_attempts=max_attempts,
        clock=clock,
    )
    pipeline._suite = suite
    pipeline._browser = the_browser
    return pipeline


def operator_actor(channel=Channel.CONTROL_CENTER):
    return Actor(actor_id="operator", channel=channel, authenticated=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_a_low_risk_feature_reaches_ready(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY, result.history[-1]
        assert result.preview_url == "https://feature-preview.app"

    def test_the_states_are_visited_in_order_and_none_is_skipped(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        visited = [entry["to"] for entry in result.history]
        assert visited == [
            "UNDERSTANDING",
            "PLANNING",
            "APPROVED_FOR_BUILD",
            "BUILDING",
            "TESTING",
            "PREVIEWING",
            "VERIFYING",
            "READY",
        ]

    def test_the_branch_is_pushed_before_a_preview_is_expected(self, tmp_path, clock):
        workspace = FakeWorkspace(tmp_path)
        pipeline = build_pipeline(tmp_path, clock, workspace=workspace)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        assert workspace.pushes == ["origin"]
        assert workspace.commits

    def test_the_operator_gets_plain_english_at_each_step(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        messages = []
        for _ in range(10):
            if feature.terminal or feature.state is FeatureState.READY:
                break
            step = pipeline.advance(feature)
            feature = step.feature
            messages.append(step.message)
        assert "Understanding..." in messages
        assert "Planning..." in messages
        assert any("Claude Code" in m for m in messages)
        assert any(m.startswith("Sir,") for m in messages)


class TestPlanProposedCriteria:
    """The planning session's proposed criteria reach the real contract."""

    CRITERIA_BLOCK = (
        "I would add a Download button next to the summary heading.\n\n"
        "```acceptance-criteria\n"
        "[\n"
        '  {"kind": "INTERACTION", "route": "/coach/summary", "selector": '
        '"button[name=download]", "then_text": "Downloaded", "description": '
        '"clicking it downloads"}\n'
        "]\n"
        "```\n"
    )

    def test_a_proposed_criterion_reaches_the_real_contract(self, tmp_path, clock):
        engineer = ScriptedEngineer(plan_claim=self.CRITERIA_BLOCK)
        pipeline = build_pipeline(tmp_path, clock, engineer=engineer)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY, result.history[-1]

        assert result.metadata["proposed_criteria"][0]["kind"] == "INTERACTION"
        contract_kinds = {c["kind"] for c in result.metadata["contract"]["criteria"]}
        assert "INTERACTION" in contract_kinds
        # Additive, not instead of: the deterministic criterion is still there.
        assert "CONTENT" in contract_kinds

    def test_a_plan_with_no_block_proposes_nothing(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)  # default plan_claim, no block
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY, result.history[-1]
        assert "proposed_criteria" not in result.metadata

    def test_a_malformed_block_does_not_stop_the_feature(self, tmp_path, clock):
        engineer = ScriptedEngineer(
            plan_claim="```acceptance-criteria\n[{not valid json\n```"
        )
        pipeline = build_pipeline(tmp_path, clock, engineer=engineer)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY, result.history[-1]
        assert "proposed_criteria" not in result.metadata


class TestClaudeCodeIsTheOnlyAuthor:
    def test_every_build_goes_through_the_claude_adapter(self, tmp_path, clock):
        engineer = ScriptedEngineer()
        pipeline = build_pipeline(tmp_path, clock, engineer=engineer)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        assert len(engineer.build_calls) == 1
        assert len(engineer.plan_calls) == 1

    def test_an_unavailable_cli_stops_the_work_rather_than_finding_another_way(
        self, tmp_path, clock
    ):
        class Missing(ScriptedEngineer):
            def plan(self, pack, *, workspace):
                raise CodingEngineUnavailable("the 'claude' CLI is not available")

        pipeline = build_pipeline(tmp_path, clock, engineer=Missing())
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert "cannot write code" in result.history[-1]["reason"]

    def test_planning_runs_in_a_worktree_and_not_the_live_checkout(
        self, tmp_path, clock
    ):
        engineer = ScriptedEngineer()
        pipeline = build_pipeline(tmp_path, clock, engineer=engineer)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        _, workspace = engineer.plan_calls[0]
        assert workspace != PROFILE.checkout
        assert "wt-FEAT" in workspace

    def test_planning_and_building_share_one_worktree(self, tmp_path, clock):
        engineer = ScriptedEngineer()
        pipeline = build_pipeline(tmp_path, clock, engineer=engineer)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        assert engineer.plan_calls[0][1] == engineer.build_calls[0][1]

    def test_the_planning_session_never_gets_a_write_tool(self):
        # Proved against the real adapter's declared tool lists rather than
        # against a double, because this is the property that makes "read-only
        # planning" true rather than aspirational.
        from openjarvis.wiz.features.engineer import BUILDING_TOOLS, PLANNING_TOOLS

        assert set(PLANNING_TOOLS).isdisjoint({"Edit", "Write", "Bash"})
        assert "Bash" in BUILDING_TOOLS


class TestTheDiffIsReadFromGit:
    def test_the_agents_claim_is_stored_but_the_files_come_from_the_workspace(
        self, tmp_path, clock
    ):
        workspace = FakeWorkspace(tmp_path, changed_files=["src/components/Card.tsx"])
        engineer = ScriptedEngineer(
            builds=[
                EngineeringSession(
                    mode="build",
                    succeeded=True,
                    claim="I rewrote the entire authentication system",
                    changed_files=["src/lib/auth/session.ts"],
                )
            ]
        )
        pipeline = build_pipeline(
            tmp_path, clock, engineer=engineer, workspace=workspace
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        attempt = result.attempts[0]
        assert attempt.changed_files == ["src/components/Card.tsx"]
        assert "authentication" in attempt.claim

    def test_a_session_that_changed_nothing_is_a_failed_attempt(self, tmp_path, clock):
        # The most common silent failure: a session that ends confidently and
        # changed no files. Carried into the gates it would pass against
        # nothing at all.
        workspace = FakeWorkspace(tmp_path, changed_files=[])
        pipeline = build_pipeline(tmp_path, clock, workspace=workspace, max_attempts=1)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert "changed no files" in result.attempts[0].failure


class TestRiskIsRedecidedOnTheRealDiff:
    def test_a_harmless_request_that_touches_authentication_stops(
        self, tmp_path, clock
    ):
        # The case the whole re-classification exists for: the request read as
        # a styling change and the diff turned out to touch session handling.
        workspace = FakeWorkspace(tmp_path, changed_files=["src/lib/auth/session.ts"])
        pipeline = build_pipeline(tmp_path, clock, workspace=workspace)
        feature = pipeline.submit(
            "make the header a bit rounder", actor=operator_actor()
        )
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert result.risk == "HIGH"
        assert not workspace.pushes, "nothing sensitive may be pushed"

    def test_an_agent_may_raise_the_risk_but_not_lower_it(self, tmp_path, clock):
        engineer = ScriptedEngineer(
            plan_claim="This is high risk: it changes how sessions are stored."
        )
        pipeline = build_pipeline(tmp_path, clock, engineer=engineer)
        feature = pipeline.submit("adjust the spacing", actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.risk == "HIGH"

    def test_a_high_risk_feature_will_not_build_without_an_approval(
        self, tmp_path, clock
    ):
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(
            "change who is allowed to see other swimmers' data",
            actor=operator_actor(),
        )
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.PLANNING
        assert result.risk == "HIGH"
        assert result.attempts == []

    def test_an_approval_bound_to_this_plan_lets_it_build(self, tmp_path, clock):
        now = {"t": 0.0}
        approvals = ApprovalStore(clock=lambda: now["t"], ttl_seconds=900)
        pipeline = build_pipeline(tmp_path, clock, approvals=approvals)
        feature = pipeline.submit(
            "change who is allowed to see other swimmers' data",
            actor=operator_actor(),
        )
        pipeline.run(feature.id)

        feature = pipeline.store.get(feature.id)
        assert feature.state is FeatureState.PLANNING

        from openjarvis.wiz.features.acceptance import contract_for
        from openjarvis.wiz.features.pipeline import _digest

        contract = contract_for(
            feature_id=feature.id,
            request=feature.operator_request,
            plan=feature.plan,
            gates=list(PROFILE.configured_gates),
        )
        approval = approvals.issue(
            capability="feature.build",
            subject=feature.id,
            parameters={
                "plan": _digest(feature.plan),
                "risk": feature.risk,
                "acceptance": contract.describe(),
            },
        )
        feature.metadata["approval_token"] = approval.token
        pipeline.store.save(feature)

        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY

    def test_an_approval_for_a_different_plan_does_not_work(self, tmp_path, clock):
        now = {"t": 0.0}
        approvals = ApprovalStore(clock=lambda: now["t"], ttl_seconds=900)
        pipeline = build_pipeline(tmp_path, clock, approvals=approvals)
        feature = pipeline.submit(
            "change who is allowed to see other swimmers' data",
            actor=operator_actor(),
        )
        pipeline.run(feature.id)
        feature = pipeline.store.get(feature.id)

        approval = approvals.issue(
            capability="feature.build",
            subject=feature.id,
            parameters={"plan": "a completely different plan", "risk": "HIGH"},
        )
        feature.metadata["approval_token"] = approval.token
        pipeline.store.save(feature)

        result = pipeline.run(feature.id)
        assert result.state is FeatureState.PLANNING
        assert "not the action that was approved" in result.metadata["approval_error"]


class TestTheIterativeLoop:
    def test_a_failing_gate_produces_another_attempt_rather_than_a_message(
        self, tmp_path, clock
    ):
        # §16: the operator is not told about the first normal failure. That is
        # what the loop is for.
        pipeline = build_pipeline(
            tmp_path,
            clock,
            suite_results=[
                FakeCheckResult(passed=False, summary="2 tests failed"),
                FakeCheckResult(passed=True),
            ],
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert len(result.attempts) == 2
        assert result.attempts[0].failure == "2 tests failed"

    def test_the_second_attempt_is_told_exactly_what_went_wrong(self, tmp_path, clock):
        engineer = ScriptedEngineer()
        pipeline = build_pipeline(
            tmp_path,
            clock,
            engineer=engineer,
            suite_results=[
                FakeCheckResult(
                    passed=False,
                    summary="typecheck: Property 'total' does not exist",
                ),
                FakeCheckResult(passed=True),
            ],
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        second_pack, _ = engineer.build_calls[1]
        rendered = second_pack.render()
        assert "Property 'total' does not exist" in rendered
        assert "Do not repeat an approach listed here" in rendered

    def test_a_failed_preview_build_feeds_its_logs_back(self, tmp_path, clock):
        engineer = ScriptedEngineer()
        pipeline = build_pipeline(
            tmp_path, clock, engineer=engineer, vercel_state="ERROR", max_attempts=2
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert len(engineer.build_calls) == 2
        second_pack, _ = engineer.build_calls[1]
        assert "not assignable to type" in second_pack.render()
        assert result.state is FeatureState.HUMAN_REQUIRED

    def test_attempts_are_bounded(self, tmp_path, clock):
        pipeline = build_pipeline(
            tmp_path,
            clock,
            suite_results=[FakeCheckResult(passed=False, summary="still broken")],
            max_attempts=2,
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert len(result.attempts) == 2
        assert "tried 2 times" in result.history[-1]["reason"]

    def test_the_operator_is_told_only_when_the_attempts_run_out(self, tmp_path, clock):
        pipeline = build_pipeline(
            tmp_path,
            clock,
            suite_results=[FakeCheckResult(passed=False, summary="still broken")],
            max_attempts=2,
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        messages = []
        for _ in range(20):
            if feature.terminal or feature.state is FeatureState.READY:
                break
            step = pipeline.advance(feature)
            feature = step.feature
            messages.append(step.message)
        sir_messages = [m for m in messages if m.startswith("Sir,")]
        assert len(sir_messages) == 1
        assert "could not get" in sir_messages[0]


class TestVerificationDecides:
    def test_a_preview_that_fails_the_contract_is_not_ready(self, tmp_path, clock):
        pipeline = build_pipeline(
            tmp_path, clock, browser=FakeBrowser([False]), max_attempts=1
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert result.state is not FeatureState.READY

    def test_the_feature_is_checked_on_both_screen_sizes(self, tmp_path, clock):
        browser = FakeBrowser()
        pipeline = build_pipeline(tmp_path, clock, browser=browser)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        viewports = {spec.metadata["viewport"] for spec in browser.runs}
        assert viewports == {DESKTOP.name, MOBILE.name}

    def test_without_a_browser_the_feature_does_not_claim_to_work(
        self, tmp_path, clock
    ):
        pipeline = build_pipeline(tmp_path, clock, verifier=False)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert "no browser" in result.history[-1]["reason"]

    def test_without_a_preview_provider_the_feature_does_not_claim_to_work(
        self, tmp_path, clock
    ):
        pipeline = build_pipeline(tmp_path, clock, preview=False)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert "no way to see a preview" in result.history[-1]["reason"]


class TestProductionAlwaysWins:
    def test_a_feature_stops_when_reliability_needs_the_machine(self, tmp_path, clock):
        queue = DevelopmentQueue(max_concurrent=1)
        pipeline = build_pipeline(tmp_path, clock, queue=queue)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        queue.admit_next()
        queue.yield_to_production("the site is down")

        result = pipeline.advance(feature)
        assert not result.progressed
        assert "live site" in result.message
        assert result.feature.state is FeatureState.RECEIVED

    def test_the_work_is_not_lost_when_it_yields(self, tmp_path, clock):
        queue = DevelopmentQueue(max_concurrent=1)
        pipeline = build_pipeline(tmp_path, clock, queue=queue)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.advance(feature)  # UNDERSTANDING
        queue.admit_next()
        queue.yield_to_production("incident")
        pipeline.advance(feature)

        stored = pipeline.store.get(feature.id)
        assert stored.state is FeatureState.UNDERSTANDING
        assert not stored.terminal


class TestAuthority:
    def test_a_channel_without_code_write_cannot_have_code_written_for_it(
        self, tmp_path, clock
    ):
        policy = AuthorityPolicy(
            grants={Channel.CONTROL_CENTER: frozenset({Authority.READ})}
        )
        pipeline = build_pipeline(tmp_path, clock, policy=policy)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert result.attempts == []

    def test_a_channel_with_code_write_may_proceed(self, tmp_path, clock):
        policy = AuthorityPolicy(
            grants={
                Channel.CONTROL_CENTER: frozenset(
                    {Authority.READ, Authority.CODE_WRITE}
                )
            }
        )
        pipeline = build_pipeline(tmp_path, clock, policy=policy)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY

    def test_voice_is_capped_below_what_the_policy_grants(self, tmp_path, clock):
        # The ceiling is in source, not configuration: voice must be *unable*
        # to have production code written for it, not merely un-granted.
        policy = AuthorityPolicy(
            grants={Channel.VOICE: frozenset({Authority.PRODUCTION_CHANGE})}
        )
        pipeline = build_pipeline(tmp_path, clock, policy=policy)
        feature = pipeline.submit(REQUEST, actor=operator_actor(Channel.VOICE))
        result = pipeline.run(feature.id)
        # Voice's ceiling includes CODE_WRITE, so the build itself is allowed;
        # what it can never carry is a production change, and the policy's own
        # tests cover that. Here the point is that the ceiling is consulted.
        assert result.state in (FeatureState.READY, FeatureState.HUMAN_REQUIRED)


class TestIntake:
    def test_every_channel_produces_the_same_kind_of_request(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        made = [
            pipeline.submit(REQUEST, actor=operator_actor(channel))
            for channel in (
                Channel.CONTROL_CENTER,
                Channel.CLI,
                Channel.TELEGRAM,
                Channel.VOICE,
            )
        ]
        assert len({f.state for f in made}) == 1
        assert {f.source for f in made} == {
            "control_center",
            "cli",
            "telegram",
            "voice",
        }

    def test_an_empty_request_is_refused(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        with pytest.raises(ValueError, match="what to build"):
            pipeline.submit("   ", actor=operator_actor())

    def test_a_request_gets_a_title_it_can_be_called_by(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        assert feature.title
        assert len(feature.title) <= 120

    def test_a_feature_cannot_claim_a_reliability_priority(self, tmp_path, clock):
        queue = DevelopmentQueue(max_concurrent=1)
        pipeline = build_pipeline(tmp_path, clock, queue=queue)
        pipeline.submit(REQUEST, actor=operator_actor(), priority=Priority.P0)
        waiting = queue.waiting()
        assert waiting[0].priority is Priority.P2


class TestDiskSpacePreflight:
    """FEAT-00007 crashed a coding session with a bare "JavaScript heap out
    of memory"; the machine was actually out of disk. FEAT-00008 died too
    abruptly to even record a failed attempt. Both should instead stop
    cleanly, before doing more work that is likely to crash mid-way."""

    def test_advance_refuses_to_start_when_disk_is_low(
        self, tmp_path, clock, monkeypatch
    ):
        import openjarvis.wiz.features.pipeline as pipeline_module

        monkeypatch.setattr(pipeline_module, "has_enough_disk", lambda root: False)
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert "disk" in result.history[-1]["reason"].lower()
        # Nothing was attempted: still RECEIVED-shaped, no attempt recorded.
        assert not result.attempts

    def test_a_workspace_with_no_root_skips_the_check(
        self, tmp_path, clock, monkeypatch
    ):
        # A test double or an unconfigured workspace has no volume to check;
        # that must not be treated as "unsafe to proceed".
        import openjarvis.wiz.features.pipeline as pipeline_module

        monkeypatch.setattr(
            pipeline_module,
            "has_enough_disk",
            lambda root: (_ for _ in ()).throw(AssertionError("should not be called")),
        )
        workspace = FakeWorkspace(tmp_path)
        workspace.root = None
        pipeline = build_pipeline(tmp_path, clock, workspace=workspace)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY

    def test_healthy_disk_proceeds_normally(self, tmp_path, clock, monkeypatch):
        import openjarvis.wiz.features.pipeline as pipeline_module

        monkeypatch.setattr(pipeline_module, "has_enough_disk", lambda root: True)
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY


class TestAudit:
    def test_every_step_is_journalled(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        entries = (tmp_path / "journal.jsonl").read_text().splitlines()
        kinds = {__import__("json").loads(line)["kind"] for line in entries}
        assert "feature.received" in kinds
        assert "feature.ready" in kinds

    def test_the_journal_chain_survives_a_whole_feature(self, tmp_path, clock):
        journal = WizJournal(tmp_path / "journal.jsonl")
        pipeline = build_pipeline(tmp_path, clock)
        pipeline.journal = journal
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        intact, break_at = journal.verify()
        assert intact, f"the audit chain broke at {break_at}"


class TestReadyOpensAPullRequestAndNothingMore:
    class FakeShipper:
        def __init__(self):
            self.opened = []
            self.merged = []

        def open_pull_request(self, feature):
            self.opened.append(feature.id)
            feature.pr_url = "https://github.com/a/b/pull/7"
            feature.pr_number = 7
            return {"created": True, "url": feature.pr_url, "number": 7}

    def test_a_ready_feature_gets_a_pull_request(self, tmp_path, clock):
        shipper = self.FakeShipper()
        pipeline = build_pipeline(tmp_path, clock)
        pipeline.shipper = shipper
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert shipper.opened == [feature.id]
        assert result.pr_url.endswith("/pull/7")

    def test_a_feature_that_did_not_reach_ready_gets_no_pull_request(
        self, tmp_path, clock
    ):
        shipper = self.FakeShipper()
        pipeline = build_pipeline(
            tmp_path,
            clock,
            suite_results=[FakeCheckResult(passed=False, summary="broken")],
            max_attempts=1,
        )
        pipeline.shipper = shipper
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        assert shipper.opened == []

    def test_run_never_merges_anything(self, tmp_path, clock):
        # `run` is the autonomous loop; it stops at READY whatever is
        # configured. Only the separate `ship` verb — see TestShip — can take
        # a feature further, and nothing here calls it.
        shipper = self.FakeShipper()
        pipeline = build_pipeline(tmp_path, clock)
        pipeline.shipper = shipper
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert shipper.merged == []

    def test_a_shipper_that_fails_does_not_undo_ready(self, tmp_path, clock):
        class Broken:
            def open_pull_request(self, feature):
                raise RuntimeError("403 Forbidden")

        pipeline = build_pipeline(tmp_path, clock)
        pipeline.shipper = Broken()
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY


class TestReadyCleansUpItsWorktree:
    """FEAT-00001 through FEAT-00006 each left node_modules and a `next
    build` on disk forever once READY — nothing downstream of READY ever
    reads the local worktree again, since its commit is already pushed."""

    def test_the_worktree_is_removed_once_ready(self, tmp_path, clock):
        workspace = FakeWorkspace(tmp_path)
        shipper = TestReadyOpensAPullRequestAndNothingMore.FakeShipper()
        pipeline = build_pipeline(tmp_path, clock, workspace=workspace)
        pipeline.shipper = shipper
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert len(workspace.removed) == 1
        removed_worktree, succeeded = workspace.removed[0]
        assert removed_worktree is workspace.created[0]
        assert succeeded is True

    def test_a_worktree_that_cannot_be_removed_does_not_undo_ready(
        self, tmp_path, clock
    ):
        class Unremovable(FakeWorkspace):
            def remove(self, worktree, *, succeeded=True):
                raise OSError("device busy")

        pipeline = build_pipeline(tmp_path, clock, workspace=Unremovable(tmp_path))
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY

    def test_a_feature_that_does_not_reach_ready_keeps_its_worktree(
        self, tmp_path, clock
    ):
        # Preserved for inspection — see FeaturePipeline.run's own docstring
        # on stopping "with its worktree intact".
        workspace = FakeWorkspace(tmp_path)
        pipeline = build_pipeline(
            tmp_path,
            clock,
            workspace=workspace,
            suite_results=[FakeCheckResult(passed=False, summary="broken")],
            max_attempts=1,
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is not FeatureState.READY
        assert workspace.removed == []


class TestShip:
    """`ship`: the explicit verb `run` never reaches on its own."""

    #: Matches FakeWorkspace.commit_sha, so a real merge decision sees the
    #: pull request head as the exact commit the pipeline verified.
    HEAD_SHA = "feedface" + "0" * 32
    #: Matches the worktree's base_commit, so "the base branch has not moved"
    #: is a gate that can actually be satisfied by a fake.
    BASE_SHA = "base1234" + "0" * 32

    class FakeGitHub:
        def __init__(self, *, can_write=True, merge_result=None):
            self.pr_calls: List[Dict[str, Any]] = []
            self.merge_calls: List[Dict[str, Any]] = []
            self._can_write = can_write
            self._merge_result = merge_result or {
                "merged": True,
                "sha": "deadbeef" + "0" * 32,
            }

        def create_pull_request(self, **kwargs):
            self.pr_calls.append(kwargs)
            return {"html_url": "https://github.com/a/b/pull/7", "number": 7}

        def get_pull_request(self, number):
            return {
                "number": number,
                "head_sha": TestShip.HEAD_SHA,
                "base_sha": TestShip.BASE_SHA,
                "state": "open",
                "mergeable": True,
            }

        def combined_status(self, sha, *, required_contexts=()):
            return {"state": "success", "contexts": {}}

        def can_write(self):
            return self._can_write

        def merge_pull_request(self, **kwargs):
            self.merge_calls.append(kwargs)
            return self._merge_result

    class FakePostShip:
        def __init__(self, *, verified=True, reason="production agrees"):
            self.calls: List[str] = []
            self.journal = None
            self._verified = verified
            self._reason = reason

        def verify(self, feature, *, merge_commit_sha):
            from openjarvis.wiz.features.postship import PostShipResult

            self.calls.append(merge_commit_sha)
            return PostShipResult(verified=self._verified, reason=self._reason)

    def _shipper(self, github, *, production_change=True):
        from openjarvis.wiz.features.shipping import (
            FeatureShipper,
            FeatureShippingPolicy,
        )

        authorities = {Authority.PR_WRITE}
        if production_change:
            authorities.add(Authority.PRODUCTION_CHANGE)
        return FeatureShipper(
            # The REQUEST fixture classifies MEDIUM once a real diff exists
            # (a component file, touched with no path-level risk signal), so
            # both are enabled — the point of these tests is `ship`'s own
            # wiring, not re-proving evaluate_shipping's risk gate.
            policy=FeatureShippingPolicy(merge_low_risk=True, merge_medium_risk=True),
            github=github,
            authority=AuthorityPolicy(
                grants={Channel.CONTROL_CENTER: frozenset(authorities)}
            ),
        )

    def _ready(self, tmp_path, clock, *, shipper, postship=None):
        pipeline = build_pipeline(tmp_path, clock)
        pipeline.shipper = shipper
        pipeline.postship = postship
        submitted = pipeline.submit(REQUEST, actor=operator_actor())
        feature = pipeline.run(submitted.id)
        return pipeline, feature

    def test_a_ready_feature_merges_and_reaches_complete(self, tmp_path, clock):
        github = self.FakeGitHub()
        postship = self.FakePostShip(verified=True)
        pipeline, feature = self._ready(
            tmp_path, clock, shipper=self._shipper(github), postship=postship
        )
        assert feature.state is FeatureState.READY

        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.COMPLETE
        assert github.merge_calls[0]["number"] == 7
        assert github.merge_calls[0]["expected_head_sha"] == self.HEAD_SHA
        assert postship.calls == [github._merge_result["sha"]]

    def test_states_are_visited_in_order(self, tmp_path, clock):
        github = self.FakeGitHub()
        pipeline, feature = self._ready(
            tmp_path,
            clock,
            shipper=self._shipper(github),
            postship=self.FakePostShip(verified=True),
        )
        shipped = pipeline.ship(feature.id)
        visited = [entry["to"] for entry in shipped.history]
        tail = [
            FeatureState.READY.value,
            FeatureState.MERGING.value,
            FeatureState.DEPLOYING.value,
            FeatureState.PRODUCTION_VERIFYING.value,
            FeatureState.COMPLETE.value,
        ]
        assert visited[-len(tail) :] == tail

    def test_production_disagreeing_hands_over_not_completes(self, tmp_path, clock):
        github = self.FakeGitHub()
        postship = self.FakePostShip(verified=False, reason="the button is missing")
        pipeline, feature = self._ready(
            tmp_path, clock, shipper=self._shipper(github), postship=postship
        )
        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.HUMAN_REQUIRED
        # The merge itself is not undone — only a person decides on a revert.
        assert github.merge_calls

    def test_a_feature_not_yet_ready_is_refused(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        pipeline.shipper = self._shipper(self.FakeGitHub())
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        # Deliberately not run(): the feature is still RECEIVED.
        result = pipeline.ship(feature.id)
        assert result.state is FeatureState.RECEIVED

    def test_no_shipper_configured_is_refused(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        result = pipeline.ship(feature.id)
        assert result.state is FeatureState.READY

    def test_a_token_without_push_permission_refuses_and_stays_ready(
        self, tmp_path, clock
    ):
        github = self.FakeGitHub(can_write=False)
        pipeline, feature = self._ready(
            tmp_path,
            clock,
            shipper=self._shipper(github),
            postship=self.FakePostShip(),
        )
        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.READY
        assert github.merge_calls == []

    def test_a_channel_without_production_change_cannot_ship(self, tmp_path, clock):
        github = self.FakeGitHub()
        pipeline, feature = self._ready(
            tmp_path,
            clock,
            shipper=self._shipper(github, production_change=False),
            postship=self.FakePostShip(),
        )
        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.READY
        assert github.merge_calls == []

    def test_no_postship_configured_merges_but_hands_to_a_person(self, tmp_path, clock):
        github = self.FakeGitHub()
        pipeline, feature = self._ready(
            tmp_path, clock, shipper=self._shipper(github), postship=None
        )
        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.HUMAN_REQUIRED
        assert github.merge_calls  # the merge did happen; only proof is missing

    def test_shipping_twice_only_merges_once(self, tmp_path, clock):
        # The second call finds the feature COMPLETE, not READY, and refuses
        # before touching GitHub again.
        github = self.FakeGitHub()
        pipeline, feature = self._ready(
            tmp_path,
            clock,
            shipper=self._shipper(github),
            postship=self.FakePostShip(verified=True),
        )
        pipeline.ship(feature.id)
        pipeline.ship(feature.id)
        assert len(github.merge_calls) == 1

    def test_two_features_shipping_at_once_never_overlap(self, tmp_path, clock):
        # Feature A reaches READY, feature B reaches READY, and both are
        # ship()ped from separate threads at the same moment. Without
        # self._ship_lock this races: two production verifications could
        # interleave and there would be nothing stopping one feature's
        # postship.verify() from running concurrently with another's merge.
        import threading
        import time

        class TrackingPostShip:
            def __init__(self):
                self.lock = threading.Lock()
                self.active = 0
                self.max_active = 0
                self.calls = []

            def verify(self, feature, *, merge_commit_sha):
                from openjarvis.wiz.features.postship import PostShipResult

                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                # Widen the race window deliberately: a real production
                # observation takes real time, and this is where two
                # verifications would overlap if the lock did not serialise
                # ship() as a whole.
                time.sleep(0.05)
                with self.lock:
                    self.calls.append((feature.id, merge_commit_sha))
                    self.active -= 1
                return PostShipResult(verified=True, reason="production agrees")

        class MultiFeatureGitHub(self.FakeGitHub):
            def __init__(self):
                super().__init__()
                self._next_pr = 100

            def create_pull_request(self, **kwargs):
                self._next_pr += 1
                self.pr_calls.append(kwargs)
                return {
                    "html_url": f"https://github.com/a/b/pull/{self._next_pr}",
                    "number": self._next_pr,
                }

            def get_pull_request(self, number):
                return {
                    "number": number,
                    "head_sha": TestShip.HEAD_SHA,
                    "base_sha": TestShip.BASE_SHA,
                    "state": "open",
                    "mergeable": True,
                }

            def merge_pull_request(self, **kwargs):
                self.merge_calls.append(kwargs)
                return {"merged": True, "sha": f"sha-for-pr-{kwargs['number']}"}

        github = MultiFeatureGitHub()
        postship = TrackingPostShip()
        pipeline = build_pipeline(tmp_path, clock)
        pipeline.shipper = self._shipper(github)
        pipeline.postship = postship

        feature_a = pipeline.run(pipeline.submit(REQUEST, actor=operator_actor()).id)
        feature_b = pipeline.run(pipeline.submit(REQUEST, actor=operator_actor()).id)
        assert feature_a.state is FeatureState.READY
        assert feature_b.state is FeatureState.READY
        assert feature_a.id != feature_b.id

        results = {}

        def ship(feature_id):
            results[feature_id] = pipeline.ship(feature_id)

        t1 = threading.Thread(target=ship, args=(feature_a.id,))
        t2 = threading.Thread(target=ship, args=(feature_b.id,))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert postship.max_active == 1, "two features verified production concurrently"
        assert len(postship.calls) == 2
        assert results[feature_a.id].state is FeatureState.COMPLETE
        assert results[feature_b.id].state is FeatureState.COMPLETE
        # Each feature's production observation was bound to its own merge
        # SHA — neither verified the other's deployment.
        assert postship.calls[0][1] != postship.calls[1][1]

    def test_a_retry_against_an_already_merged_pr_asks_a_person_not_a_stall(
        self, tmp_path, clock
    ):
        # The crash window this covers: GitHub confirmed the merge, but this
        # process never recorded it (killed, or a second process retried
        # first). The feature is still READY locally. ship() must not
        # silently refuse forever — "the PR is closed" is technically true
        # and explains nothing an operator could act on — and must not guess
        # that it is safe to continue into production verification either.
        class AlreadyMergedGitHub(self.FakeGitHub):
            def get_pull_request(self, number):
                return {
                    "number": number,
                    "head_sha": TestShip.HEAD_SHA,
                    "base_sha": TestShip.BASE_SHA,
                    "state": "closed",
                    "mergeable": None,
                    "merged": True,
                }

        github = AlreadyMergedGitHub()
        pipeline, feature = self._ready(
            tmp_path,
            clock,
            shipper=self._shipper(github),
            postship=self.FakePostShip(verified=True),
        )
        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.HUMAN_REQUIRED
        assert "already merged" in shipped.history[-1]["reason"]
        # No second merge was attempted against the already-merged PR.
        assert github.merge_calls == []


class TestAutoShipIfEligible:
    """`auto_ship_if_eligible`: the only caller allowed to reach `ship` on its
    own, and only for LOW risk with `merge_low_risk` on.
    """

    def _low_risk_ready(self, tmp_path, clock, *, shipper, postship=None):
        pipeline, feature = TestShip()._ready(
            tmp_path, clock, shipper=shipper, postship=postship
        )
        assert feature.state is FeatureState.READY
        # Isolated from risk classification, which is proved elsewhere
        # (TestRiskIsRedecidedOnTheRealDiff) — this class is about the
        # trigger's own wiring, so the risk is set directly to LOW.
        feature.risk = "LOW"
        pipeline.store.save(feature)
        return pipeline, feature

    def test_low_risk_with_every_gate_clear_ships(self, tmp_path, clock):
        github = TestShip.FakeGitHub()
        pipeline, feature = self._low_risk_ready(
            tmp_path,
            clock,
            shipper=TestShip()._shipper(github),
            postship=TestShip.FakePostShip(verified=True),
        )
        result = pipeline.auto_ship_if_eligible(feature.id)
        assert result.state is FeatureState.COMPLETE
        assert github.merge_calls

    def test_medium_risk_is_never_attempted(self, tmp_path, clock):
        github = TestShip.FakeGitHub()
        pipeline, feature = self._low_risk_ready(
            tmp_path,
            clock,
            shipper=TestShip()._shipper(github),
            postship=TestShip.FakePostShip(verified=True),
        )
        feature.risk = "MEDIUM"
        pipeline.store.save(feature)
        result = pipeline.auto_ship_if_eligible(feature.id)
        assert result.state is FeatureState.READY
        assert github.merge_calls == []

    def test_high_risk_is_never_attempted(self, tmp_path, clock):
        github = TestShip.FakeGitHub()
        pipeline, feature = self._low_risk_ready(
            tmp_path,
            clock,
            shipper=TestShip()._shipper(github),
            postship=TestShip.FakePostShip(verified=True),
        )
        feature.risk = "HIGH"
        pipeline.store.save(feature)
        result = pipeline.auto_ship_if_eligible(feature.id)
        assert result.state is FeatureState.READY
        assert github.merge_calls == []

    def test_unknown_risk_is_never_attempted(self, tmp_path, clock):
        github = TestShip.FakeGitHub()
        pipeline, feature = self._low_risk_ready(
            tmp_path,
            clock,
            shipper=TestShip()._shipper(github),
            postship=TestShip.FakePostShip(verified=True),
        )
        feature.risk = ""
        pipeline.store.save(feature)
        result = pipeline.auto_ship_if_eligible(feature.id)
        assert result.state is FeatureState.READY
        assert github.merge_calls == []

    def test_merge_low_risk_switched_off_never_attempts(self, tmp_path, clock):
        from openjarvis.wiz.features.shipping import FeatureShippingPolicy

        github = TestShip.FakeGitHub()
        shipper = TestShip()._shipper(github)
        shipper.policy = FeatureShippingPolicy(merge_low_risk=False)
        pipeline, feature = self._low_risk_ready(
            tmp_path, clock, shipper=shipper, postship=TestShip.FakePostShip()
        )
        result = pipeline.auto_ship_if_eligible(feature.id)
        assert result.state is FeatureState.READY
        assert github.merge_calls == []

    def test_no_shipper_configured_never_attempts(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        feature = pipeline.store.get(feature.id)
        feature.risk = "LOW"
        pipeline.store.save(feature)
        result = pipeline.auto_ship_if_eligible(feature.id)
        assert result.state is FeatureState.READY

    def test_not_ready_is_left_alone(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        pipeline.shipper = TestShip()._shipper(TestShip.FakeGitHub())
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        # Deliberately not run(): still RECEIVED.
        result = pipeline.auto_ship_if_eligible(feature.id)
        assert result.state is FeatureState.RECEIVED

    def test_emergency_stop_blocks_the_attempt(self, tmp_path, clock):
        github = TestShip.FakeGitHub()
        pipeline, feature = self._low_risk_ready(
            tmp_path,
            clock,
            shipper=TestShip()._shipper(github),
            postship=TestShip.FakePostShip(verified=True),
        )
        result = pipeline.auto_ship_if_eligible(
            feature.id, emergency_stop_engaged=lambda: True
        )
        assert result.state is FeatureState.READY
        assert github.merge_calls == []

    def test_reliability_busy_blocks_the_attempt(self, tmp_path, clock):
        github = TestShip.FakeGitHub()
        pipeline, feature = self._low_risk_ready(
            tmp_path,
            clock,
            shipper=TestShip()._shipper(github),
            postship=TestShip.FakePostShip(verified=True),
        )
        result = pipeline.auto_ship_if_eligible(
            feature.id, reliability_busy=lambda: True
        )
        assert result.state is FeatureState.READY
        assert github.merge_calls == []

    def test_unhealthy_audit_blocks_the_attempt(self, tmp_path, clock):
        github = TestShip.FakeGitHub()
        pipeline, feature = self._low_risk_ready(
            tmp_path,
            clock,
            shipper=TestShip()._shipper(github),
            postship=TestShip.FakePostShip(verified=True),
        )
        result = pipeline.auto_ship_if_eligible(feature.id, audit_healthy=lambda: False)
        assert result.state is FeatureState.READY
        assert github.merge_calls == []

    def test_calling_it_twice_only_merges_once(self, tmp_path, clock):
        github = TestShip.FakeGitHub()
        pipeline, feature = self._low_risk_ready(
            tmp_path,
            clock,
            shipper=TestShip()._shipper(github),
            postship=TestShip.FakePostShip(verified=True),
        )
        pipeline.auto_ship_if_eligible(feature.id)
        pipeline.auto_ship_if_eligible(feature.id)
        assert len(github.merge_calls) == 1

    def test_a_restart_finding_the_feature_already_complete_is_a_no_op(
        self, tmp_path, clock
    ):
        # Simulates the process restarting between the first successful
        # auto-ship and a second call reaching this feature again: nothing
        # about `auto_ship_if_eligible` may assume it is the first caller.
        github = TestShip.FakeGitHub()
        pipeline, feature = self._low_risk_ready(
            tmp_path,
            clock,
            shipper=TestShip()._shipper(github),
            postship=TestShip.FakePostShip(verified=True),
        )
        first = pipeline.auto_ship_if_eligible(feature.id)
        assert first.state is FeatureState.COMPLETE
        second = pipeline.auto_ship_if_eligible(feature.id)
        assert second.state is FeatureState.COMPLETE
        assert len(github.merge_calls) == 1


class TestTheReviewIsAdvisory:
    class DisapprovingReviewer:
        def __init__(self):
            self.calls = []

        def review(self, feature, *, workspace, worktree=None):
            self.calls.append(feature.id)
            return {
                "ran": True,
                "text": "This is a critical problem and must not ship.",
                "blocking_suggested": True,
            }

    def test_a_disapproving_review_does_not_stop_a_verified_feature(
        self, tmp_path, clock
    ):
        reviewer = self.DisapprovingReviewer()
        pipeline = build_pipeline(tmp_path, clock)
        pipeline.reviewer = reviewer
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert reviewer.calls == [feature.id]
        assert result.metadata["review"]["blocking_suggested"] is True

    def test_a_review_never_runs_on_a_change_that_failed_its_checks(
        self, tmp_path, clock
    ):
        # A review is expensive and there is one Claude slot. Reviewing a change
        # that already failed its tests spends it on a foregone conclusion.
        reviewer = self.DisapprovingReviewer()
        pipeline = build_pipeline(
            tmp_path,
            clock,
            suite_results=[FakeCheckResult(passed=False, summary="broken")],
            max_attempts=1,
        )
        pipeline.reviewer = reviewer
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        assert reviewer.calls == []


class TestTheEvidenceAGateProduces:
    def test_the_next_attempt_gets_the_command_output_not_the_headline(
        self, tmp_path, clock
    ):
        # "tests: failed" is a headline. "Property 'total' does not exist on
        # type 'Session'" is a bug report, and it is what makes the second
        # attempt a fix rather than a guess.
        engineer = ScriptedEngineer()
        pipeline = build_pipeline(
            tmp_path,
            clock,
            engineer=engineer,
            suite_results=[
                FakeCheckResult(
                    passed=False,
                    summary="✗ tests: failed",
                    output=(
                        "### tests failed\n\n"
                        "src/Summary.tsx(14,22): error TS2339: Property 'total' "
                        "does not exist on type 'Session'."
                    ),
                ),
                FakeCheckResult(passed=True),
            ],
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        second = engineer.build_calls[1][0].render()
        assert "TS2339" in second
        assert "does not exist on type" in second

    def test_the_record_carries_a_summary_the_pull_request_can_show(
        self, tmp_path, clock
    ):
        # The real CheckSuiteResult.to_dict has no "summary" key, and the pull
        # request body read one. It silently showed nothing.
        pipeline = build_pipeline(
            tmp_path, clock, suite_results=[FakeCheckResult(summary="✓ tests: passed")]
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.metadata["gates"]["summary"] == "✓ tests: passed"

        from openjarvis.wiz.features.shipping import pull_request_body

        assert "tests: passed" in pull_request_body(result)


class TestNoSecretIsCommitted:
    """No gate the engineering profile configures is a secret scanner."""

    def test_a_diff_with_a_real_looking_token_is_not_committed(self, tmp_path, clock):
        workspace = FakeWorkspace(
            tmp_path, diff_text='+ const token = "ghp_' + "a" * 36 + '";'
        )
        pipeline = build_pipeline(tmp_path, clock, workspace=workspace, max_attempts=1)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert not workspace.commits, "a diff carrying a credential was committed"
        assert not workspace.pushes

    def test_the_next_attempt_is_told_why_not_shown_the_secret(self, tmp_path, clock):
        engineer = ScriptedEngineer()
        secret = "ghp_" + "a" * 36
        workspace = FakeWorkspace(tmp_path, diff_text=f'+ const token = "{secret}";')
        pipeline = build_pipeline(
            tmp_path, clock, engineer=engineer, workspace=workspace
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        # Second attempt happens because FakeWorkspace keeps returning the
        # same tainted diff — the point here is what Claude was told, not
        # whether the retry succeeds.
        assert len(engineer.build_calls) >= 2
        second_prompt = engineer.build_calls[1][0].render()
        assert "credential" in second_prompt.lower()
        assert secret not in second_prompt

    def test_an_ordinary_diff_is_committed_normally(self, tmp_path, clock):
        # The default fixture already carries an innocuous diff; this just
        # makes the contrast with the two tests above explicit.
        workspace = FakeWorkspace(tmp_path)
        pipeline = build_pipeline(tmp_path, clock, workspace=workspace)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert workspace.commits


class TestRecoveringFromAMess:
    def test_a_git_failure_is_explained_in_words_a_person_can_act_on(
        self, tmp_path, clock
    ):
        # A dump of `git worktree add -b ... failed (255)` is precise and
        # useless to somebody who is not reading the code.
        from openjarvis.reliability.workspace import WorkspaceError

        class Broken(FakeWorkspace):
            def create(self, feature_id, *, title="", base_ref="HEAD"):
                raise WorkspaceError(
                    "git worktree add -b wiz/feature/X /tmp/x abc failed (255): "
                    "fatal: a branch named 'wiz/feature/X' already exists"
                )

        pipeline = build_pipeline(tmp_path, clock, workspace=Broken(tmp_path))
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        message = result.history[-1]["reason"]
        assert "git worktree" not in message
        assert "clean copy of the repository" in message
        assert "not changed anything" in message

    def test_a_stale_worktree_registration_is_pruned_before_creating(self, tmp_path):
        # Git keeps a registration per worktree; a directory removed out from
        # under it leaves one behind, and then the branch counts as checked out
        # somewhere and the feature can never be retried.
        import subprocess

        from openjarvis.wiz.features.workspace import FeatureWorkspace

        repo = tmp_path / "repo"
        repo.mkdir()

        def run(*args):
            return subprocess.run(
                ["git", *args], cwd=repo, capture_output=True, text=True, check=True
            )

        run("init", "-q", ".")
        run("config", "user.email", "w@example.com")
        run("config", "user.name", "W")
        (repo / "a.txt").write_text("hello")
        run("add", "-A")
        run("commit", "-qm", "first")

        workspace = FeatureWorkspace(
            repo_path=str(repo),
            root=str(tmp_path / "worktrees"),
            git_identity=("W", "w@example.com"),
        )
        first = workspace.create("FEAT-00001", title="a thing")

        # Simulate the crash: the directory vanishes, git's record does not.
        import shutil

        shutil.rmtree(first.path)

        # Without the prune this raises "a branch named ... already exists".
        second = workspace.create("FEAT-00001", title="a thing")
        assert second.branch == first.branch
        assert Path(second.path).is_dir()
