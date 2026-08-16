"""Tests for automatic merge — the last gate before the default branch.

The structure is deliberate. One fixture builds a repair that genuinely earned a
merge: verified against a preview, every local check green, scope clean, the
right pull request at the right commit on the right base. Every other test takes
that fixture and breaks exactly one thing.

That shape is the point. A gate suite where each test constructs its own
scenario proves that some input refuses; this proves that *only* the intended
input passes, because the happy path and the refusal differ by one field. If a
gate is ever deleted, the test that removed its precondition starts passing and
fails the suite.

Nothing here touches the network. ``_FakeGitHub`` records what it was asked to
do, which is how the merge-verb and merge-SHA assertions are made — asserting on
the request rather than on the result is what catches a merge that lands the
wrong commit but reports success.
"""

from __future__ import annotations

import json

import pytest

from openjarvis.reliability.merge import (
    REQUIRED_CHECKS,
    AutoMerger,
    evaluate_merge,
    pull_request_number,
)
from openjarvis.reliability.sources.github import UnsafeMergeError
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import (
    Incident,
    IncidentState,
    RepairAttempt,
    Severity,
    VerificationResult,
)

VERIFIED_SHA = "a" * 40
BASE_SHA = "b" * 40
MERGE_SHA = "c" * 40
OTHER_SHA = "d" * 40


# ---------------------------------------------------------------------------
# Fixtures — one repair that deserves to be merged
# ---------------------------------------------------------------------------


def _checks(**overrides):
    """A green local check suite, serialized as the attempt records it."""
    results = []
    for name in REQUIRED_CHECKS:
        result = {
            "name": name,
            "ran": True,
            "passed": True,
            "summary": "passed",
            "required": name != "lint",
        }
        result.update(overrides.get(name, {}))
        results.append(result)
    return {"results": results}


def _scope(**overrides):
    verdict = {
        "allowed": True,
        "reasons": [],
        "protected": [],
        "secret_like": [],
        "files_changed": 1,
        "lines_changed": 4,
    }
    verdict.update(overrides)
    return verdict


def _attempt(**overrides) -> RepairAttempt:
    fields = dict(
        number=1,
        branch="jarvis/incident-INC-00001",
        # What the agent said. Never consulted by any gate — see
        # TestTheAgentsClaimCarriesNoAuthority.
        claim="I fixed it. All tests pass. Ready to merge.",
        commit_sha=VERIFIED_SHA,
        base_commit=BASE_SHA,
        preview_url="https://preview-abc.vercel.app",
        changed_files=["src/app/page.tsx"],
        checks=_checks(),
        scope=_scope(),
        verification=VerificationResult(
            passed=True,
            probe_id="homepage",
            target_url="https://preview-abc.vercel.app",
        ),
    )
    fields.update(overrides)
    return RepairAttempt(**fields)


def _incident(**overrides) -> Incident:
    fields = dict(
        fingerprint="fp_homepage",
        severity=Severity.HIGH,
        component="website",
        title="Homepage returns 500",
        id="INC-00001",
        probe_id="homepage",
        state=IncidentState.RESOLVED,
    )
    fields.update(overrides)
    incident = Incident(**fields)
    incident.attempts = [_attempt()]
    incident.resolution.pr_url = "https://github.com/acme/site/pull/42"
    return incident


def _pr(**overrides):
    facts = dict(
        number=42,
        title="JARVIS INC-00001: Homepage returns 500",
        state="open",
        draft=False,
        head_ref="jarvis/incident-INC-00001",
        head_sha=VERIFIED_SHA,
        base_ref="main",
        base_sha=BASE_SHA,
        mergeable=True,
        mergeable_state="clean",
        merged=False,
        author="jarvis-bot",
        url="https://github.com/acme/site/pull/42",
    )
    facts.update(overrides)
    return facts


