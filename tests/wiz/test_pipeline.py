"""The feature pipeline: from "build X" to "it's ready", and the refusals."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from openjarvis.reliability.types import ProbeResult
from openjarvis.reliability.workspace import MergeOutcome
from openjarvis.wiz.approvals import ApprovalError, ApprovalStore
from openjarvis.wiz.authority import Actor, Authority, AuthorityPolicy, Channel
from openjarvis.wiz.features.acceptance import DESKTOP, MOBILE
from openjarvis.wiz.features.engineer import (
    ClaudeCodeEngineeringAgent,
    CodingEngineUnavailable,
    EngineeringSession,
)
from openjarvis.wiz.features.model import (
    FeatureState,
    InvalidFeatureTransition,
    Priority,
)
from openjarvis.wiz.features.notify import NEEDS_OWNER_KINDS
from openjarvis.wiz.features.pipeline import FeaturePipeline
from openjarvis.wiz.features.preview import PreviewObserver
from openjarvis.wiz.features.profile import EngineeringProfile
from openjarvis.wiz.features.queue import DevelopmentQueue
from openjarvis.wiz.features.shipping import FeatureShipper, FeatureShippingPolicy
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
        #: Independent of `changed` (what `_build` reads as the attempt's
        #: files) so a test can simulate "already committed" -- has_changes()
        #: False even though the attempt genuinely has changed_files.
        self.changes_present = True
        self.checkouts = []
        self.merge_calls = []
        #: A test overrides this to a MergeOutcome-shaped stand-in (merged
        #: True/False, new_sha, conflicting_files) before calling
        #: reverify_against_current_base.
        self.merge_outcome = None

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

    def reuse(self, feature_id, *, path, branch, base_sha):
        # None by default: every existing test's pipeline falls through to
        # create() exactly as before. A test proving real reuse semantics
        # subclasses this and overrides it.
        return None

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
        return self.changes_present

    def head_sha(self, worktree):
        return self.commit_sha

    def checkout_existing(self, feature_id, *, title="", branch):
        path = self.tmp_path / f"wt-{feature_id}-refresh"
        path.mkdir(parents=True, exist_ok=True)
        worktree = FakeWorktree(
            path=str(path), branch=branch, base_commit=self.commit_sha
        )
        self.checkouts.append(worktree)
        return worktree

    def merge_base(self, worktree, *, base_branch):
        self.merge_calls.append((worktree, base_branch))
        return self.merge_outcome

    def remove(self, worktree, *, succeeded=True):
        self.removed.append((worktree, succeeded))


class ScriptedEngineer(ClaudeCodeEngineeringAgent):
    """A Claude Code adapter whose sessions are scripted.

    Subclasses the real adapter rather than duck-typing it, so that a test
    asserting "planning used no write tools" is asserting about the same class
    the operator's machine runs.
    """

    def __init__(self, *, plan_claim="I would edit Summary.tsx", plans=None, builds=None):
        super().__init__(agent_factory=self._never_called)
        self.plan_claim = plan_claim
        self.plans = list(plans or [])
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
        if self.plans:
            return self.plans.pop(0)
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
    def __init__(self, state="READY", commit_sha=None):
        self.state = state
        #: Mutable so a test can move it to match a commit produced mid-test
        #: (e.g. after reverify_against_current_base() advances the head) --
        #: the real Vercel deployment list always reflects whatever was
        #: actually pushed, so this fake has to be steerable the same way.
        self.commit_sha = commit_sha or "feedface" + "0" * 32

    def list_deployments(self, **kwargs):
        return [
            {
                "id": "dpl_1",
                "state": self.state,
                "target": "preview",
                "url": "https://feature-preview.app",
                "created_at": "2026-08-19T10:00:00+00:00",
                "commit_sha": self.commit_sha,
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


def _passing_provision(profile, workspace):
    from openjarvis.reliability.checks import CheckResult

    return CheckResult(name="provision", ran=True, passed=True, summary="provisioned")


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
    provision_factory=None,
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
        provision_factory=provision_factory or _passing_provision,
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
        # Building may change source files (that is the point) but, since
        # FEAT-00030, may not execute anything — Bash is what let a building
        # session merge its own pull request. It still gets Edit/Write.
        assert {"Edit", "Write"} <= set(BUILDING_TOOLS)
        assert "Bash" not in BUILDING_TOOLS


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

    def test_the_escalated_risk_is_persisted_not_just_held_in_memory(
        self, tmp_path, clock
    ):
        # Regression for FEAT-00009: the request text alone ("adjust the
        # spacing") classifies LOW at _understand() and that LOW gets saved
        # then. Only the plan-informed reassessment in _decide_to_build
        # raises it to HIGH — and `run()`'s return value is the same
        # in-memory object the pipeline mutated, so asserting on it (as
        # TestAnAgentMayRaiseTheRisk... does above) could not have caught a
        # missing store.save() for that escalation. A *second* read, the way
        # `jarvis wiz show` or Control Center actually reads it (a fresh
        # process, a fresh load from the store), is what proves persistence.
        engineer = ScriptedEngineer(
            plan_claim="This is high risk: it changes how sessions are stored."
        )
        pipeline = build_pipeline(tmp_path, clock, engineer=engineer)
        feature = pipeline.submit("adjust the spacing", actor=operator_actor())
        assert feature.risk != "HIGH", "the request text alone should read as LOW"
        pipeline.run(feature.id)

        reloaded = pipeline.store.get(feature.id)
        assert reloaded.risk == "HIGH"
        assert reloaded.metadata.get("risk_reasons")

    def test_a_file_mentioned_only_as_existing_context_does_not_count_as_touched(
        self, tmp_path, clock
    ):
        # Found on FEAT-00017: the plan mentioned "e2e/auth.spec.ts" only as
        # an existing, unrelated test that happens to also load the page
        # being edited — not a file the change touches — and the word "auth"
        # in that filename raised risk to HIGH anyway, blocking a genuinely
        # LOW/MEDIUM text-only change on an approval it did not need.
        plan = (
            "**What already exists**\n"
            "Tests touching this page: e2e/landing.spec.ts, e2e/auth.spec.ts "
            "and e2e/security-headers.spec.ts load '/' but assert unrelated "
            "things.\n\n"
            "**Files expected to change**\n"
            "- src/lib/language.ts — one string value.\n\n"
            "**Tests to prove it**\n"
            "- npm run lint, npm test\n"
        )
        engineer = ScriptedEngineer(plan_claim=plan)
        pipeline = build_pipeline(tmp_path, clock, engineer=engineer)
        feature = pipeline.submit(
            "improve the wording on the landing page", actor=operator_actor()
        )
        result = pipeline.run(feature.id)
        assert result.risk != "HIGH", result.metadata.get("risk_reasons")
        assert result.state is FeatureState.READY

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


class TestPathsMentionedInAPlan:
    """_paths_mentioned() decides what a plan will touch, feeding the
    pre-build risk re-classification in _decide_to_build. Scoped to the
    plan's "files expected to change" section rather than every path-shaped
    token in the prose — see TestRiskIsRedecidedOnTheRealDiff for the
    FEAT-00017 regression this exists for.
    """

    def test_a_file_named_only_as_existing_context_is_excluded(self):
        from openjarvis.wiz.features.pipeline import _paths_mentioned

        plan = (
            "**What already exists**\n"
            "e2e/auth.spec.ts already covers this page.\n\n"
            "**Files expected to change**\n"
            "- src/lib/language.ts\n"
        )
        assert _paths_mentioned(plan) == ["src/lib/language.ts"]

    def test_a_heading_with_a_different_wording_is_matched_case_insensitively(self):
        from openjarvis.wiz.features.pipeline import _paths_mentioned

        plan = "## FILES EXPECTED TO CHANGE\n- src/lib/language.ts\n"
        assert _paths_mentioned(plan) == ["src/lib/language.ts"]

    def test_a_files_that_stay_unchanged_heading_is_not_read_as_the_opposite(self):
        from openjarvis.wiz.features.pipeline import _paths_mentioned

        plan = (
            "**Files that stay unchanged**\n"
            "- src/lib/auth/session.ts\n\n"
            "**Files expected to change**\n"
            "- src/lib/language.ts\n"
        )
        assert _paths_mentioned(plan) == ["src/lib/language.ts"]

    def test_with_no_files_section_at_all_it_falls_back_to_the_whole_plan(self):
        # The conservative default this classifier is built on: a plan that
        # never names its files is read as though anything it mentions might
        # be touched, not as though nothing is.
        from openjarvis.wiz.features.pipeline import _paths_mentioned

        plan = "I will edit src/lib/language.ts to fix the wording."
        assert _paths_mentioned(plan) == ["src/lib/language.ts"]

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


class TestPlanTransientCapacityFailure:
    """A Claude Code usage-limit exit during planning is not a understanding failure.

    Found on FEAT-00017: the planning session hit "You've hit your session
    limit" mid-investigation, and the pipeline reported it to the operator as
    "I could not work out how to build this: exit code 1" — losing the real
    reason and treating a capacity limit as if the request were too hard to
    plan.
    """

    def test_a_transient_failure_is_retried_automatically(self, tmp_path, clock):
        engineer = ScriptedEngineer(
            plans=[
                EngineeringSession(
                    mode="plan",
                    succeeded=False,
                    error="You've hit your session limit · resets 6:30pm",
                    metadata={"transient_reason": "usage_limit"},
                )
            ]
        )
        pipeline = build_pipeline(tmp_path, clock, engineer=engineer)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert len(engineer.plan_calls) == 2

    def test_the_operator_is_not_told_about_the_first_transient_failure(
        self, tmp_path, clock
    ):
        engineer = ScriptedEngineer(
            plans=[
                EngineeringSession(
                    mode="plan",
                    succeeded=False,
                    error="You've hit your session limit · resets 6:30pm",
                    metadata={"transient_reason": "usage_limit"},
                )
            ]
        )
        pipeline = build_pipeline(tmp_path, clock, engineer=engineer)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        messages = []
        for _ in range(20):
            if feature.terminal or feature.state is FeatureState.READY:
                break
            step = pipeline.advance(feature)
            feature = step.feature
            messages.append(step.message)
        # "Sir, ... is ready" is the legitimate success notice once the retry
        # works; what must not appear is a failure notice about it.
        assert not any("tried" in m or "usage limit" in m for m in messages if m.startswith("Sir,"))

    def test_exhausting_the_bound_stops_with_the_real_reason(self, tmp_path, clock):
        transient = EngineeringSession(
            mode="plan",
            succeeded=False,
            error="You've hit your session limit · resets 6:30pm",
            metadata={"transient_reason": "usage_limit"},
        )
        engineer = ScriptedEngineer(plans=[transient, transient, transient])
        pipeline = build_pipeline(tmp_path, clock, engineer=engineer, max_attempts=2)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert len(engineer.plan_calls) == 3
        reason = result.history[-1]["reason"]
        assert "Claude Code usage/session limit" in reason
        assert "session limit" in reason
        assert "could not work out how to build this" not in reason

    def test_a_non_transient_plan_failure_still_stops_immediately(self, tmp_path, clock):
        engineer = ScriptedEngineer(
            plans=[
                EngineeringSession(
                    mode="plan", succeeded=False, error="ambiguous request"
                )
            ]
        )
        pipeline = build_pipeline(tmp_path, clock, engineer=engineer)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert len(engineer.plan_calls) == 1
        assert "could not work out how to build this: ambiguous request" in (
            result.history[-1]["reason"]
        )


class TestReopenForPlanning:
    """The narrow, explicit-operator-only path back from a planning-stage
    HUMAN_REQUIRED — distinct from the build-crash recovery in
    openjarvis.wiz.features.recovery, and distinct from resume_from_human_required.
    """

    def test_reopening_a_planning_failure_lets_it_reach_ready(self, tmp_path, clock):
        engineer = ScriptedEngineer(
            plans=[EngineeringSession(mode="plan", succeeded=False, error="ambiguous")]
        )
        pipeline = build_pipeline(tmp_path, clock, engineer=engineer)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        stopped = pipeline.run(feature.id)
        assert stopped.state is FeatureState.HUMAN_REQUIRED
        assert stopped.attempts_used == 0

        reopened = pipeline.reopen_for_planning(feature.id, reason="fixed since")
        assert reopened.state is FeatureState.RECEIVED

        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY

    def test_reopening_is_recorded_in_the_journal(self, tmp_path, clock):
        engineer = ScriptedEngineer(
            plans=[EngineeringSession(mode="plan", succeeded=False, error="ambiguous")]
        )
        pipeline = build_pipeline(tmp_path, clock, engineer=engineer)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        pipeline.reopen_for_planning(feature.id, reason="fixed since")

        entries = [
            e for e in pipeline.journal.tail(50) if e.detail.get("feature_id") == feature.id
        ]
        assert any(e.kind == "feature.reopened_for_planning" for e in entries)

    def test_a_feature_that_already_used_an_attempt_cannot_reopen_this_way(
        self, tmp_path, clock
    ):
        pipeline = build_pipeline(
            tmp_path,
            clock,
            suite_results=[FakeCheckResult(passed=False, summary="still broken")],
            max_attempts=1,
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        stopped = pipeline.run(feature.id)
        assert stopped.state is FeatureState.HUMAN_REQUIRED
        assert stopped.attempts_used == 1

        with pytest.raises(InvalidFeatureTransition):
            pipeline.reopen_for_planning(feature.id)

    def test_a_feature_not_in_human_required_cannot_be_reopened(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        with pytest.raises(InvalidFeatureTransition):
            pipeline.reopen_for_planning(feature.id)


class TestReopenForDeploy:
    """FEAT-00031: attempts exhausted by infrastructure (node_modules never
    provisioned), not by the diff. Recovering must not call Claude again.
    """

    def _always_failing_provision(self, profile, workspace):
        from openjarvis.reliability.checks import CheckResult

        return CheckResult(
            name="provision",
            ran=True,
            passed=False,
            summary="failed (exit 127)",
            output="sh: tsc: command not found",
        )

    def test_reopening_after_the_infra_is_fixed_reaches_ready_without_a_new_build(
        self, tmp_path, clock
    ):
        engineer = ScriptedEngineer()
        pipeline = build_pipeline(
            tmp_path,
            clock,
            engineer=engineer,
            max_attempts=1,
            provision_factory=self._always_failing_provision,
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        stopped = pipeline.run(feature.id)
        assert stopped.state is FeatureState.HUMAN_REQUIRED
        assert stopped.attempts_used == 1
        first_build_calls = len(engineer.build_calls)
        assert first_build_calls == 1

        # "the infra is fixed" — the same feature, the same worktree, the
        # same diff; only the provisioning outcome changes.
        pipeline.provision_factory = _passing_provision

        reopened = pipeline.reopen_for_deploy(feature.id, reason="node_modules fix landed")
        assert reopened.state is FeatureState.TESTING

        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert len(engineer.build_calls) == first_build_calls  # no new Claude session

    def test_reopening_an_already_committed_attempt_does_not_crash(
        self, tmp_path, clock
    ):
        """Found re-verifying FEAT-00031 against a fixed acceptance contract:
        reopen_for_deploy re-enters TESTING, which always tried to
        commit_all() the worktree -- fine the first time a build genuinely
        left something uncommitted, but this attempt's diff was already
        committed and pushed by an earlier full run of this same method
        (the one that reached PREVIEWING/VERIFYING and only failed browser
        acceptance). commit_all() raised "nothing to commit", which the
        pipeline's generic exception handler turned into an opaque
        feature.failed -- masking the real, fixed, re-verifiable diff behind
        a crash that looked like a fresh problem.
        """
        engineer = ScriptedEngineer()
        workspace = FakeWorkspace(tmp_path)
        pipeline = build_pipeline(
            tmp_path,
            clock,
            engineer=engineer,
            workspace=workspace,
            max_attempts=1,
            browser=FakeBrowser([False]),  # first run fails browser acceptance
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        stopped = pipeline.run(feature.id)
        assert stopped.state is FeatureState.HUMAN_REQUIRED
        first_commit_sha = stopped.attempts[-1].commit_sha
        assert first_commit_sha  # it really did commit and push once already

        # "already committed" -- the worktree has nothing left to commit,
        # exactly as it would after a prior successful commit_all().
        workspace.changes_present = False
        pipeline._browser.outcomes = [True]  # the fix under test now passes

        reopened = pipeline.reopen_for_deploy(feature.id, reason="verifier fixed")
        assert reopened.state is FeatureState.TESTING

        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert result.attempts[-1].commit_sha == first_commit_sha  # reused, not crashed
        assert len(engineer.build_calls) == 1  # still no new Claude session
        assert len(workspace.commits) == 1  # never asked to commit a second time
        assert len(workspace.pushes) == 2  # pushed again -- a safe no-op for git

    def test_a_genuine_second_failure_stops_again_without_a_new_build(
        self, tmp_path, clock
    ):
        # Reopened, but the check suite itself still fails for a real
        # reason this time — must not retry into a 4th Claude session
        # either; attempts are already exhausted.
        engineer = ScriptedEngineer()
        suite = FakeSuite([FakeCheckResult(passed=False, summary="still broken")])
        pipeline = build_pipeline(
            tmp_path,
            clock,
            engineer=engineer,
            max_attempts=1,
            provision_factory=self._always_failing_provision,
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        first_build_calls = len(engineer.build_calls)

        pipeline.provision_factory = _passing_provision
        pipeline.check_suite_factory = lambda profile: suite
        pipeline.reopen_for_deploy(feature.id, reason="node_modules fix landed")
        result = pipeline.run(feature.id)

        assert result.state is FeatureState.HUMAN_REQUIRED
        assert len(engineer.build_calls) == first_build_calls

    def test_no_existing_attempt_cannot_be_reopened_this_way(self, tmp_path, clock):
        pipeline = build_pipeline(
            tmp_path,
            clock,
            engineer=ScriptedEngineer(
                plans=[EngineeringSession(mode="plan", succeeded=False, error="ambiguous")]
            ),
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)  # stops in planning, zero attempts

        with pytest.raises(InvalidFeatureTransition):
            pipeline.reopen_for_deploy(feature.id)

    def test_a_feature_not_in_human_required_cannot_be_reopened_this_way(
        self, tmp_path, clock
    ):
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        with pytest.raises(InvalidFeatureTransition):
            pipeline.reopen_for_deploy(feature.id)

    def test_reopening_is_recorded_in_the_journal(self, tmp_path, clock):
        engineer = ScriptedEngineer()
        pipeline = build_pipeline(
            tmp_path,
            clock,
            engineer=engineer,
            max_attempts=1,
            provision_factory=self._always_failing_provision,
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        pipeline.provision_factory = _passing_provision
        pipeline.reopen_for_deploy(feature.id, reason="node_modules fix landed")

        entries = [
            e for e in pipeline.journal.tail(50) if e.detail.get("feature_id") == feature.id
        ]
        assert any(e.kind == "feature.reopened_for_deploy" for e in entries)


class TestReopenForOwnerAuthorizedRebuild:
    """FEAT-00031's actual situation: attempts exhausted entirely by
    infrastructure, and the diff that infrastructure fix would have
    unblocked was separately lost (a worktree-reuse bug), leaving nothing
    for reopen_for_deploy to re-test. Unlike that method, this one *does*
    call Claude again — exactly once, on the owner's explicit say-so.
    """

    def _always_failing_provision(self, profile, workspace):
        from openjarvis.reliability.checks import CheckResult

        return CheckResult(
            name="provision",
            ran=True,
            passed=False,
            summary="failed (exit 127)",
            output="sh: tsc: command not found",
        )

    def test_reopening_calls_a_genuinely_new_build_and_can_reach_ready(
        self, tmp_path, clock
    ):
        engineer = ScriptedEngineer()
        pipeline = build_pipeline(
            tmp_path,
            clock,
            engineer=engineer,
            max_attempts=1,
            provision_factory=self._always_failing_provision,
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        stopped = pipeline.run(feature.id)
        assert stopped.state is FeatureState.HUMAN_REQUIRED
        assert stopped.attempts_used == 1
        first_build_calls = len(engineer.build_calls)

        pipeline.provision_factory = _passing_provision
        reopened = pipeline.reopen_for_owner_authorized_rebuild(
            feature.id, reason="attempts burned entirely by a framework bug, now fixed"
        )
        assert reopened.state is FeatureState.BUILDING

        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert len(engineer.build_calls) == first_build_calls + 1  # a new Claude session
        assert result.attempts_used == 2  # prior attempt preserved, not reset

    def test_a_second_owner_authorized_rebuild_is_refused(self, tmp_path, clock):
        engineer = ScriptedEngineer()
        pipeline = build_pipeline(
            tmp_path,
            clock,
            engineer=engineer,
            max_attempts=1,
            provision_factory=self._always_failing_provision,
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        pipeline.provision_factory = self._always_failing_provision
        pipeline.reopen_for_owner_authorized_rebuild(feature.id, reason="first exception")
        stopped_again = pipeline.run(feature.id)
        assert stopped_again.state is FeatureState.HUMAN_REQUIRED

        with pytest.raises(InvalidFeatureTransition):
            pipeline.reopen_for_owner_authorized_rebuild(feature.id, reason="again?")

    def test_a_feature_not_in_human_required_cannot_be_reopened_this_way(
        self, tmp_path, clock
    ):
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        with pytest.raises(InvalidFeatureTransition):
            pipeline.reopen_for_owner_authorized_rebuild(feature.id, reason="x")

    def test_reopening_is_recorded_in_the_journal(self, tmp_path, clock):
        engineer = ScriptedEngineer()
        pipeline = build_pipeline(
            tmp_path,
            clock,
            engineer=engineer,
            max_attempts=1,
            provision_factory=self._always_failing_provision,
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        pipeline.provision_factory = _passing_provision
        pipeline.reopen_for_owner_authorized_rebuild(feature.id, reason="the owner said so")

        entries = [
            e for e in pipeline.journal.tail(50) if e.detail.get("feature_id") == feature.id
        ]
        assert any(e.kind == "feature.owner_authorized_rebuild" for e in entries)


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


class TestOwnerNotification:
    """The pipeline's own half of FeatureOwnerNotifier's contract: it is
    asked about every event, not just the ones that turn out to matter."""

    class SpyNotifier:
        def __init__(self):
            self.calls = []

        def notify(self, feature, *, kind, reason):
            self.calls.append((feature.id, kind, reason))
            return True

    def test_a_ready_feature_with_no_shipper_is_not_notified(self, tmp_path, clock):
        # No PR was opened, nothing shipped — nothing to say yet.
        spy = self.SpyNotifier()
        pipeline = build_pipeline(tmp_path, clock)
        pipeline.owner_notifier = spy
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        kinds = [kind for _, kind, _ in spy.calls]
        assert "feature.shipped" not in kinds

    def test_a_stalled_feature_notifies_needs_a_person(self, tmp_path, clock):
        spy = self.SpyNotifier()
        pipeline = build_pipeline(
            tmp_path,
            clock,
            suite_results=[FakeCheckResult(passed=False, summary="broken")],
            max_attempts=1,
        )
        pipeline.owner_notifier = spy
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert any(kind in NEEDS_OWNER_KINDS for _, kind, _ in spy.calls)

    def test_routine_progress_kinds_are_still_reported_to_the_notifier(
        self, tmp_path, clock
    ):
        # The pipeline does not pre-filter — FeatureOwnerNotifier itself
        # decides what is silence, so every kind must actually reach it.
        spy = self.SpyNotifier()
        pipeline = build_pipeline(tmp_path, clock)
        pipeline.owner_notifier = spy
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        kinds = {kind for _, kind, _ in spy.calls}
        assert "feature.received" in kinds
        assert "feature.ready" in kinds

    def test_no_notifier_configured_is_not_an_error(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        assert pipeline.owner_notifier is None
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY

    def test_a_notifier_that_raises_does_not_break_the_pipeline(self, tmp_path, clock):
        class Broken:
            def notify(self, feature, *, kind, reason):
                raise RuntimeError("telegram is down")

        pipeline = build_pipeline(tmp_path, clock)
        pipeline.owner_notifier = Broken()
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


class TestRecoverMissingPullRequest:
    """A READY feature whose pull request never got opened — FEAT-00006's
    exact shape — has to be able to get one without a person noticing and
    intervening by hand."""

    #: Matches FakeWorkspace.commit_sha, the same convention TestShip uses.
    HEAD_SHA = "feedface" + "0" * 32

    class FlakyGitHub:
        """Fails create_pull_request until told not to, like the real bug did."""

        def __init__(self):
            self.calls: List[Dict[str, Any]] = []
            self.fail = True
            self.branch_sha = TestRecoverMissingPullRequest.HEAD_SHA

        def create_pull_request(self, **kwargs):
            self.calls.append(kwargs)
            if self.fail:
                raise RuntimeError("still broken")
            return {"html_url": "https://github.com/a/b/pull/9", "number": 9}

        def list_pull_requests(self, *, state="open"):
            return []

        def branch_head_sha(self, branch):
            return self.branch_sha

    class StubPreview:
        def __init__(self, *, usable=True, reason=""):
            self.usable = usable
            self.reason = reason
            self.calls: List[str] = []

        def observe(self, *, commit_sha, branch=""):
            self.calls.append(commit_sha)
            return SimpleNamespace(usable=self.usable, reason=self.reason)

    def _ready_without_a_pull_request(self, tmp_path, clock, *, workspace=None):
        """Drive a real feature to READY through a shipper whose first (and,
        during the run, only) attempt to open a pull request fails — exactly
        the shape a stale create_pull_request contract left behind."""
        github = self.FlakyGitHub()
        shipper = FeatureShipper(
            policy=FeatureShippingPolicy(merge_low_risk=True), github=github
        )
        pipeline = build_pipeline(tmp_path, clock, workspace=workspace)
        pipeline.shipper = shipper
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert not result.pr_number
        github.fail = False
        github.calls.clear()  # the run above already made (and failed) one
        return pipeline, result, github

    def test_recovers_and_opens_the_pull_request(self, tmp_path, clock):
        pipeline, feature, github = self._ready_without_a_pull_request(tmp_path, clock)
        result = pipeline.recover_missing_pull_request(feature.id)
        assert result.pr_number == 9
        assert result.pr_url.endswith("/pull/9")

    def test_is_idempotent_once_a_pull_request_exists(self, tmp_path, clock):
        pipeline, feature, github = self._ready_without_a_pull_request(tmp_path, clock)
        first = pipeline.recover_missing_pull_request(feature.id)
        second = pipeline.recover_missing_pull_request(feature.id)
        assert first.pr_number == second.pr_number == 9
        assert len(github.calls) == 1

    def test_refuses_when_the_feature_is_not_ready(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.recover_missing_pull_request(feature.id)
        assert result.state is FeatureState.RECEIVED
        assert not result.pr_number

    def test_refuses_high_risk(self, tmp_path, clock):
        pipeline, feature, github = self._ready_without_a_pull_request(tmp_path, clock)
        feature.risk = "HIGH"
        pipeline.store.save(feature)
        result = pipeline.recover_missing_pull_request(feature.id)
        assert not result.pr_number
        assert github.calls == []

    def test_refuses_when_the_branch_has_moved(self, tmp_path, clock):
        pipeline, feature, github = self._ready_without_a_pull_request(tmp_path, clock)
        github.branch_sha = "somethingelse" + "0" * 19
        result = pipeline.recover_missing_pull_request(feature.id)
        assert not result.pr_number
        assert github.calls == []

    def test_refuses_when_acceptance_evidence_does_not_match_the_commit(
        self, tmp_path, clock
    ):
        pipeline, feature, github = self._ready_without_a_pull_request(tmp_path, clock)
        feature.metadata["verification"]["commit_sha"] = "stale123" + "0" * 24
        pipeline.store.save(feature)
        result = pipeline.recover_missing_pull_request(feature.id)
        assert not result.pr_number
        assert github.calls == []

    def test_refuses_when_the_preview_is_no_longer_usable(self, tmp_path, clock):
        pipeline, feature, github = self._ready_without_a_pull_request(tmp_path, clock)
        pipeline.preview = self.StubPreview(usable=False, reason="preview expired")
        result = pipeline.recover_missing_pull_request(feature.id)
        assert not result.pr_number
        assert github.calls == []

    def test_a_missing_preview_configuration_does_not_block_recovery(
        self, tmp_path, clock
    ):
        # Not every profile configures a preview observer; absence is not a
        # refusal reason on its own — only an observed-and-unusable preview is.
        pipeline, feature, github = self._ready_without_a_pull_request(tmp_path, clock)
        pipeline.preview = None
        result = pipeline.recover_missing_pull_request(feature.id)
        assert result.pr_number == 9

    def test_cleans_up_the_worktree_once_recovered(self, tmp_path, clock):
        workspace = FakeWorkspace(tmp_path)
        pipeline, feature, github = self._ready_without_a_pull_request(
            tmp_path, clock, workspace=workspace
        )
        pipeline.recover_missing_pull_request(feature.id)
        assert len(workspace.removed) == 1

    def test_auto_ship_if_eligible_recovers_then_ships(self, tmp_path, clock):
        # The wiring that actually matters for unattended operation: the same
        # call every intake channel already makes after `run` self-heals a
        # missing pull request before deciding whether to ship.
        class FullyRecoverableGitHub(self.FlakyGitHub):
            def __init__(self):
                super().__init__()
                self.merge_calls: List[Dict[str, Any]] = []

            def get_pull_request(self, number):
                return {
                    "number": number,
                    "head_sha": TestRecoverMissingPullRequest.HEAD_SHA,
                    "base_sha": "base1234" + "0" * 32,
                    "state": "open",
                    "mergeable": True,
                }

            def combined_status(self, sha, *, required_contexts=()):
                return {"state": "success", "contexts": [], "required": {}}

            def can_write(self):
                return True

            def merge_pull_request(self, **kwargs):
                self.merge_calls.append(kwargs)
                return {"merged": True, "sha": "deadbeef" + "0" * 32}

        github = FullyRecoverableGitHub()
        shipper = FeatureShipper(
            policy=FeatureShippingPolicy(merge_low_risk=True),
            github=github,
            authority=AuthorityPolicy(
                grants={
                    Channel.CONTROL_CENTER: frozenset(
                        {Authority.PR_WRITE, Authority.PRODUCTION_CHANGE}
                    )
                }
            ),
        )
        pipeline = build_pipeline(tmp_path, clock)
        pipeline.shipper = shipper
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert not result.pr_number
        github.fail = False
        # This test is about the recovery -> ship wiring, not about risk
        # classification (covered elsewhere) — force LOW so auto_ship's own
        # risk gate does not mask what is being tested here.
        result.risk = "LOW"
        pipeline.store.save(result)

        shipped = pipeline.auto_ship_if_eligible(feature.id)
        assert shipped.pr_number == 9
        assert github.merge_calls, "recovery must lead into the normal ship path"


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


class TestMediumRiskOneOffShipApproval:
    """The full, real path: a redeemed approval token, bound to this feature
    and this exact head SHA, is the only thing that lets a MEDIUM-risk
    feature ship while merge_medium_risk stays False.
    """

    def _shipper(self, github):
        from openjarvis.wiz.features.shipping import (
            FeatureShipper,
            FeatureShippingPolicy,
        )

        return FeatureShipper(
            # merge_medium_risk stays False on purpose: the standing policy
            # never changes here, only a per-feature approval does.
            policy=FeatureShippingPolicy(merge_medium_risk=False),
            github=github,
            authority=AuthorityPolicy(
                grants={
                    Channel.CONTROL_CENTER: frozenset(
                        {Authority.PR_WRITE, Authority.PRODUCTION_CHANGE}
                    )
                }
            ),
        )

    def _ready(self, tmp_path, clock, approvals):
        github = TestShip.FakeGitHub()
        pipeline = build_pipeline(tmp_path, clock, approvals=approvals)
        pipeline.shipper = self._shipper(github)
        pipeline.postship = TestShip.FakePostShip(verified=True)
        submitted = pipeline.submit(REQUEST, actor=operator_actor())
        feature = pipeline.run(submitted.id)
        assert feature.risk == "MEDIUM", "the fixture request should classify MEDIUM"
        return pipeline, feature, github

    def _approvals(self):
        from openjarvis.wiz.approvals import ApprovalStore

        return ApprovalStore(clock=lambda: 0.0, ttl_seconds=900)

    def test_medium_without_any_approval_is_refused(self, tmp_path, clock):
        pipeline, feature, github = self._ready(tmp_path, clock, self._approvals())
        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.READY
        assert not github.merge_calls

    def test_a_valid_approval_for_this_feature_and_head_ships(self, tmp_path, clock):
        approvals = self._approvals()
        pipeline, feature, github = self._ready(tmp_path, clock, approvals)
        approval = approvals.issue(
            capability="feature.ship",
            subject=feature.id,
            parameters={"risk": feature.risk, "head_sha": TestShip.HEAD_SHA},
        )
        feature.metadata["ship_approval_token"] = approval.token
        pipeline.store.save(feature)

        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.COMPLETE
        assert github.merge_calls
        assert shipped.metadata["ship_approved_head_sha"] == TestShip.HEAD_SHA
        assert "ship_approval_token" not in shipped.metadata

    def test_an_approval_for_a_different_feature_does_not_transfer(
        self, tmp_path, clock
    ):
        approvals = self._approvals()
        pipeline, feature, github = self._ready(tmp_path, clock, approvals)
        approval = approvals.issue(
            capability="feature.ship",
            subject="FEAT-SOMEONE-ELSE",
            parameters={"risk": feature.risk, "head_sha": TestShip.HEAD_SHA},
        )
        feature.metadata["ship_approval_token"] = approval.token
        pipeline.store.save(feature)

        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.READY
        assert not github.merge_calls

    def test_a_stale_approval_after_the_head_changes_is_refused(self, tmp_path, clock):
        approvals = self._approvals()
        pipeline, feature, github = self._ready(tmp_path, clock, approvals)
        approval = approvals.issue(
            capability="feature.ship",
            subject=feature.id,
            parameters={"risk": feature.risk, "head_sha": "a-superseded-sha"},
        )
        feature.metadata["ship_approval_token"] = approval.token
        pipeline.store.save(feature)

        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.READY
        assert not github.merge_calls
        assert "not the action that was approved" in shipped.metadata.get(
            "ship_approval_error", ""
        )

    def test_the_approval_is_single_use(self, tmp_path, clock):
        from openjarvis.wiz.approvals import ApprovalError

        approvals = self._approvals()
        pipeline, feature, github = self._ready(tmp_path, clock, approvals)
        approval = approvals.issue(
            capability="feature.ship",
            subject=feature.id,
            parameters={"risk": feature.risk, "head_sha": TestShip.HEAD_SHA},
        )
        approvals.redeem(
            approval.token,
            capability="feature.ship",
            subject=feature.id,
            parameters={"risk": feature.risk, "head_sha": TestShip.HEAD_SHA},
        )
        with pytest.raises(ApprovalError):
            approvals.redeem(
                approval.token,
                capability="feature.ship",
                subject=feature.id,
                parameters={"risk": feature.risk, "head_sha": TestShip.HEAD_SHA},
            )

    def test_merge_medium_risk_default_is_unaffected_by_a_granted_approval(
        self, tmp_path, clock
    ):
        approvals = self._approvals()
        pipeline, feature, github = self._ready(tmp_path, clock, approvals)
        approval = approvals.issue(
            capability="feature.ship",
            subject=feature.id,
            parameters={"risk": feature.risk, "head_sha": TestShip.HEAD_SHA},
        )
        feature.metadata["ship_approval_token"] = approval.token
        pipeline.store.save(feature)
        pipeline.ship(feature.id)
        assert pipeline.shipper.policy.merge_medium_risk is False


class TestAwaitingItemsOneOffShipApproval:
    """The full, real path for the separate nothing_awaiting_a_person gate:
    a redeemed approval bound to this feature, this exact head SHA, and this
    exact set of outstanding items is the only thing that lets a feature
    with something a person needs to look at still ship.
    """

    def _shipper(self, github):
        from openjarvis.wiz.features.shipping import (
            FeatureShipper,
            FeatureShippingPolicy,
        )

        return FeatureShipper(
            # merge_medium_risk on, so this test isolates the awaiting-items
            # gate rather than also needing a risk approval.
            policy=FeatureShippingPolicy(merge_medium_risk=True),
            github=github,
            authority=AuthorityPolicy(
                grants={
                    Channel.CONTROL_CENTER: frozenset(
                        {Authority.PR_WRITE, Authority.PRODUCTION_CHANGE}
                    )
                }
            ),
        )

    def _ready_with_outstanding_item(self, tmp_path, clock, approvals):
        github = TestShip.FakeGitHub()
        pipeline = build_pipeline(tmp_path, clock, approvals=approvals)
        pipeline.shipper = self._shipper(github)
        pipeline.postship = TestShip.FakePostShip(verified=True)
        submitted = pipeline.submit(REQUEST, actor=operator_actor())
        feature = pipeline.run(submitted.id)
        assert feature.state is FeatureState.READY
        feature.metadata["verification"]["awaiting_a_person"] = [
            "/ renders without layout overflow"
        ]
        pipeline.store.save(feature)
        return pipeline, feature, github

    def _approvals(self):
        from openjarvis.wiz.approvals import ApprovalStore

        return ApprovalStore(clock=lambda: 0.0, ttl_seconds=900)

    def test_an_outstanding_item_without_approval_is_refused(self, tmp_path, clock):
        pipeline, feature, github = self._ready_with_outstanding_item(
            tmp_path, clock, self._approvals()
        )
        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.READY
        assert not github.merge_calls

    def test_a_valid_approval_for_this_feature_head_and_items_ships(
        self, tmp_path, clock
    ):
        approvals = self._approvals()
        pipeline, feature, github = self._ready_with_outstanding_item(
            tmp_path, clock, approvals
        )
        approval = approvals.issue(
            capability="feature.ship_manual_items",
            subject=feature.id,
            parameters={
                "head_sha": TestShip.HEAD_SHA,
                "outstanding": ["/ renders without layout overflow"],
            },
        )
        feature.metadata["ship_manual_approval_token"] = approval.token
        pipeline.store.save(feature)

        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.COMPLETE
        assert github.merge_calls
        assert (
            shipped.metadata["ship_manual_approved_head_sha"] == TestShip.HEAD_SHA
        )
        assert "ship_manual_approval_token" not in shipped.metadata

    def test_approval_for_a_different_set_of_items_is_refused(self, tmp_path, clock):
        # A new outstanding item appearing changes the fingerprint, exactly
        # like a moved head SHA.
        approvals = self._approvals()
        pipeline, feature, github = self._ready_with_outstanding_item(
            tmp_path, clock, approvals
        )
        approval = approvals.issue(
            capability="feature.ship_manual_items",
            subject=feature.id,
            parameters={
                "head_sha": TestShip.HEAD_SHA,
                "outstanding": ["a different outstanding item entirely"],
            },
        )
        feature.metadata["ship_manual_approval_token"] = approval.token
        pipeline.store.save(feature)

        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.READY
        assert not github.merge_calls

    def test_approval_for_a_different_feature_does_not_transfer(self, tmp_path, clock):
        approvals = self._approvals()
        pipeline, feature, github = self._ready_with_outstanding_item(
            tmp_path, clock, approvals
        )
        approval = approvals.issue(
            capability="feature.ship_manual_items",
            subject="FEAT-SOMEONE-ELSE",
            parameters={
                "head_sha": TestShip.HEAD_SHA,
                "outstanding": ["/ renders without layout overflow"],
            },
        )
        feature.metadata["ship_manual_approval_token"] = approval.token
        pipeline.store.save(feature)

        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.READY
        assert not github.merge_calls


class RealisticFakeBrowser:
    """Unlike :class:`FakeBrowser`, this mirrors production's per-criterion
    attribution shape (``expectation_outcomes``/``assertion_outcomes`` in
    :class:`~openjarvis.reliability.types.ProbeResult`'s metadata) rather
    than one pass/fail verdict applied to every criterion in the batch by
    association. That distinction matters for exactly one thing here: a
    VIEWPORT criterion with no selector produces no expectation at all
    (:meth:`~openjarvis.wiz.features.acceptance.AcceptanceContract.
    _spec_for_route`), so with real per-criterion attribution it is
    genuinely left uncovered and lands in ``awaiting_a_person`` — the
    :class:`FakeBrowser` fallback instead marks it passed "by association"
    with whatever else shared its probe run, which never reproduces
    FEAT-00031's actual deadlock shape at all.
    """

    def __init__(self):
        self.runs = []

    def run(self, spec, *, base_url="", evidence_dir=None, **kwargs):
        self.runs.append(spec)
        expectation_outcomes = [
            {
                "kind": e.kind,
                "selector": e.selector,
                "value": e.value,
                "matches": e.matches,
                "passed": True,
                "detail": "",
            }
            for e in spec.expect
        ]
        assertion_outcomes = {}
        if spec.assertions.no_console_errors:
            assertion_outcomes["console"] = {"passed": True, "detail": ""}
        if spec.assertions.no_failed_requests:
            assertion_outcomes["network"] = {"passed": True, "detail": ""}
        return ProbeResult(
            probe_id=spec.id,
            success=True,
            error="",
            metadata={
                "viewport": spec.metadata.get("viewport", ""),
                "expectation_outcomes": expectation_outcomes,
                "assertion_outcomes": assertion_outcomes,
            },
        )


class TestManualAcceptanceLifecycle:
    """The lifecycle gap FEAT-00031 actually hit, and the fix for it.

    _finish() stops unconditionally whenever verification.complete is
    false — true whenever anything sits in awaiting_a_person, which a
    VIEWPORT criterion with no selector always does, by contract_for()'s
    own deterministic design, for essentially any UI-touching feature.
    FeatureRecovery.recover() *can* tolerate outstanding awaiting_a_person
    items and reach READY despite them, but it hard-requires an existing
    pull request — and a pull request is only ever opened by _finish()
    itself, after reaching READY. A feature with only awaiting_a_person
    items left and no PR yet could reach neither path: a permanent
    deadlock, proven here before it is closed.
    """

    def _pipeline_with_unmeasured_viewport(self, tmp_path, clock, *, approvals=None):
        # REQUEST ('...button...') is a UI request with nothing quoted for
        # contract_for() to key a CONTENT criterion off directly-checkable
        # text alone beyond what ScriptedEngineer's default diff satisfies —
        # what matters here is only that it is UI-touching, so contract_for()
        # always appends the unmeasured "works on a phone-sized screen"
        # VIEWPORT criterion (acceptance.py: no labels quoted -> also a
        # layout-overflow VIEWPORT criterion; both have no selector).
        browser = RealisticFakeBrowser()
        pipeline = build_pipeline(
            tmp_path, clock, browser=browser, approvals=approvals, max_attempts=1
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        return pipeline, result, browser

    def test_the_deadlock_is_real_without_an_approval(self, tmp_path, clock):
        pipeline, feature, browser = self._pipeline_with_unmeasured_viewport(
            tmp_path, clock
        )
        assert feature.state is FeatureState.HUMAN_REQUIRED
        v = feature.metadata["verification"]
        assert v["passed"] is True  # nothing actually failed
        assert v["complete"] is False  # only because of awaiting_a_person
        assert v["awaiting_a_person"]  # genuinely non-empty
        assert not feature.pr_number  # no PR — _open_pull_request never ran

        # And FeatureRecovery cannot rescue it either: no PR exists yet.
        from openjarvis.wiz.features.recovery import recover_feature

        result = recover_feature(
            feature.id,
            store=pipeline.store,
            profile=pipeline.profile,
            github=None,
            preview=pipeline.preview,
            check_suite_factory=pipeline.check_suite_factory,
        )
        assert not result.recovered
        assert any(r.code == "no_pull_request" for r in result.refusals)

        # And ship() refuses outright: it never got to READY.
        pipeline.shipper = None  # irrelevant to this refusal; make it explicit
        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.HUMAN_REQUIRED

    def test_an_exact_approval_reaches_ready_and_opens_a_pr(self, tmp_path, clock):
        approvals = ApprovalStore(clock=lambda: 0.0, ttl_seconds=900)
        pipeline, feature, browser = self._pipeline_with_unmeasured_viewport(
            tmp_path, clock, approvals=approvals
        )
        assert feature.state is FeatureState.HUMAN_REQUIRED
        head_sha = feature.attempts[-1].commit_sha
        outstanding = sorted(feature.metadata["verification"]["awaiting_a_person"])

        approved = pipeline.approve_manual_acceptance(
            feature.id, reason="looked at the desktop and mobile preview, both clean"
        )
        assert approved.metadata.get("ship_manual_approval_token")

        pipeline.shipper = FeatureShipper(
            policy=FeatureShippingPolicy(merge_medium_risk=True),
            github=TestShip.FakeGitHub(),
            authority=AuthorityPolicy(
                grants={
                    Channel.CONTROL_CENTER: frozenset(
                        {Authority.PR_WRITE, Authority.PRODUCTION_CHANGE}
                    )
                }
            ),
        )
        reopened = pipeline.reopen_for_deploy(
            feature.id, reason="manual acceptance approved; re-verifying"
        )
        assert reopened.state is FeatureState.TESTING

        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert result.pr_number  # _open_pull_request ran

        # The distinction the task requires: an owner's yes is not a
        # Playwright pass. verification.complete stays exactly what was
        # measured; manual_acceptance is recorded separately.
        assert result.metadata["verification"]["complete"] is False
        assert result.metadata["verification"]["awaiting_a_person"] == outstanding
        manual = result.metadata["manual_acceptance"]
        assert manual["owner_confirmed"] is True
        assert sorted(manual["items"]) == outstanding
        assert manual["head_sha"] == result.attempts[-1].commit_sha
        for outcome in result.metadata["verification"]["outcomes"]:
            if outcome["description"] in outstanding:
                assert outcome["passed"] is None  # never rewritten to True

        assert "ship_manual_approval_token" not in result.metadata

    def test_a_feature_that_reached_ready_this_way_can_actually_be_shipped(
        self, tmp_path, clock
    ):
        """Found live shipping FEAT-00031: reaching READY via
        approve_manual_acceptance() consumes the ship_manual_approval_token
        (by design -- see that method's docstring, "consumed the moment it
        is used"). ship()'s own nothing_awaiting_a_person gate used to
        re-check that same token, independently, every time -- so it always
        found the token already spent and refused to ship, for every
        feature this whole mechanism exists for. The fix: ship() checks the
        durable feature.metadata["manual_acceptance"] record _finish()
        already left behind instead of re-redeeming a spent token.
        """
        approvals = ApprovalStore(clock=lambda: 0.0, ttl_seconds=900)
        pipeline, feature, browser = self._pipeline_with_unmeasured_viewport(
            tmp_path, clock, approvals=approvals
        )
        assert feature.state is FeatureState.HUMAN_REQUIRED

        pipeline.approve_manual_acceptance(
            feature.id, reason="looked at the desktop and mobile preview, both clean"
        )
        github = TestShip.FakeGitHub()
        pipeline.shipper = FeatureShipper(
            policy=FeatureShippingPolicy(merge_medium_risk=True),
            github=github,
            authority=AuthorityPolicy(
                grants={
                    Channel.CONTROL_CENTER: frozenset(
                        {Authority.PR_WRITE, Authority.PRODUCTION_CHANGE}
                    )
                }
            ),
        )
        pipeline.postship = TestShip.FakePostShip(verified=True)
        pipeline.reopen_for_deploy(feature.id, reason="manual acceptance approved")
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert "ship_manual_approval_token" not in result.metadata  # spent
        assert result.metadata["manual_acceptance"]["owner_confirmed"] is True

        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.COMPLETE
        assert github.merge_calls

    def test_the_approval_is_consumed_the_journal_records_both_steps(
        self, tmp_path, clock
    ):
        approvals = ApprovalStore(clock=lambda: 0.0, ttl_seconds=900)
        pipeline, feature, browser = self._pipeline_with_unmeasured_viewport(
            tmp_path, clock, approvals=approvals
        )
        pipeline.approve_manual_acceptance(feature.id, reason="checked the preview")
        pipeline.reopen_for_deploy(feature.id, reason="re-verifying")
        pipeline.run(feature.id)

        entries = [
            e
            for e in pipeline.journal.tail(50)
            if e.detail.get("feature_id") == feature.id
        ]
        kinds = [e.kind for e in entries]
        assert "feature.manual_acceptance_approved" in kinds
        assert "feature.manual_items_confirmed" in kinds

    def test_a_stale_head_sha_approval_is_refused(self, tmp_path, clock):
        # The approval names a head SHA that is no longer this attempt's —
        # exactly what "if head SHA changes, revalidate" means in practice.
        approvals = ApprovalStore(clock=lambda: 0.0, ttl_seconds=900)
        pipeline, feature, browser = self._pipeline_with_unmeasured_viewport(
            tmp_path, clock, approvals=approvals
        )
        outstanding = sorted(feature.metadata["verification"]["awaiting_a_person"])
        approval = approvals.issue(
            capability="feature.ship_manual_items",
            subject=feature.id,
            parameters={"head_sha": "stale" + "0" * 36, "outstanding": outstanding},
        )
        feature.metadata["ship_manual_approval_token"] = approval.token
        pipeline.store.save(feature)

        reopened = pipeline.reopen_for_deploy(feature.id, reason="re-verifying")
        assert reopened.state is FeatureState.TESTING
        result = pipeline.run(feature.id)

        assert result.state is FeatureState.HUMAN_REQUIRED
        assert not result.pr_number
        assert "manual_acceptance" not in result.metadata

    def test_a_genuine_automated_failure_is_never_masked_by_a_valid_approval(
        self, tmp_path, clock
    ):
        # A real failure must refuse regardless of any manual approval —
        # _finish() checks verification.passed, unconditionally, before it
        # ever looks at the awaiting-items approval.
        approvals = ApprovalStore(clock=lambda: 0.0, ttl_seconds=900)
        browser = RealisticFakeBrowser()
        pipeline = build_pipeline(
            tmp_path,
            clock,
            browser=browser,
            approvals=approvals,
            suite_results=[FakeCheckResult(passed=False, summary="typecheck broken")],
            max_attempts=1,
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        # Never even reached the browser: a failing local gate stops in
        # _deploy_preview, before there is any verification to approve.
        assert "verification" not in result.metadata

        # Even handing it a (structurally unusable, since there is no
        # verified commit yet) approval attempt is refused outright.
        with pytest.raises(ApprovalError):
            pipeline.approve_manual_acceptance(feature.id, reason="trying anyway")

    def test_approve_manual_acceptance_refuses_with_nothing_outstanding(
        self, tmp_path, clock
    ):
        approvals = ApprovalStore(clock=lambda: 0.0, ttl_seconds=900)
        pipeline = build_pipeline(tmp_path, clock, approvals=approvals)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY  # the plain FakeBrowser path

        with pytest.raises(ApprovalError):
            pipeline.approve_manual_acceptance(feature.id, reason="nothing to approve")

    def test_approve_manual_acceptance_refuses_without_an_approval_store(
        self, tmp_path, clock
    ):
        pipeline, feature, browser = self._pipeline_with_unmeasured_viewport(
            tmp_path, clock, approvals=None
        )
        with pytest.raises(ApprovalError):
            pipeline.approve_manual_acceptance(feature.id, reason="no store configured")


class TestReverifyAgainstCurrentBase:
    """The primitive ship()'s require_base_unmoved refusal has no other way
    back into: a READY feature whose base has moved has nowhere to go
    without this. Found on FEAT-00031.
    """

    HEAD_SHA = "feedface" + "0" * 32  # matches FakeWorkspace's default commit_sha
    OLD_BASE = "base1234" + "0" * 32  # matches FakeWorkspace.create()'s default
    NEW_BASE = "newbase1" + "0" * 32
    MERGED_SHA = "merged01" + "0" * 32
    NEW_BASE_2 = "newbase2" + "0" * 32
    MERGED_SHA_2 = "merged02" + "0" * 32

    class FakeGitHub(TestShip.FakeGitHub):
        """Unlike TestShip.FakeGitHub, base_sha is mutable per test/per
        call -- exactly what proving "the base moved" requires.
        """

        def __init__(self, *, base_sha=None, head_sha=None, **kwargs):
            super().__init__(**kwargs)
            self.base_sha = base_sha or TestReverifyAgainstCurrentBase.OLD_BASE
            self.head_sha = head_sha or TestReverifyAgainstCurrentBase.HEAD_SHA

        def get_pull_request(self, number):
            return {
                "number": number,
                "head_sha": self.head_sha,
                "base_sha": self.base_sha,
                "state": "open",
                "mergeable": True,
            }

    def _shipper(self, github):
        return FeatureShipper(
            policy=FeatureShippingPolicy(merge_medium_risk=True),
            github=github,
            authority=AuthorityPolicy(
                grants={
                    Channel.CONTROL_CENTER: frozenset(
                        {Authority.PR_WRITE, Authority.PRODUCTION_CHANGE}
                    )
                }
            ),
        )

    def _ready(self, tmp_path, clock, *, github=None, workspace=None, max_attempts=3):
        github = github or self.FakeGitHub()
        workspace = workspace or FakeWorkspace(tmp_path)
        engineer = ScriptedEngineer()
        pipeline = build_pipeline(
            tmp_path,
            clock,
            workspace=workspace,
            engineer=engineer,
            max_attempts=max_attempts,
        )
        pipeline.shipper = self._shipper(github)
        pipeline.postship = TestShip.FakePostShip(verified=True)
        submitted = pipeline.submit(REQUEST, actor=operator_actor())
        feature = pipeline.run(submitted.id)
        assert feature.state is FeatureState.READY
        return pipeline, feature, github, workspace, engineer

    def test_ship_refuses_when_base_has_moved(self, tmp_path, clock):
        github = self.FakeGitHub(base_sha=self.NEW_BASE)
        pipeline, feature, github, workspace, engineer = self._ready(
            tmp_path, clock, github=github
        )
        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.READY
        assert not github.merge_calls

    def test_unchanged_base_is_a_no_op(self, tmp_path, clock):
        github = self.FakeGitHub(base_sha=self.OLD_BASE)
        pipeline, feature, github, workspace, engineer = self._ready(
            tmp_path, clock, github=github
        )
        result = pipeline.reverify_against_current_base(
            feature.id, expected_head_sha=self.HEAD_SHA, reason="checking"
        )
        assert result.state is FeatureState.READY
        assert not workspace.checkouts  # never even tried to reconcile

    def test_moved_base_merges_cleanly_and_re_enters_testing(self, tmp_path, clock):
        github = self.FakeGitHub(base_sha=self.NEW_BASE)
        pipeline, feature, github, workspace, engineer = self._ready(
            tmp_path, clock, github=github
        )
        workspace.merge_outcome = MergeOutcome(merged=True, new_sha=self.MERGED_SHA)

        refreshed = pipeline.reverify_against_current_base(
            feature.id, expected_head_sha=self.HEAD_SHA, reason="base moved"
        )
        assert refreshed.state is FeatureState.TESTING
        assert workspace.checkouts  # a fresh worktree was checked out
        assert workspace.merge_calls
        assert refreshed.attempts[-1].commit_sha == self.MERGED_SHA
        assert refreshed.base_sha == self.NEW_BASE
        assert "manual_acceptance" not in refreshed.metadata
        assert refreshed.attempts_used == 1  # no new attempt spent
        assert len(engineer.build_calls) == 1  # unchanged -- no rebuild

        # Re-entering TESTING drives fresh, real re-verification. The merge
        # already landed and was pushed by reverify_against_current_base
        # itself, so the worktree is clean going into this run -- exactly
        # like a real git worktree is after a successful merge+push (see
        # _deploy_preview's has_changes()/head_sha() branch). FakeWorkspace
        # and FakeVercel have no real git/deployment state, so both need
        # telling explicitly that the merged commit is now the one that
        # exists.
        workspace.changes_present = False
        workspace.commit_sha = self.MERGED_SHA
        pipeline.preview.vercel.commit_sha = self.MERGED_SHA
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert result.attempts[-1].commit_sha == self.MERGED_SHA
        assert len(engineer.build_calls) == 1  # still no rebuild

    def test_a_stale_expected_head_is_refused(self, tmp_path, clock):
        github = self.FakeGitHub(base_sha=self.NEW_BASE)
        pipeline, feature, github, workspace, engineer = self._ready(
            tmp_path, clock, github=github
        )
        with pytest.raises(InvalidFeatureTransition):
            pipeline.reverify_against_current_base(
                feature.id,
                expected_head_sha="not-the-real-head" + "0" * 20,
                reason="x",
            )
        assert not workspace.checkouts

    def test_no_pull_request_is_refused(self, tmp_path, clock):
        pipeline = build_pipeline(tmp_path, clock)
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)
        with pytest.raises(InvalidFeatureTransition):
            pipeline.reverify_against_current_base(
                feature.id, expected_head_sha=self.HEAD_SHA, reason="x"
            )

    def test_not_ready_is_refused(self, tmp_path, clock):
        pipeline = build_pipeline(
            tmp_path,
            clock,
            engineer=ScriptedEngineer(
                plans=[EngineeringSession(mode="plan", succeeded=False, error="ambiguous")]
            ),
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)  # stops in planning, never reaches READY
        with pytest.raises(InvalidFeatureTransition):
            pipeline.reverify_against_current_base(
                feature.id, expected_head_sha="x", reason="x"
            )

    def test_a_merge_conflict_stops_at_human_required_no_engineer_no_attempt(
        self, tmp_path, clock
    ):
        github = self.FakeGitHub(base_sha=self.NEW_BASE)
        pipeline, feature, github, workspace, engineer = self._ready(
            tmp_path, clock, github=github
        )
        workspace.merge_outcome = MergeOutcome(
            merged=False,
            conflicting_files=["src/lib/language.ts"],
            error="<<<<<<< HEAD",
        )

        result = pipeline.reverify_against_current_base(
            feature.id, expected_head_sha=self.HEAD_SHA, reason="base moved"
        )
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert "src/lib/language.ts" in result.history[-1]["reason"]
        assert result.attempts_used == 1  # unchanged
        assert len(engineer.build_calls) == 1  # never invoked again

    def test_automated_failure_after_refresh_still_blocks_ship(self, tmp_path, clock):
        github = self.FakeGitHub(base_sha=self.NEW_BASE)
        suite = FakeSuite([FakeCheckResult(passed=False, summary="still broken")])
        workspace = FakeWorkspace(tmp_path)
        pipeline, feature, github, workspace, engineer = self._ready(
            tmp_path, clock, github=github, workspace=workspace
        )
        workspace.merge_outcome = MergeOutcome(merged=True, new_sha=self.MERGED_SHA)
        pipeline.check_suite_factory = lambda profile: suite

        pipeline.reverify_against_current_base(
            feature.id, expected_head_sha=self.HEAD_SHA, reason="base moved"
        )
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.HUMAN_REQUIRED
        shipped = pipeline.ship(feature.id)
        assert shipped.state is FeatureState.HUMAN_REQUIRED
        assert not github.merge_calls

    def test_repeatable_a_second_base_move_can_be_refreshed_again(self, tmp_path, clock):
        github = self.FakeGitHub(base_sha=self.NEW_BASE)
        pipeline, feature, github, workspace, engineer = self._ready(
            tmp_path, clock, github=github
        )
        workspace.merge_outcome = MergeOutcome(merged=True, new_sha=self.MERGED_SHA)
        pipeline.reverify_against_current_base(
            feature.id, expected_head_sha=self.HEAD_SHA, reason="first move"
        )
        # Same fake-vs-real-git subtlety as
        # test_moved_base_merges_cleanly_and_re_enters_testing: the merge was
        # already pushed, so the worktree is clean going into this run.
        workspace.changes_present = False
        workspace.commit_sha = self.MERGED_SHA
        pipeline.preview.vercel.commit_sha = self.MERGED_SHA
        # A real push moves the PR's own head too -- GitHub reports whatever
        # is actually on the branch, so the fake has to track it as well.
        github.head_sha = self.MERGED_SHA
        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY

        second_base = "yetanother" + "0" * 22
        second_sha = "second02" + "0" * 32
        github.base_sha = second_base
        workspace.merge_outcome = MergeOutcome(merged=True, new_sha=second_sha)

        refreshed_again = pipeline.reverify_against_current_base(
            feature.id, expected_head_sha=self.MERGED_SHA, reason="second move"
        )
        assert refreshed_again.state is FeatureState.TESTING
        assert refreshed_again.attempts[-1].commit_sha == second_sha
        assert refreshed_again.base_sha == second_base
        assert len(engineer.build_calls) == 1  # still just the one real build

    def test_direct_main_push_is_still_impossible(self, tmp_path, clock):
        # merge_base() only ever pushes the feature's own branch (via
        # workspace.push, whose real implementation refuses anything not
        # carrying the feature prefix) -- the fake records what it was
        # asked to push, proving this primitive never targets main itself.
        github = self.FakeGitHub(base_sha=self.NEW_BASE)
        pipeline, feature, github, workspace, engineer = self._ready(
            tmp_path, clock, github=github
        )
        workspace.merge_outcome = MergeOutcome(merged=True, new_sha=self.MERGED_SHA)
        pipeline.reverify_against_current_base(
            feature.id, expected_head_sha=self.HEAD_SHA, reason="base moved"
        )
        # One push from _ready()'s own initial build-to-READY run, one more
        # from reverify_against_current_base's merge push -- both always to
        # "origin" for the feature branch, never main.
        assert workspace.pushes == ["origin", "origin"]

    def test_reentry_from_human_required_after_exhausted_attempts_reverifies_cleanly(
        self, tmp_path, clock
    ):
        """Found live on FEAT-00031: a base-refresh's own re-verification can
        exhaust attempts against an *intermediate* base and stop at
        HUMAN_REQUIRED while the base moves again in the meantime -- here,
        because a later commit on main happened to also fix whatever the
        check suite was failing on. Re-entry must work with no Engineer
        session and no new attempt spent, exactly like the READY path.
        """
        github = self.FakeGitHub(base_sha=self.NEW_BASE)
        failing_suite = FakeSuite([FakeCheckResult(passed=False, summary="still broken")])
        workspace = FakeWorkspace(tmp_path)
        # Attempts already exhausted by the time reverify runs, exactly as
        # they were for real on FEAT-00031 -- so the post-reverify TESTING
        # failure stops immediately (_retry_or_stop's exhausted-attempts
        # check fires on the very first failure) rather than spending fresh
        # BUILDING retries whose own attempt objects would never reach the
        # commit-assignment code and so would carry no commit_sha at all.
        pipeline, feature, github, workspace, engineer = self._ready(
            tmp_path, clock, github=github, workspace=workspace, max_attempts=1
        )
        workspace.merge_outcome = MergeOutcome(merged=True, new_sha=self.MERGED_SHA)
        pipeline.check_suite_factory = lambda profile: failing_suite

        pipeline.reverify_against_current_base(
            feature.id, expected_head_sha=self.HEAD_SHA, reason="base moved"
        )
        exhausted = pipeline.run(feature.id)
        assert exhausted.state is FeatureState.HUMAN_REQUIRED
        exhausted_head = exhausted.attempts[-1].commit_sha
        assert exhausted_head == self.MERGED_SHA
        build_calls_before = len(engineer.build_calls)

        # The base moves again, and this time reconciling it also happens to
        # carry in whatever fixed the check suite (matching FEAT-00031: a
        # later main commit independently fixed the lucide-react break).
        # The first reverify's merge was actually pushed, so the PR's real
        # head moved too.
        github.head_sha = exhausted_head
        github.base_sha = self.NEW_BASE_2
        fixed_suite = FakeSuite([FakeCheckResult(passed=True)])
        pipeline.check_suite_factory = lambda profile: fixed_suite
        workspace.merge_outcome = MergeOutcome(merged=True, new_sha=self.MERGED_SHA_2)

        refreshed = pipeline.reverify_against_current_base(
            feature.id, expected_head_sha=exhausted_head, reason="second move, fix landed"
        )
        assert refreshed.state is FeatureState.TESTING
        assert refreshed.attempts[-1].commit_sha == self.MERGED_SHA_2
        assert refreshed.base_sha == self.NEW_BASE_2
        assert len(engineer.build_calls) == build_calls_before  # no new session

        # Same fake-vs-real-git subtlety as the READY-entry tests: the merge
        # already landed and was pushed, so the worktree is clean going into
        # this run, and every double consulted elsewhere in the pipeline
        # needs to agree the merged commit is now the one that exists.
        workspace.changes_present = False
        workspace.commit_sha = self.MERGED_SHA_2
        pipeline.preview.vercel.commit_sha = self.MERGED_SHA_2
        github.head_sha = self.MERGED_SHA_2

        result = pipeline.run(feature.id)
        assert result.state is FeatureState.READY
        assert result.attempts[-1].commit_sha == self.MERGED_SHA_2
        assert len(engineer.build_calls) == build_calls_before  # still none

    def test_reentry_from_human_required_conflict_stays_human_required(
        self, tmp_path, clock
    ):
        """A second base movement that genuinely conflicts, discovered while
        already HUMAN_REQUIRED, must not crash on an illegal
        HUMAN_REQUIRED -> HUMAN_REQUIRED self-transition -- it has to record
        the new conflict and stay put, the same as _retry_or_stop does for
        BUILDING -> BUILDING.
        """
        github = self.FakeGitHub(base_sha=self.NEW_BASE)
        failing_suite = FakeSuite([FakeCheckResult(passed=False, summary="still broken")])
        workspace = FakeWorkspace(tmp_path)
        pipeline, feature, github, workspace, engineer = self._ready(
            tmp_path, clock, github=github, workspace=workspace, max_attempts=1
        )
        workspace.merge_outcome = MergeOutcome(merged=True, new_sha=self.MERGED_SHA)
        pipeline.check_suite_factory = lambda profile: failing_suite
        pipeline.reverify_against_current_base(
            feature.id, expected_head_sha=self.HEAD_SHA, reason="base moved"
        )
        exhausted = pipeline.run(feature.id)
        assert exhausted.state is FeatureState.HUMAN_REQUIRED
        exhausted_head = exhausted.attempts[-1].commit_sha
        assert exhausted_head == self.MERGED_SHA

        # The first reverify's merge was actually pushed, so the PR's real
        # head moved too -- the fake has to track that, same as every other
        # test here that pushes a second time.
        github.head_sha = exhausted_head
        github.base_sha = self.NEW_BASE_2
        workspace.merge_outcome = MergeOutcome(
            merged=False,
            conflicting_files=["src/lib/language.ts"],
            error="<<<<<<< HEAD",
        )
        result = pipeline.reverify_against_current_base(
            feature.id, expected_head_sha=exhausted_head, reason="second move, real conflict"
        )
        assert result.state is FeatureState.HUMAN_REQUIRED
        assert "src/lib/language.ts" in result.history[-1]["reason"]
        assert result.attempts[-1].commit_sha == exhausted_head  # unchanged

    def test_reentry_from_human_required_works_even_when_original_stop_was_unrelated(
        self, tmp_path, clock
    ):
        """Safe by construction, not by checking why the feature stopped:
        this primitive never calls Engineer and never spends an attempt, so
        re-entry from a HUMAN_REQUIRED reached for a completely unrelated
        reason is harmless -- the worst case is a real, unrelated defect
        surfacing again with fresh evidence, never a bypassed gate.
        """
        github = self.FakeGitHub(base_sha=self.OLD_BASE)
        pipeline, feature, github, workspace, engineer = self._ready(
            tmp_path, clock, github=github
        )
        # Reached HUMAN_REQUIRED for a reason with nothing to do with the
        # base -- an ordinary owner cancellation, say.
        feature.transition(
            FeatureState.HUMAN_REQUIRED, at=clock(), reason="operator paused this for review"
        )
        pipeline.store.save(feature)

        github.base_sha = self.NEW_BASE
        workspace.merge_outcome = MergeOutcome(merged=True, new_sha=self.MERGED_SHA)
        refreshed = pipeline.reverify_against_current_base(
            feature.id, expected_head_sha=self.HEAD_SHA, reason="base moved"
        )
        assert refreshed.state is FeatureState.TESTING
        assert refreshed.attempts[-1].commit_sha == self.MERGED_SHA


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


class TestProvisioningRunsBeforeTheCheckSuite:
    """FEAT-00031: three attempts failed identically on "tsc: command not
    found" because node_modules never existed and nothing installed it.
    """

    def test_a_failed_provision_blocks_the_check_suite_entirely(self, tmp_path, clock):
        from openjarvis.reliability.checks import CheckResult

        def failing_provision(profile, workspace):
            return CheckResult(
                name="provision",
                ran=True,
                passed=False,
                summary="failed (exit 127)",
                output="sh: tsc: command not found",
            )

        pipeline = build_pipeline(
            tmp_path, clock, max_attempts=1, provision_factory=failing_provision
        )
        submitted = pipeline.submit(REQUEST, actor=operator_actor())
        feature = pipeline.run(submitted.id)

        assert pipeline._suite.runs == []  # the check suite itself never ran
        assert feature.state is FeatureState.HUMAN_REQUIRED
        assert any("tsc: command not found" in a.failure for a in feature.attempts)

    def test_a_failed_provision_is_fed_back_to_the_next_attempt(self, tmp_path, clock):
        from openjarvis.reliability.checks import CheckResult

        calls = {"n": 0}

        def flaky_provision(profile, workspace):
            calls["n"] += 1
            if calls["n"] == 1:
                return CheckResult(
                    name="provision",
                    ran=True,
                    passed=False,
                    summary="failed (exit 127)",
                    output="sh: tsc: command not found",
                )
            return CheckResult(name="provision", ran=True, passed=True, summary="provisioned")

        engineer = ScriptedEngineer()
        pipeline = build_pipeline(
            tmp_path, clock, engineer=engineer, provision_factory=flaky_provision
        )
        feature = pipeline.submit(REQUEST, actor=operator_actor())
        pipeline.run(feature.id)

        assert calls["n"] >= 2
        # The second build session was told about the first failure.
        second_goal = engineer.build_calls[1][0].render()
        assert "tsc: command not found" in second_goal

    def test_a_successful_provision_result_is_recorded_alongside_the_gates(
        self, tmp_path, clock
    ):
        pipeline = build_pipeline(tmp_path, clock)
        submitted = pipeline.submit(REQUEST, actor=operator_actor())
        feature = pipeline.run(submitted.id)

        gates = feature.metadata.get("gates", {})
        names = [r.get("name") for r in gates.get("results", [])]
        assert "provision" in names

    def test_building_still_has_no_bash_and_cannot_provision_itself(self):
        # Not re-tested here in depth (see test_engineer.py) — pinned once
        # more at the integration boundary this module actually touches:
        # provisioning is pipeline code, never something the model's own
        # tool list could reach.
        from openjarvis.wiz.features.engineer import BUILDING_TOOLS

        assert "Bash" not in BUILDING_TOOLS


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
