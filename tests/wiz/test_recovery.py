"""Recovering a feature from real evidence, without editing the store by hand."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from openjarvis.wiz.features.acceptance import CONTENT, AcceptanceContract, Criterion
from openjarvis.wiz.features.model import FeatureAttempt, FeatureRequest, FeatureState
from openjarvis.wiz.features.profile import EngineeringProfile
from openjarvis.wiz.features.recovery import FeatureRecovery
from openjarvis.wiz.features.store import FeatureStore
from openjarvis.wiz.features.verification import CriterionOutcome, FeatureVerification
from openjarvis.wiz.journal import WizJournal

PROFILE = EngineeringProfile(
    name="wize",
    repository="axe11112/Wize-Performance",
    checkout="/tmp/wize",
    base_branch="main",
    lint_command="npm run lint",
    typecheck_command="npm run typecheck",
    test_command="npm test",
    build_command="npm run build",
)

HEAD_SHA = "96ad25f074b4c44a4f72bdfcbb10839d58f7330f"
BASE_SHA = "e7640b02b25e061cd8e29ad0bcf622f559ff5991"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeCheckResult:
    passed: bool = True
    summary: str = "all checks passed"

    def feedback(self, *, max_chars: int = 6000) -> str:
        return self.summary

    def to_dict(self) -> Dict[str, Any]:
        return {"passed": self.passed, "ran_any": True, "results": []}


class FakeSuite:
    def __init__(self, result: FakeCheckResult):
        self.result = result
        self.runs: List[str] = []

    def run(self, *, workspace: str):
        self.runs.append(workspace)
        return self.result


@dataclass
class FakeObservation:
    usable: bool = True
    url: str = "https://feature-preview.example.vercel.app/"
    reason: str = "the preview for this commit is ready"
    deployment_id: str = "dpl_fake123"


class FakePreview:
    def __init__(self, observation: Optional[FakeObservation] = None):
        self.observation = observation or FakeObservation()
        self.calls: List[Dict[str, Any]] = []

    def observe(self, *, commit_sha: str, branch: str = ""):
        self.calls.append({"commit_sha": commit_sha, "branch": branch})
        return self.observation


class FakeGitHub:
    def __init__(
        self,
        pr: Optional[Dict[str, Any]] = None,
        status: Optional[Dict[str, Any]] = None,
    ):
        self.pr = pr or self._open_pr()
        self.status = status or {
            "state": "success",
            "missing_required": [],
            "required": {"Vercel": "success"},
        }
        self.calls: List[int] = []

    @staticmethod
    def _open_pr(**overrides) -> Dict[str, Any]:
        pr = {
            "number": 229,
            "state": "open",
            "merged": False,
            "head_ref": "wiz/feature/FEAT-00001-landing-page-footer-copyright-notice",
            "head_sha": HEAD_SHA,
            "base_ref": "main",
            "mergeable": True,
            "url": "https://github.com/axe11112/Wize-Performance/pull/229",
        }
        pr.update(overrides)
        return pr

    def get_pull_request(self, number: int) -> Dict[str, Any]:
        self.calls.append(number)
        return self.pr

    def combined_status(self, sha: str, *, required_contexts=None) -> Dict[str, Any]:
        return self.status


class FakeVerifier:
    """A browser verifier stand-in that hands back a scripted verdict."""

    def __init__(self, *, passed: bool = True):
        self._passed = passed
        self.calls: List[Dict[str, Any]] = []

    def verify(
        self,
        contract,
        *,
        preview_url: str,
        commit_sha: str = "",
        deployment_id: str = "",
        attempt: int = 1,
        gate_outcomes=(),
    ) -> FeatureVerification:
        self.calls.append(
            {
                "preview_url": preview_url,
                "commit_sha": commit_sha,
                "deployment_id": deployment_id,
                "attempt": attempt,
            }
        )
        criterion = contract.browser_criteria[0]
        outcome = CriterionOutcome(
            criterion=criterion,
            passed=self._passed,
            viewport="desktop",
            detail="" if self._passed else "the expected text is missing",
        )
        return FeatureVerification(
            feature_id=contract.feature_id,
            preview_url=preview_url,
            commit_sha=commit_sha,
            deployment_id=deployment_id,
            outcomes=[outcome],
        )


def _contract_with_content_check(feature_id: str = "FEAT-00001") -> Dict[str, Any]:
    contract = AcceptanceContract(
        feature_id=feature_id,
        criteria=(
            Criterion(kind=CONTENT, route="/", text="hello", description="says hello"),
        ),
    )
    return contract.to_dict()


def make_feature(
    *,
    state: FeatureState = FeatureState.BUILDING,
    pr_number: int = 229,
    branch: str = "wiz/feature/FEAT-00001-landing-page-footer-copyright-notice",
    repository: str = "axe11112/Wize-Performance",
    risk: str = "MEDIUM",
    worktree: str = "/tmp/does-not-matter",
    attempt_commit_sha: str = "",
    contract: Optional[Dict[str, Any]] = None,
    stale_verification: Optional[Dict[str, Any]] = None,
) -> FeatureRequest:
    feature = FeatureRequest(
        id="FEAT-00001",
        title="Landing page footer copyright notice",
        operator_request="Add a copyright line to the landing page footer",
        source="cli",
        actor_id="operator",
        target="wize",
        repository=repository,
        state=state,
        risk=risk,
        branch=branch,
        worktree=worktree,
        base_sha=BASE_SHA,
        pr_number=pr_number,
    )
    feature.attempts.append(
        FeatureAttempt(number=1, started_at="t0", commit_sha=attempt_commit_sha)
    )
    if contract is not None:
        feature.metadata["contract"] = contract
    if stale_verification is not None:
        feature.metadata["verification"] = stale_verification
    return feature


def build_recovery(
    tmp_path,
    *,
    github=None,
    preview=None,
    suite_result=None,
    required_status_contexts=(),
    verifier=None,
    git_rev_parse_head=None,
    git_diff=None,
):
    store = FeatureStore(tmp_path / "features.db")
    journal = WizJournal(tmp_path / "journal.jsonl")
    suite = FakeSuite(suite_result or FakeCheckResult())
    recovery = FeatureRecovery(
        store=store,
        profile=PROFILE,
        github=github or FakeGitHub(),
        preview=preview or FakePreview(),
        check_suite_factory=lambda profile: suite,
        journal=journal,
        required_status_contexts=required_status_contexts,
        verifier=verifier,
        clock=lambda: "2026-08-24T09:00:00+00:00",
        git_rev_parse_head=git_rev_parse_head or (lambda worktree: HEAD_SHA),
        git_diff=git_diff or (lambda worktree, base, head: "+ nothing sensitive"),
    )
    return recovery, store, journal, suite


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_a_genuinely_finished_build_reaches_ready(self, tmp_path):
        recovery, store, journal, suite = build_recovery(tmp_path)
        store.create(make_feature())

        result = recovery.recover(
            "FEAT-00001", reason="the coding session was interrupted"
        )

        assert result.recovered
        assert result.state == "READY"
        saved = store.get("FEAT-00001")
        assert saved.state is FeatureState.READY
        assert saved.attempts[-1].commit_sha == HEAD_SHA
        assert saved.attempts[-1].succeeded
        assert saved.pr_url == "https://github.com/axe11112/Wize-Performance/pull/229"
        assert suite.runs == ["/tmp/does-not-matter"]

    def test_every_legal_hop_is_visited_and_none_is_skipped(self, tmp_path):
        recovery, store, journal, _ = build_recovery(tmp_path)
        store.create(make_feature())
        recovery.recover("FEAT-00001", reason="recovering")
        saved = store.get("FEAT-00001")
        visited = [entry["to"] for entry in saved.history]
        assert visited == ["TESTING", "PREVIEWING", "VERIFYING", "READY"]

    def test_recovery_from_a_later_state_only_takes_the_remaining_hops(self, tmp_path):
        recovery, store, _, _ = build_recovery(tmp_path)
        store.create(make_feature(state=FeatureState.PREVIEWING))
        recovery.recover("FEAT-00001", reason="recovering")
        saved = store.get("FEAT-00001")
        assert [e["to"] for e in saved.history] == ["VERIFYING", "READY"]

    def test_a_feature_the_attempt_loop_gave_up_on_can_still_be_recovered(
        self, tmp_path
    ):
        """HUMAN_REQUIRED is where attempts_exhausted lands a feature — the
        exact real-world case this exists for."""
        recovery, store, _, _ = build_recovery(tmp_path)
        store.create(make_feature(state=FeatureState.HUMAN_REQUIRED))
        result = recovery.recover("FEAT-00001", reason="recovering")
        assert result.recovered
        saved = store.get("FEAT-00001")
        assert saved.state is FeatureState.READY
        visited = [entry["to"] for entry in saved.history]
        assert visited == ["BUILDING", "TESTING", "PREVIEWING", "VERIFYING", "READY"]
        # the resume hop is marked, so the audit trail can tell it apart from
        # an ordinary attempt-loop transition
        assert saved.history[0]["resumed"] is True
        assert saved.history[0]["from"] == "HUMAN_REQUIRED"

    def test_resuming_from_human_required_does_not_touch_legal_transitions(
        self, tmp_path
    ):
        """The general FSM guarantee — a terminal state progresses nowhere on
        its own via transition() — must still hold for every other caller."""
        from openjarvis.wiz.features.model import LEGAL_TRANSITIONS

        assert LEGAL_TRANSITIONS[FeatureState.HUMAN_REQUIRED] == frozenset()

    def test_an_unrecorded_pull_request_is_adopted_when_named_and_it_matches(
        self, tmp_path
    ):
        """The operator opened PR #229 by hand; the feature never heard about
        it. Recovery may adopt it, but only because it actually belongs to
        this feature's branch — not on the operator's word alone."""
        recovery, store, _, _ = build_recovery(tmp_path)
        store.create(make_feature(pr_number=0))
        result = recovery.recover("FEAT-00001", reason="recovering", pr_number=229)
        assert result.recovered
        saved = store.get("FEAT-00001")
        assert saved.pr_number == 229
        assert saved.pr_url == "https://github.com/axe11112/Wize-Performance/pull/229"

    def test_an_adopted_pull_request_from_the_wrong_branch_still_refuses(
        self, tmp_path
    ):
        github = FakeGitHub(
            pr=FakeGitHub._open_pr(head_ref="wiz/feature/some-other-feature")
        )
        recovery, store, _, _ = build_recovery(tmp_path, github=github)
        store.create(make_feature(pr_number=0))
        result = recovery.recover("FEAT-00001", reason="recovering", pr_number=229)
        assert not result.recovered
        assert result.refusals[0].code == "wrong_branch"
        assert store.get("FEAT-00001").pr_number == 0

    def test_a_feature_that_already_has_a_pull_request_ignores_a_different_adopt_number(
        self, tmp_path
    ):
        """Adoption only fills an empty slot — it can never override a
        recorded pull request, named or not."""
        github = FakeGitHub()
        recovery, store, _, _ = build_recovery(tmp_path, github=github)
        store.create(make_feature(pr_number=229))
        recovery.recover("FEAT-00001", reason="recovering", pr_number=999)
        # the github fake always answers with PR #229 regardless of the
        # number asked for; if adoption had overridden the recorded number
        # with 999 this call would fail with pr_number_mismatch instead
        assert github.calls == [229]

    def test_recovery_is_journalled_with_the_operators_reason(self, tmp_path):
        recovery, store, journal, _ = build_recovery(tmp_path)
        store.create(make_feature())
        recovery.recover(
            "FEAT-00001", reason="attempt 2's WIP diff was recovered from reflog"
        )
        entries = journal.entries()
        reasons = [e.reason for e in entries]
        assert any("recovered from reflog" in r for r in reasons)
        # feature.ready is the last state transition recorded
        assert entries[-1].kind == "feature.ready"

    def test_recovering_an_already_ready_feature_with_the_same_commit_is_a_noop(
        self, tmp_path
    ):
        recovery, store, journal, _ = build_recovery(tmp_path)
        store.create(make_feature())
        recovery.recover("FEAT-00001", reason="first pass")
        entries_after_first = len(journal.entries())

        result = recovery.recover("FEAT-00001", reason="second pass")

        assert result.recovered
        assert result.state == "READY"
        # idempotent: a pure recheck of already-recovered state writes nothing new
        assert len(journal.entries()) == entries_after_first
        saved = store.get("FEAT-00001")
        assert [e["to"] for e in saved.history] == [
            "TESTING",
            "PREVIEWING",
            "VERIFYING",
            "READY",
        ]


class TestFreshBrowserVerification:
    """Recovery must refresh feature.metadata["verification"] with a real,
    fresh browser check bound to the exact commit/deployment being
    recovered — not leave whatever an earlier, possibly-failed attempt last
    stored there. Found on FEAT-00017: recovery reached READY off local
    gates and preview reachability alone, while the stored acceptance
    result was still the stale, contaminated one from before the verifier
    bugs were fixed.
    """

    def test_a_fresh_passing_verification_replaces_stale_metadata(self, tmp_path):
        verifier = FakeVerifier(passed=True)
        recovery, store, _, _ = build_recovery(tmp_path, verifier=verifier)
        store.create(
            make_feature(
                contract=_contract_with_content_check(),
                stale_verification={
                    "passed": False,
                    "summary": "11 of 15 checks failed on the preview",
                },
            )
        )

        result = recovery.recover("FEAT-00001", reason="recovering")

        assert result.recovered
        saved = store.get("FEAT-00001")
        assert saved.metadata["verification"]["passed"] is True
        assert "11 of 15" not in saved.metadata["verification"].get("summary", "")

    def test_verification_is_bound_to_the_exact_commit_and_deployment(self, tmp_path):
        preview = FakePreview(
            FakeObservation(url="https://exact-preview.example.vercel.app/")
        )
        verifier = FakeVerifier(passed=True)
        recovery, store, _, _ = build_recovery(
            tmp_path, preview=preview, verifier=verifier
        )
        store.create(make_feature(contract=_contract_with_content_check()))

        recovery.recover("FEAT-00001", reason="recovering")

        assert verifier.calls[0]["commit_sha"] == HEAD_SHA
        assert verifier.calls[0]["preview_url"] == "https://exact-preview.example.vercel.app/"
        saved = store.get("FEAT-00001")
        assert saved.metadata["verification"]["commit_sha"] == HEAD_SHA

    def test_a_failing_fresh_verification_refuses_and_does_not_touch_stored_evidence(
        self, tmp_path
    ):
        verifier = FakeVerifier(passed=False)
        recovery, store, _, _ = build_recovery(tmp_path, verifier=verifier)
        original_verification = {"passed": True, "summary": "an earlier, real pass"}
        store.create(
            make_feature(
                contract=_contract_with_content_check(),
                stale_verification=original_verification,
            )
        )

        result = recovery.recover("FEAT-00001", reason="recovering")

        assert not result.recovered
        assert result.refusals[0].code == "acceptance_failed"
        # Refused before _advance ever runs: nothing was overwritten with the
        # fresh failure, good or bad.
        saved = store.get("FEAT-00001")
        assert saved.metadata["verification"] == original_verification
        assert saved.state is FeatureState.BUILDING

    def test_no_verifier_configured_behaves_exactly_as_before(self, tmp_path):
        # Backward compatible: a recovery instance with no verifier wired
        # still recovers on local gates and preview reachability alone.
        recovery, store, _, _ = build_recovery(tmp_path, verifier=None)
        store.create(
            make_feature(
                contract=_contract_with_content_check(),
                stale_verification={"passed": False, "summary": "stale"},
            )
        )

        result = recovery.recover("FEAT-00001", reason="recovering")

        assert result.recovered
        # Untouched: with no verifier, recovery has no fresh evidence to
        # replace the stale result with, so it correctly leaves it alone
        # rather than inventing a verdict.
        assert store.get("FEAT-00001").metadata["verification"]["summary"] == "stale"

    def test_a_feature_with_no_browser_criteria_skips_the_browser_check(self, tmp_path):
        # A backend-only feature has nothing for a browser to check; the
        # verifier must not be asked to check nothing.
        verifier = FakeVerifier(passed=True)
        recovery, store, _, _ = build_recovery(tmp_path, verifier=verifier)
        store.create(make_feature())  # no contract at all

        result = recovery.recover("FEAT-00001", reason="recovering")

        assert result.recovered
        assert verifier.calls == []


# ---------------------------------------------------------------------------
# Refusals — each one independently provable
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_unknown_feature_refuses(self, tmp_path):
        recovery, store, _, _ = build_recovery(tmp_path)
        result = recovery.recover("FEAT-99999", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "not_found"

    def test_wrong_repository_refuses(self, tmp_path):
        recovery, store, _, _ = build_recovery(tmp_path)
        store.create(make_feature(repository="someone-else/other-repo"))
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "wrong_repository"

    def test_high_risk_refuses(self, tmp_path):
        recovery, store, _, _ = build_recovery(tmp_path)
        store.create(make_feature(risk="HIGH"))
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "risk_too_high"

    def test_no_pull_request_refuses(self, tmp_path):
        recovery, store, _, _ = build_recovery(tmp_path)
        store.create(make_feature(pr_number=0))
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "no_pull_request"

    def test_a_closed_pull_request_refuses(self, tmp_path):
        github = FakeGitHub(pr=FakeGitHub._open_pr(state="closed"))
        recovery, store, _, _ = build_recovery(tmp_path, github=github)
        store.create(make_feature())
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "stale_pull_request"

    def test_a_merged_pull_request_refuses(self, tmp_path):
        github = FakeGitHub(pr=FakeGitHub._open_pr(merged=True, state="closed"))
        recovery, store, _, _ = build_recovery(tmp_path, github=github)
        store.create(make_feature())
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "stale_pull_request"

    def test_a_pull_request_from_a_different_branch_refuses(self, tmp_path):
        github = FakeGitHub(
            pr=FakeGitHub._open_pr(head_ref="wiz/feature/some-other-feature")
        )
        recovery, store, _, _ = build_recovery(tmp_path, github=github)
        store.create(make_feature())
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "wrong_branch"

    def test_a_non_mergeable_pull_request_refuses(self, tmp_path):
        github = FakeGitHub(pr=FakeGitHub._open_pr(mergeable=False))
        recovery, store, _, _ = build_recovery(tmp_path, github=github)
        store.create(make_feature())
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "not_mergeable"

    def test_a_pr_number_mismatch_refuses(self, tmp_path):
        github = FakeGitHub(pr=FakeGitHub._open_pr(number=555))
        recovery, store, _, _ = build_recovery(tmp_path, github=github)
        store.create(make_feature())
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "pr_number_mismatch"

    def test_a_local_worktree_that_does_not_match_the_pr_head_refuses(self, tmp_path):
        recovery, store, _, _ = build_recovery(
            tmp_path, git_rev_parse_head=lambda wt: "deadbeef" * 5
        )
        store.create(make_feature())
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "sha_mismatch"

    def test_no_worktree_refuses_rather_than_trusting_a_prior_claim(self, tmp_path):
        recovery, store, _, _ = build_recovery(tmp_path)
        store.create(make_feature(worktree=""))
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "no_worktree"

    def test_failing_checks_refuse_even_though_a_prior_attempt_claimed_success(
        self, tmp_path
    ):
        recovery, store, _, _ = build_recovery(
            tmp_path,
            suite_result=FakeCheckResult(passed=False, summary="tests: failed"),
        )
        store.create(make_feature())
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "checks_failed"

    def test_a_secret_in_the_diff_refuses(self, tmp_path):
        recovery, store, _, _ = build_recovery(
            tmp_path,
            git_diff=lambda wt, base, head: (
                '+ const key = "sk-live-abcdefghijklmnopqrstuvwxyz123456";'
            ),
        )
        store.create(make_feature())
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "secret_detected"

    def test_a_missing_required_status_refuses(self, tmp_path):
        github = FakeGitHub(
            status={
                "state": "pending",
                "missing_required": ["Vercel"],
                "required": {"Vercel": "pending"},
            }
        )
        recovery, store, _, _ = build_recovery(
            tmp_path, github=github, required_status_contexts=["Vercel"]
        )
        store.create(make_feature())
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "status_not_green"

    def test_an_unusable_preview_refuses(self, tmp_path):
        preview = FakePreview(
            FakeObservation(usable=False, reason="deployment errored")
        )
        recovery, store, _, _ = build_recovery(tmp_path, preview=preview)
        store.create(make_feature())
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "preview_not_usable"

    def test_a_terminal_or_pre_build_state_refuses(self, tmp_path):
        recovery, store, _, _ = build_recovery(tmp_path)
        store.create(make_feature(state=FeatureState.PLANNING))
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "wrong_state"

    def test_ready_with_a_stale_recorded_commit_refuses_rather_than_no_opping(
        self, tmp_path
    ):
        recovery, store, _, _ = build_recovery(tmp_path)
        feature = make_feature(
            state=FeatureState.READY, attempt_commit_sha="stale" + "0" * 35
        )
        store.create(feature)
        result = recovery.recover("FEAT-00001", reason="x")
        assert not result.recovered
        assert result.refusals[0].code == "already_ready_mismatch"

    def test_a_refusal_never_touches_the_stored_state(self, tmp_path):
        recovery, store, _, _ = build_recovery(
            tmp_path, suite_result=FakeCheckResult(passed=False)
        )
        store.create(make_feature())
        recovery.recover("FEAT-00001", reason="x")
        saved = store.get("FEAT-00001")
        assert saved.state is FeatureState.BUILDING
        assert saved.history == []

    def test_recovery_never_grants_shipping_authority(self, tmp_path):
        """READY is the ceiling; nothing here can reach MERGING or beyond."""
        recovery, store, _, _ = build_recovery(tmp_path)
        store.create(make_feature())
        result = recovery.recover("FEAT-00001", reason="x")
        assert result.state == "READY"
        assert not hasattr(recovery, "ship")