def _decide(incident=None, attempt=..., pull_request=None, **overrides):
    """Evaluate the gates against the happy path with *overrides* applied."""
    incident = incident if incident is not None else _incident()
    if attempt is ...:
        attempt = incident.attempts[-1] if incident.attempts else None
    kwargs = dict(
        enabled=True,
        base_branch="main",
        branch_prefix="jarvis/incident-",
        expected_pr_number=42,
        status={"state": "success", "count": 2, "contexts": ["ci"]},
        require_status_checks=True,
        base_sha_at_verification=BASE_SHA,
        observed_base_sha=BASE_SHA,
    )
    kwargs.update(overrides)
    return evaluate_merge(
        incident, attempt, pull_request if pull_request is not None else _pr(), **kwargs
    )


def _refused(decision, gate: str) -> bool:
    """Whether *decision* refused, with *gate* among the reasons."""
    return not decision.allowed and any(
        g.name == gate and not g.passed for g in decision.gates
    )


class _FakeGitHub:
    """Records requests. The assertions are made against what it was asked."""

    def __init__(self, *, pr=None, status=None, base_sha=BASE_SHA, fail=None):
        self.repo = "acme/site"
        self._pr = pr if pr is not None else _pr()
        self._status = status or {"state": "success", "count": 2, "contexts": ["ci"]}
        self._base_sha = base_sha
        self._fail = fail
        self.merge_calls = []
        self.client = self

    # -- read side (also serves as the fake ResilientClient) --------------
    def get_pull_request(self, number):
        if self._fail == "read":
            raise RuntimeError("GitHub is unavailable")
        return dict(self._pr, number=number)

    def combined_status(self, sha):
        return dict(self._status)

    def get_json(self, path, default=None, **_kw):
        if "/git/ref/heads/" in path:
            return {"object": {"sha": self._base_sha}}
        return default

    # -- the one write ----------------------------------------------------
    def merge_pull_request(
        self, *, number, expected_head_sha, method, title="", message=""
    ):
        self.merge_calls.append(
            {
                "number": number,
                "expected_head_sha": expected_head_sha,
                "method": method,
                "title": title,
                "message": message,
            }
        )
        if self._fail == "merge":
            raise RuntimeError("409 head changed")
        return {"merged": True, "sha": MERGE_SHA, "message": "Pull Request merged"}


class _RecordingNotifier:
    def __init__(self):
        self.attempts = []
        self.outcomes = []

    def merge_attempt(self, incident, *, pr_number, head_sha, method):
        self.attempts.append((incident.id, pr_number, head_sha, method))
        return True

    def merge_outcome(self, incident, *, record):
        self.outcomes.append(record)
        return True


@pytest.fixture
def store(tmp_path):
    opened = IncidentStore(tmp_path / "incidents.db")
    yield opened
    opened.close()


# ---------------------------------------------------------------------------
# The happy path, so every refusal below is one field away from a merge
# ---------------------------------------------------------------------------


class TestTheHappyPath:
    def test_a_fully_verified_repair_may_merge(self):
        decision = _decide()
        assert decision.allowed, decision.reason
        assert decision.failures == []

    def test_every_gate_is_reported_not_just_the_first(self):
        """The record is for a human reading it later, not for the code."""
        decision = _decide()
        names = {g.name for g in decision.gates}
        for expected in (
            "merge_enabled",
            "incident_state",
            "not_flapping",
            "verified",
            "scope",
            "no_protected_paths",
            "preview_deployment",
            "original_reproduction",
            "pr_belongs_to_incident",
            "pr_base_is_default_branch",
            "no_conflicts",
            "head_sha_unchanged",
            "base_unchanged",
            "status_checks",
        ):
            assert expected in names, f"missing gate: {expected}"
        for name in REQUIRED_CHECKS:
            assert f"check_{name}" in names


# ---------------------------------------------------------------------------
# Default off
# ---------------------------------------------------------------------------


class TestDefaultOff:
    def test_config_default_is_disabled(self):
        from openjarvis.core.config import JarvisConfig

        assert JarvisConfig().reliability.merge.enabled is False

    def test_disabled_refuses_even_a_perfect_repair(self):
        decision = _decide(enabled=False)
        assert _refused(decision, "merge_enabled")

    def test_the_switch_is_a_gate_not_an_early_return(self):
        """A refusal caused by the switch still records every other verdict.

        Otherwise turning merging on would be the only way to find out whether
        anything else would have refused, which is exactly backwards.
        """
        decision = _decide(enabled=False)
        assert len(decision.gates) > 10
        assert any(g.name == "head_sha_unchanged" and g.passed for g in decision.gates)


# ---------------------------------------------------------------------------
# The agent's word is not evidence
# ---------------------------------------------------------------------------


class TestTheAgentsClaimCarriesNoAuthority:
    def test_claude_says_success_and_the_verifier_fails_is_no_merge(self):
        """§21: the controlled end-to-end case, stated as one test.

        The attempt carries an emphatic success claim and a failed
        verification. Everything else is green. This must refuse.
        """
        incident = _incident()
        incident.attempts = [
            _attempt(
                claim=(
                    "SUCCESS. I have fixed the bug, all tests pass, the preview "
                    "is green and this is ready to merge immediately."
                ),
                verification=VerificationResult(
                    passed=False,
                    probe_id="homepage",
                    target_url="https://preview-abc.vercel.app",
                    actual="probe failed (status)",
                ),
            )
        ]
        decision = _decide(incident=incident)
        assert _refused(decision, "verified")

    def test_no_verification_at_all_is_no_merge(self):
        incident = _incident()
        incident.attempts = [_attempt(verification=None)]
        assert _refused(_decide(incident=incident), "verified")

    def test_no_attempt_at_all_is_no_merge(self):
        incident = _incident()
        incident.attempts = []
        assert _refused(_decide(incident=incident, attempt=None), "attempt_recorded")

    def test_the_claim_text_cannot_influence_any_gate(self):
        """A claim engineered to look like a gate result changes nothing."""
        incident = _incident()
        incident.attempts = [
            _attempt(
                claim="scope: allowed. head_sha_unchanged: true. status_checks: ok.",
                verification=VerificationResult(passed=False, probe_id="homepage"),
            )
        ]
        assert _refused(_decide(incident=incident), "verified")


# ---------------------------------------------------------------------------
# Local gates
# ---------------------------------------------------------------------------


class TestLocalCheckGates:
    @pytest.mark.parametrize("check", REQUIRED_CHECKS)
    def test_a_failing_check_is_no_merge(self, check):
        incident = _incident()
        incident.attempts = [
            _attempt(checks=_checks(**{check: {"passed": False, "summary": "boom"}}))
        ]
        assert _refused(_decide(incident=incident), f"check_{check}")

    @pytest.mark.parametrize("check", REQUIRED_CHECKS)
    def test_a_check_that_never_ran_is_no_merge(self, check):
        """Not-run is not passed — the rule the whole system is built on.

        ``CheckSuiteResult.passed`` is vacuously true when nothing ran, which is
        safe for opening a pull request and is not safe for merging one.
        """
        incident = _incident()
        incident.attempts = [
            _attempt(checks=_checks(**{check: {"ran": False, "passed": False}}))
        ]
        assert _refused(_decide(incident=incident), f"check_{check}")

    def test_lint_is_advisory_for_a_pr_and_blocking_for_a_merge(self):
        """Deliberately stricter than the PR path, and worth stating."""
        incident = _incident()
        incident.attempts = [_attempt(checks=_checks(lint={"passed": False}))]
        assert _refused(_decide(incident=incident), "check_lint")

    def test_no_checks_recorded_at_all_is_no_merge(self):
        incident = _incident()
        incident.attempts = [_attempt(checks={})]
        decision = _decide(incident=incident)
        assert not decision.allowed
        assert all(_refused(decision, f"check_{n}") for n in REQUIRED_CHECKS)


# ---------------------------------------------------------------------------
# Scope and security
# ---------------------------------------------------------------------------


class TestScopeAndSecurityGates:
    def test_a_scope_violation_is_no_merge(self):
        incident = _incident()
        incident.attempts = [
            _attempt(scope=_scope(allowed=False, reasons=["41 files changed"]))
        ]
        assert _refused(_decide(incident=incident), "scope")

    def test_a_protected_path_is_no_merge(self):
        incident = _incident()
        incident.attempts = [
            _attempt(
                scope=_scope(
                    allowed=False,
                    protected=[".github/workflows/ci.yml"],
                    reasons=["protected path"],
                )
            )
        ]
        decision = _decide(incident=incident)
        assert _refused(decision, "no_protected_paths")

    def test_a_secret_like_path_is_no_merge(self):
        """The security scan's verdict, carried into the merge decision."""
        incident = _incident()
        incident.attempts = [
            _attempt(scope=_scope(allowed=False, secret_like=[".env.production"]))
        ]
        assert _refused(_decide(incident=incident), "no_secret_like_paths")

    def test_a_missing_scope_verdict_is_no_merge(self):
        """Absent evidence refuses; it is not read as "nothing was wrong"."""
        incident = _incident()
        incident.attempts = [_attempt(scope={})]
        assert _refused(_decide(incident=incident), "scope")


# ---------------------------------------------------------------------------
# Preview and reproduction
# ---------------------------------------------------------------------------


class TestPreviewGates:
    def test_no_preview_deployment_is_no_merge(self):
        incident = _incident()
        incident.attempts = [_attempt(preview_url="")]
        assert _refused(_decide(incident=incident), "preview_deployment")

    def test_verifying_a_different_probe_is_no_merge(self):
        """A green probe that is not the one that broke proves nothing."""
        incident = _incident()
        incident.attempts = [
            _attempt(
                verification=VerificationResult(
                    passed=True,
                    probe_id="sitemap-absolute-urls",
                    target_url="https://preview-abc.vercel.app",
                )
            )
        ]
        assert _refused(_decide(incident=incident), "original_reproduction")


# ---------------------------------------------------------------------------
# Time-of-check / time-of-use
# ---------------------------------------------------------------------------


class TestTheCommitMergedIsTheCommitVerified:
    def test_a_changed_pr_head_sha_is_no_merge(self):
        """Somebody pushed to the branch after verification."""
        decision = _decide(pull_request=_pr(head_sha=OTHER_SHA))
        assert _refused(decision, "head_sha_unchanged")

    def test_an_unknown_head_sha_is_no_merge(self):
        assert _refused(_decide(pull_request=_pr(head_sha="")), "head_sha_unchanged")

    def test_a_moved_base_branch_is_no_merge(self):
        """The fix was verified against a base that is no longer the base."""
        decision = _decide(observed_base_sha=OTHER_SHA)
        assert _refused(decision, "base_unchanged")

    def test_an_unreadable_base_is_no_merge(self):
        assert _refused(_decide(observed_base_sha=""), "base_unchanged")

    def test_the_verified_sha_is_what_is_sent_to_github(self, store):
        """Belt and braces: even if a gate let a mismatch through, the SHA
        handed to GitHub is the verified one, and GitHub refuses the rest."""
        incident = store.create(_incident())
        github = _FakeGitHub()
        AutoMerger(
            github=github, store=store, enabled=True, base_branch="main"
        ).merge_for(incident)
        assert github.merge_calls[0]["expected_head_sha"] == VERIFIED_SHA


# ---------------------------------------------------------------------------
# Pull-request identity
# ---------------------------------------------------------------------------


class TestOnlyJarvisOwnPullRequestForThisIncident:
    def test_a_pull_request_jarvis_did_not_open_is_no_merge(self):
        """A human's PR sitting on the repo is not JARVIS's to merge."""
        decision = _decide(
            pull_request=_pr(number=7, head_ref="feature/redesign", author="someone")
        )
        assert _refused(decision, "pr_belongs_to_incident")
        assert _refused(decision, "pr_head_is_incident_branch")

    def test_another_incidents_pull_request_is_no_merge(self):
        decision = _decide(
            pull_request=_pr(number=99, head_ref="jarvis/incident-INC-00009")
        )
        assert _refused(decision, "pr_belongs_to_incident")
        assert _refused(decision, "pr_head_is_incident_branch")

    def test_an_incident_with_no_recorded_pull_request_is_no_merge(self):
        incident = _incident()
        incident.resolution.pr_url = ""
        decision = _decide(incident=incident, expected_pr_number=0)
        assert _refused(decision, "pr_belongs_to_incident")

    def test_a_pull_request_targeting_something_other_than_main_is_no_merge(self):
        assert _refused(
            _decide(pull_request=_pr(base_ref="develop")), "pr_base_is_default_branch"
        )

    def test_pull_request_number_is_parsed_from_the_recorded_url(self):
        assert pull_request_number("https://github.com/acme/site/pull/42") == 42
        assert pull_request_number("") == 0
        assert pull_request_number("https://github.com/acme/site/issues/42") == 0


# ---------------------------------------------------------------------------
# Repository state
# ---------------------------------------------------------------------------


class TestRepositoryStateGates:
    def test_a_conflicting_pull_request_is_no_merge(self):
        decision = _decide(pull_request=_pr(mergeable=False, mergeable_state="dirty"))
        assert _refused(decision, "no_conflicts")

    def test_an_undetermined_mergeable_state_is_no_merge(self):
        """GitHub returns None while it computes. Unknown is not yes."""
        decision = _decide(pull_request=_pr(mergeable=None, mergeable_state="unknown"))
        assert _refused(decision, "no_conflicts")

    def test_a_blocked_pull_request_is_no_merge(self):
        """Branch protection saying no is a refusal, not an obstacle."""
        decision = _decide(pull_request=_pr(mergeable=True, mergeable_state="blocked"))
        assert _refused(decision, "no_conflicts")

    def test_a_closed_or_already_merged_pull_request_is_no_merge(self):
        assert _refused(_decide(pull_request=_pr(state="closed")), "pr_open")
        assert _refused(_decide(pull_request=_pr(merged=True)), "pr_open")

    def test_a_draft_pull_request_is_no_merge(self):
        assert _refused(_decide(pull_request=_pr(draft=True)), "pr_not_draft")

    def test_failing_ci_is_no_merge(self):
        decision = _decide(status={"state": "failure", "count": 3, "contexts": ["ci"]})
        assert _refused(decision, "status_checks")

    def test_pending_ci_is_no_merge(self):
        decision = _decide(status={"state": "pending", "count": 1, "contexts": ["ci"]})
        assert _refused(decision, "status_checks")

    def test_absent_ci_is_no_merge_by_default(self):
        """ "No CI ran" and "CI passed" are different facts."""
        decision = _decide(status={"state": "none", "count": 0, "contexts": []})
        assert _refused(decision, "status_checks")

    def test_absent_ci_can_be_opted_out_of_explicitly(self):
        """For repositories that genuinely run no CI — and it says so."""
        decision = _decide(
            status={"state": "none", "count": 0, "contexts": []},
            require_status_checks=False,
        )
        assert decision.allowed
        gate = next(g for g in decision.gates if g.name == "status_checks")
        assert "not required" in gate.detail


# ---------------------------------------------------------------------------
# Incident state
# ---------------------------------------------------------------------------


class TestIncidentStateGates:
    @pytest.mark.parametrize(
        "state",
        [
            IncidentState.HUMAN_REQUIRED,
            IncidentState.RECOVERY_REQUIRED,
            IncidentState.FAILED,
            IncidentState.ROLLED_BACK,
        ],
    )
    def test_an_incident_a_human_owns_is_no_merge(self, state):
        assert _refused(_decide(incident=_incident(state=state)), "incident_state")

    def test_a_flapping_incident_is_no_merge(self):
        """A check that alternates makes a green verification meaningless."""
        incident = _incident()
        incident.metadata["flapping"] = True
        assert _refused(_decide(incident=incident), "not_flapping")


# ---------------------------------------------------------------------------
# The orchestrator: audit trail, notifications, and the single write
# ---------------------------------------------------------------------------


class TestAutoMergerRecordsEveryDecision:
    def test_a_merge_is_chained_into_the_incident_history(self, store):
        incident = store.create(_incident())
        before = len(store.transitions_for(incident.id))
        github = _FakeGitHub()
        record = AutoMerger(github=github, store=store, enabled=True).merge_for(
            incident
        )

        assert record.merged is True
        assert record.merge_commit_sha == MERGE_SHA
        after = store.transitions_for(incident.id)
        assert len(after) == before + 1
        assert "merged PR #42" in after[-1].reason
        assert after[-1].actor == "jarvis-automerge"
        assert store.verify_chain()[0] is True

    def test_a_refusal_is_chained_too(self, store):
        """The interesting audit entry. A gate that refuses silently is a gate
        nobody can prove was ever consulted."""
        incident = store.create(_incident())
        before = len(store.transitions_for(incident.id))
        github = _FakeGitHub()
        record = AutoMerger(github=github, store=store, enabled=False).merge_for(
            incident
        )

        assert record.merged is False
        assert github.merge_calls == []
        after = store.transitions_for(incident.id)
        assert len(after) == before + 1
        assert "refused to merge" in after[-1].reason
        assert store.verify_chain()[0] is True

    def test_the_record_carries_every_identifier_asked_for(self, store):
        incident = store.create(_incident())
        github = _FakeGitHub()
        record = AutoMerger(github=github, store=store, enabled=True).merge_for(
            incident
        )
        data = record.to_dict()
        for key in (
            "incident_id",
            "pr_number",
            "verified_head_sha",
            "base_sha_at_verification",
            "base_sha_observed",
            "merge_commit_sha",
            "actor",
            "at",
            "decision",
        ):
            assert key in data, key
        assert data["incident_id"] == incident.id
        assert data["pr_number"] == 42
        assert data["verified_head_sha"] == VERIFIED_SHA
        assert data["merge_commit_sha"] == MERGE_SHA
        assert data["decision"]["gates"]

    def test_the_gate_by_gate_account_is_attached_as_evidence(self, store):
        incident = store.create(_incident())
        AutoMerger(github=_FakeGitHub(), store=store, enabled=False).merge_for(incident)
        reloaded = store.get(incident.id)
        note = next(e for e in reloaded.evidence if e.source == "merge_gate")
        payload = json.loads(note.content)
        assert payload["decision"]["allowed"] is False
        assert any(g["name"] == "merge_enabled" for g in payload["decision"]["gates"])

    def test_an_unreadable_pull_request_refuses_rather_than_raising(self, store):
        incident = store.create(_incident())
        github = _FakeGitHub(fail="read")
        record = AutoMerger(github=github, store=store, enabled=True).merge_for(
            incident
        )
        assert record.merged is False
        assert github.merge_calls == []
        assert "could not read" in record.error

    def test_a_github_refusal_at_merge_time_is_recorded_not_raised(self, store):
        """GitHub 409s when the head moved between the read and the call.

        That is the server closing the last TOCTOU window, and it must land in
        the record rather than as an exception in the watcher.
        """
        incident = store.create(_incident())
        github = _FakeGitHub(fail="merge")
        record = AutoMerger(github=github, store=store, enabled=True).merge_for(
            incident
        )
        assert record.merged is False
        assert "409" in record.error


class TestAutoMergerNotifies:
    def test_before_and_after_a_merge(self, store):
        incident = store.create(_incident())
        notifier = _RecordingNotifier()
        AutoMerger(
            github=_FakeGitHub(), store=store, enabled=True, notifier=notifier
        ).merge_for(incident)
        assert len(notifier.attempts) == 1
        assert notifier.attempts[0][1] == 42
        assert len(notifier.outcomes) == 1
        assert notifier.outcomes[0].merged is True

    def test_a_refusal_notifies_the_outcome_but_never_announces_an_attempt(self, store):
        """ "Merging now" must not be sent for a merge that never happens."""
        incident = store.create(_incident())
        notifier = _RecordingNotifier()
        AutoMerger(
            github=_FakeGitHub(), store=store, enabled=False, notifier=notifier
        ).merge_for(incident)
        assert notifier.attempts == []
        assert len(notifier.outcomes) == 1
        assert notifier.outcomes[0].merged is False

    def test_a_broken_notifier_cannot_block_a_decision(self, store):
        class _Broken:
            def merge_attempt(self, *a, **k):
                raise RuntimeError("telegram is down")

            def merge_outcome(self, *a, **k):
                raise RuntimeError("telegram is down")

        incident = store.create(_incident())
        record = AutoMerger(
            github=_FakeGitHub(), store=store, enabled=True, notifier=_Broken()
        ).merge_for(incident)
        assert record.merged is True

    def test_the_outcome_message_names_the_gates_that_refused(self):
        from openjarvis.reliability.notify import render_merge_outcome

        incident = _incident()
        github = _FakeGitHub()

        class _Store:
            def record_audit(self, *a, **k):
                return None

            def add_evidence(self, *a, **k):
                return None

        record = AutoMerger(github=github, store=_Store(), enabled=False).merge_for(
            incident
        )
        message = render_merge_outcome(incident, record=record)
        assert "merge refused" in message
        assert "merge_enabled" in message
        assert "nothing was deployed" in message.lower()

    def test_the_attempt_message_says_it_is_not_a_deployment(self):
        from openjarvis.reliability.notify import render_merge_attempt

        message = render_merge_attempt(
            _incident(), pr_number=42, head_sha=VERIFIED_SHA, method="squash"
        )
        assert "#42" in message
        assert "does not deploy" in message


# ---------------------------------------------------------------------------
# The write surface itself
# ---------------------------------------------------------------------------


class TestTheMergeCallIsNarrow:
    def _source(self):
        from openjarvis.reliability.sources.github import GitHubSource

        return GitHubSource(repo="acme/site", token_env="TEST_TOKEN")

    def test_squash_is_the_default_method(self, store):
        incident = store.create(_incident())
        github = _FakeGitHub()
        AutoMerger(github=github, store=store, enabled=True).merge_for(incident)
        assert github.merge_calls[0]["method"] == "squash"

    def test_an_invented_merge_method_is_refused(self):
        with pytest.raises(UnsafeMergeError):
            self._source().merge_pull_request(
                number=1, expected_head_sha=VERIFIED_SHA, method="force-push"
            )

    def test_merging_without_an_expected_sha_is_refused(self):
        """Without it the merge lands whatever is on the branch at the time."""
        with pytest.raises(UnsafeMergeError):
            self._source().merge_pull_request(
                number=1, expected_head_sha="", method="squash"
            )

    def test_merging_without_a_pull_request_number_is_refused(self):
        with pytest.raises(UnsafeMergeError):
            self._source().merge_pull_request(
                number=0, expected_head_sha=VERIFIED_SHA, method="squash"
            )

    def test_there_is_still_no_way_to_push_to_the_default_branch(self):
        """The new authority must not have widened anything else."""
        source = self._source()
        assert not hasattr(source, "push")
        assert not hasattr(source, "force_push")
        assert not hasattr(source, "update_ref")
        with pytest.raises(Exception):
            source.create_branch("main")


# ---------------------------------------------------------------------------
# Choosing which attempt is authoritative
# ---------------------------------------------------------------------------


class TestTheVerifiedAttemptIsTheOneThatCounts:
    def test_a_later_unverified_attempt_does_not_become_the_merge_candidate(
        self, store
    ):
        """Attempt 2 verified; attempt 3 recurred and did not. Merge attempt 2's
        commit or nothing — never attempt 3's."""
        incident = _incident()
        incident.attempts = [
            _attempt(number=1, verification=VerificationResult(passed=False)),
            _attempt(number=2, commit_sha=VERIFIED_SHA),
            _attempt(number=3, commit_sha=OTHER_SHA, verification=None),
        ]
        incident = store.create(incident)
        github = _FakeGitHub()
        record = AutoMerger(github=github, store=store, enabled=True).merge_for(
            incident
        )
        assert record.verified_head_sha == VERIFIED_SHA
        assert github.merge_calls[0]["expected_head_sha"] == VERIFIED_SHA
