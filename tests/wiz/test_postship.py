"""After the merge: proving production, and the boundary when it is not proven."""

from __future__ import annotations

from openjarvis.reliability.types import ProbeResult
from openjarvis.wiz.features.acceptance import contract_for
from openjarvis.wiz.features.model import FeatureRequest, FeatureState
from openjarvis.wiz.features.postship import (
    PostShipVerifier,
    ProductionFailure,
    complete,
    handoff_to_reliability,
    reconcile_external_merge,
    reverify_production,
)
from openjarvis.wiz.features.preview import PreviewObserver
from openjarvis.wiz.features.store import FeatureStore
from openjarvis.wiz.features.verification import FeatureVerifier

MERGE_SHA = "abc1234def5678901234567890abcdef12345678"


def shipped_feature(state=FeatureState.PRODUCTION_VERIFYING):
    feature = FeatureRequest(
        id="FEAT-00042",
        title="Add a download button",
        operator_request='Add a "Download report" button to /coach/summary',
        source="control_center",
        state=state,
        pr_url="https://github.com/a/b/pull/7",
    )
    attempt = feature.next_attempt(at="t1")
    attempt.commit_sha = MERGE_SHA
    attempt.changed_files = ["src/components/Summary.tsx"]
    feature.metadata["contract"] = contract_for(
        feature_id=feature.id, request=feature.operator_request, gates=["tests"]
    ).to_dict()
    return feature


class FakeVercel:
    def __init__(self, state="READY", target_seen=None):
        self.state = state
        self.targets = target_seen if target_seen is not None else []

    def list_deployments(self, *, limit=20, target="", state=""):
        self.targets.append(target)
        return [
            {
                "id": "dpl_prod",
                "state": self.state,
                "target": target,
                "url": "https://wize.app",
                "created_at": "2026-08-19T12:00:00+00:00",
                "commit_sha": MERGE_SHA,
                "branch": "main",
            }
        ]


class FakeBrowser:
    def __init__(self, success=True):
        self.success = success
        self.runs = []

    def run(self, spec, *, base_url="", evidence_dir=None, **kwargs):
        self.runs.append((spec, base_url, evidence_dir))
        return ProbeResult(
            probe_id=spec.id,
            success=self.success,
            error="" if self.success else "the page does not contain 'Download report'",
            metadata={"viewport": spec.metadata.get("viewport", "")},
        )


def observer(vercel, state="READY"):
    return PreviewObserver(
        vercel=vercel,
        sleep=lambda s: None,
        monotonic=lambda: 0.0,
        timeout_seconds=0.0,
        target="production",
    )


def verifier(browser, evidence_root=None):
    return FeatureVerifier(
        runner_factory=lambda vp: browser, evidence_root=evidence_root
    )


class TestProvingProduction:
    def test_production_is_checked_against_the_merge_commit(self):
        vercel = FakeVercel()
        browser = FakeBrowser()
        result = PostShipVerifier(
            deployments=observer(vercel), verifier=verifier(browser)
        ).verify(shipped_feature(), merge_commit_sha=MERGE_SHA)
        assert result.verified
        assert result.deployment.commit_sha == MERGE_SHA

    def test_it_looks_at_production_deployments_not_previews(self):
        # The whole point of this stage. Checking the preview again would prove
        # nothing about where users are.
        vercel = FakeVercel()
        PostShipVerifier(
            deployments=observer(vercel), verifier=verifier(FakeBrowser())
        ).verify(shipped_feature(), merge_commit_sha=MERGE_SHA)
        assert set(vercel.targets) == {"production"}

    def test_the_same_contract_is_used_as_against_the_preview(self):
        # A feature that passed one bar and was spared the other would make the
        # first bar meaningless.
        browser = FakeBrowser()
        PostShipVerifier(
            deployments=observer(FakeVercel()), verifier=verifier(browser)
        ).verify(shipped_feature(), merge_commit_sha=MERGE_SHA)
        assert browser.runs
        assert all(base == "https://wize.app" for _, base, _ in browser.runs)

    def test_production_screenshots_do_not_overwrite_the_preview_ones(self, tmp_path):
        # Comparing them is how somebody sees what changed between preview and
        # production.
        browser = FakeBrowser()
        PostShipVerifier(
            deployments=observer(FakeVercel()),
            verifier=verifier(browser, evidence_root=tmp_path),
        ).verify(shipped_feature(), merge_commit_sha=MERGE_SHA)
        dirs = {directory for _, _, directory in browser.runs}
        assert all("attempt-100" in d for d in dirs)

    def test_a_backend_feature_with_nothing_to_check_says_so(self):
        feature = shipped_feature()
        feature.metadata["contract"] = contract_for(
            feature_id=feature.id, request="bump the retry constant", gates=["tests"]
        ).to_dict()
        result = PostShipVerifier(
            deployments=observer(FakeVercel()), verifier=verifier(FakeBrowser())
        ).verify(feature, merge_commit_sha=MERGE_SHA)
        assert result.verified
        assert "nothing about this feature I could check" in result.reason


class TestWhenProductionDisagrees:
    def _failing(self, handler=None):
        return PostShipVerifier(
            deployments=observer(FakeVercel()),
            verifier=verifier(FakeBrowser(success=False)),
            handler=handler,
        )

    def test_a_failing_check_is_not_verified(self):
        handled = []
        result = self._failing(lambda f: handled.append(f) or "test").verify(
            shipped_feature(), merge_commit_sha=MERGE_SHA
        )
        assert not result.verified
        assert handled

    def test_the_failure_carries_what_somebody_else_needs(self):
        captured = []
        self._failing(lambda f: captured.append(f) or "test").verify(
            shipped_feature(), merge_commit_sha=MERGE_SHA
        )
        failure = captured[0]
        assert failure.feature_id == "FEAT-00042"
        assert failure.merge_commit_sha == MERGE_SHA
        assert failure.pr_url.endswith("/pull/7")
        assert failure.changed_files == ["src/components/Summary.tsx"]
        assert failure.failed_criteria

    def test_a_missing_deployment_is_a_failure_not_a_pass(self):
        result = PostShipVerifier(
            deployments=observer(FakeVercel(state="BUILDING")),
            verifier=verifier(FakeBrowser()),
            handler=lambda f: "test",
        ).verify(shipped_feature(), merge_commit_sha=MERGE_SHA)
        assert not result.verified

    def test_a_merge_with_no_commit_is_a_failure_not_a_pass(self):
        result = PostShipVerifier(
            deployments=observer(FakeVercel()), handler=lambda f: "test"
        ).verify(shipped_feature(), merge_commit_sha="")
        assert not result.verified
        assert "no commit" in result.reason

    def test_a_handler_that_itself_fails_leaves_the_failure_unhandled_loudly(self):
        def broken(failure):
            raise RuntimeError("the incident store is gone")

        result = self._failing(broken).verify(
            shipped_feature(), merge_commit_sha=MERGE_SHA
        )
        assert not result.verified
        assert result.handed_over_to == ""
        assert result.failure is not None

    def test_verification_never_raises(self):
        class Exploding:
            def observe(self, **kwargs):
                raise RuntimeError("boom")

        result = PostShipVerifier(
            deployments=Exploding(), handler=lambda f: "test"
        ).verify(shipped_feature(), merge_commit_sha=MERGE_SHA)
        assert not result.verified
        assert "boom" in result.reason


class TestTheBoundary:
    def test_the_failure_object_has_no_reliability_types_on_it(self):
        # It is a plain data object so that handing it over is a function call
        # rather than a dependency.
        failure = ProductionFailure(
            feature_id="FEAT-1", title="x", merge_commit_sha="abc", reason="y"
        )
        for value in failure.to_dict().values():
            assert isinstance(value, (str, list, int, float, type(None)))

    def test_the_handover_points_one_way_only(self):
        import inspect

        from openjarvis.wiz.features import postship

        source = inspect.getsource(postship)
        # wiz imports reliability, which is permitted. What must not exist is
        # anything registering a wiz callback into reliability.
        assert "from openjarvis.reliability" in source
        assert "reliability.register" not in source

    def test_reliability_still_does_not_import_wiz(self):
        from tests.wiz.test_dependency_direction import RELIABILITY, _offenders

        assert _offenders(RELIABILITY, "openjarvis.wiz") == []

    def test_the_handover_opens_an_incident(self, tmp_path):
        from openjarvis.reliability.store import IncidentStore

        store = IncidentStore(tmp_path / "incidents.db")
        failure = ProductionFailure(
            feature_id="FEAT-00042",
            title="Add a download button",
            merge_commit_sha=MERGE_SHA,
            reason="the button is not on the page",
            observed=["console_error: TypeError"],
            changed_files=["src/components/Summary.tsx"],
        )
        owner = handoff_to_reliability(failure, store_factory=lambda: store)
        assert owner.startswith("reliability")

        opened = IncidentStore(tmp_path / "incidents.db").list(limit=5)
        assert len(opened) == 1
        assert opened[0].metadata["feature_id"] == "FEAT-00042"
        assert opened[0].metadata["rollback"] == "a human decides"

    def test_repeated_failures_for_one_feature_share_a_fingerprint(self, tmp_path):
        from openjarvis.reliability.store import IncidentStore

        store = IncidentStore(tmp_path / "incidents.db")
        seen = set()
        for reason in ("the button is missing", "the page 500s"):
            failure = ProductionFailure(
                feature_id="FEAT-00042",
                title="Add a download button",
                merge_commit_sha=MERGE_SHA,
                reason=reason,
            )
            handoff_to_reliability(failure, store_factory=lambda: store)
        for incident in IncidentStore(tmp_path / "incidents.db").list(limit=10):
            seen.add(incident.fingerprint)
        assert len(seen) == 1

    def test_nothing_here_rolls_anything_back(self):
        # Reverting a change that is live in front of users is a judgement
        # about the product, and it belongs to a person.
        import inspect

        from openjarvis.wiz.features import postship

        source = inspect.getsource(postship)
        for word in ("revert(", "rollback(", "git revert", "reset --hard"):
            assert word not in source


class TestCompletion:
    def test_complete_only_when_production_agreed(self):
        from openjarvis.wiz.features.postship import PostShipResult

        feature = shipped_feature()
        complete(feature, PostShipResult(verified=True, reason="fine"), at="t")
        assert feature.state is FeatureState.COMPLETE

    def test_a_production_failure_needs_a_person(self):
        from openjarvis.wiz.features.postship import PostShipResult

        feature = shipped_feature()
        complete(
            feature,
            PostShipResult(verified=False, reason="broken", handed_over_to="rel"),
            at="t",
        )
        assert feature.state is FeatureState.HUMAN_REQUIRED

    def test_a_feature_not_in_that_window_is_left_alone(self):
        from openjarvis.wiz.features.postship import PostShipResult

        feature = shipped_feature(state=FeatureState.READY)
        complete(feature, PostShipResult(verified=True), at="t")
        assert feature.state is FeatureState.READY


class FakeGitHub:
    def __init__(self, *, merged=True, merge_commit_sha=MERGE_SHA, state="closed"):
        self.merged = merged
        self.merge_commit_sha = merge_commit_sha
        self.state = state
        self.calls = []

    def get_pull_request(self, number):
        self.calls.append(number)
        return {
            "number": number,
            "state": self.state,
            "merged": self.merged,
            "merge_commit_sha": self.merge_commit_sha,
        }


class BrokenGitHub:
    def get_pull_request(self, number):
        raise RuntimeError("github is down")


class FakeIncident:
    def __init__(self):
        from openjarvis.reliability.types import IncidentState

        self.id = "INC-1"
        self.state = IncidentState.DETECTED

    def can_transition_to(self, state):
        return True


class FakeIncidentStore:
    """Mirrors IncidentStore's contract: resolving goes through
    ``transition()``, the one audited, persisting call — never through
    ``incident.transition_to()`` plus a bare ``save()``, which would skip
    the hash-chained transition log.
    """

    def __init__(self, incident=None):
        self.incident = incident
        self.transitioned = []

    def find_by_fingerprint(self, fp):
        return self.incident

    def transition(self, incident, state, *, actor="", reason=""):
        incident.state = state
        self.transitioned.append((incident, state, actor, reason))
        return incident


class FakeJournal:
    def __init__(self):
        self.entries = []

    def record(self, **kwargs):
        self.entries.append(kwargs)


class FakeOwnerNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, feature, *, kind, reason):
        self.calls.append((feature.id, kind, reason))
        return True


def human_required_shipped_feature(*, pr_number=7):
    feature = shipped_feature(state=FeatureState.HUMAN_REQUIRED)
    feature.pr_number = pr_number
    return feature


class TestReverifyProduction:
    """The narrow way back in for a feature whose real merge landed but whose
    post-ship check did not agree — never merges, never touches application
    code, always re-reads the pull request fresh.
    """

    def _store(self, tmp_path, feature):
        store = FeatureStore(tmp_path / "features.db")
        store.create(feature)
        return store

    def _postship(self, *, success=True):
        return PostShipVerifier(
            deployments=observer(FakeVercel()),
            verifier=verifier(FakeBrowser(success=success)),
            handler=lambda f: "reliability (test)",
        )

    def test_requires_a_recorded_pull_request(self, tmp_path):
        feature = human_required_shipped_feature(pr_number=0)
        store = self._store(tmp_path, feature)
        result = reverify_production(
            feature.id, store=store, github=FakeGitHub(), postship=self._postship()
        )
        assert not result.recovered
        assert result.refusals[0].code == "no_pull_request"

    def test_requires_the_pull_request_to_actually_be_merged(self, tmp_path):
        feature = human_required_shipped_feature()
        store = self._store(tmp_path, feature)
        github = FakeGitHub(merged=False, state="open")
        result = reverify_production(
            feature.id, store=store, github=github, postship=self._postship()
        )
        assert not result.recovered
        assert result.refusals[0].code == "pull_request_not_merged"
        saved = store.get(feature.id)
        assert saved.state is FeatureState.HUMAN_REQUIRED

    def test_requires_a_merge_commit_sha(self, tmp_path):
        feature = human_required_shipped_feature()
        store = self._store(tmp_path, feature)
        github = FakeGitHub(merge_commit_sha="")
        result = reverify_production(
            feature.id, store=store, github=github, postship=self._postship()
        )
        assert not result.recovered
        assert result.refusals[0].code == "no_merge_commit_sha"

    def test_an_unreadable_pull_request_is_refused_not_raised(self, tmp_path):
        feature = human_required_shipped_feature()
        store = self._store(tmp_path, feature)
        result = reverify_production(
            feature.id, store=store, github=BrokenGitHub(), postship=self._postship()
        )
        assert not result.recovered
        assert result.refusals[0].code == "pull_request_unreadable"

    def test_a_feature_not_stuck_in_human_required_is_refused(self, tmp_path):
        feature = shipped_feature(state=FeatureState.READY)
        feature.pr_number = 7
        store = self._store(tmp_path, feature)
        result = reverify_production(
            feature.id, store=store, github=FakeGitHub(), postship=self._postship()
        )
        assert not result.recovered
        assert result.refusals[0].code == "wrong_state"

    def test_an_already_complete_feature_is_idempotent(self, tmp_path):
        feature = shipped_feature(state=FeatureState.COMPLETE)
        feature.pr_number = 7
        store = self._store(tmp_path, feature)
        github = FakeGitHub()
        result = reverify_production(
            feature.id, store=store, github=github, postship=self._postship()
        )
        assert result.recovered
        # Idempotent means genuinely inert, not just "reports success" —
        # it never even reads the pull request.
        assert github.calls == []

    def test_an_unknown_feature_is_refused(self, tmp_path):
        store = FeatureStore(tmp_path / "features.db")
        result = reverify_production(
            "FEAT-NOPE", store=store, github=FakeGitHub(), postship=self._postship()
        )
        assert not result.recovered
        assert result.refusals[0].code == "not_found"

    def test_a_successful_reverify_completes_the_feature(self, tmp_path):
        feature = human_required_shipped_feature()
        store = self._store(tmp_path, feature)
        journal = FakeJournal()
        result = reverify_production(
            feature.id,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(success=True),
            journal=journal,
            clock=lambda: "t2",
        )
        assert result.recovered
        assert result.state == "COMPLETE"
        saved = store.get(feature.id)
        assert saved.state is FeatureState.COMPLETE
        assert saved.metadata["production_verification"]
        kinds = [entry["kind"] for entry in journal.entries]
        assert kinds.count("feature.shipped") == 1
        assert "feature.production_reverify_started" in kinds

    def test_a_failed_reverify_stays_human_required(self, tmp_path):
        feature = human_required_shipped_feature()
        store = self._store(tmp_path, feature)
        journal = FakeJournal()
        result = reverify_production(
            feature.id,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(success=False),
            journal=journal,
        )
        assert not result.recovered
        assert result.state == "HUMAN_REQUIRED"
        saved = store.get(feature.id)
        assert saved.state is FeatureState.HUMAN_REQUIRED
        kinds = [entry["kind"] for entry in journal.entries]
        assert "feature.shipped" not in kinds
        assert "feature.production_unverified" in kinds

    def test_a_stale_deployment_cannot_complete(self, tmp_path):
        feature = human_required_shipped_feature()
        store = self._store(tmp_path, feature)
        postship = PostShipVerifier(
            deployments=observer(FakeVercel(state="BUILDING")),
            verifier=verifier(FakeBrowser()),
            handler=lambda f: "reliability (test)",
        )
        result = reverify_production(
            feature.id, store=store, github=FakeGitHub(), postship=postship
        )
        assert not result.recovered
        saved = store.get(feature.id)
        assert saved.state is FeatureState.HUMAN_REQUIRED

    def test_success_resolves_the_associated_incident(self, tmp_path):
        from openjarvis.reliability.types import IncidentState

        feature = human_required_shipped_feature()
        store = self._store(tmp_path, feature)
        incident = FakeIncident()
        incident_store = FakeIncidentStore(incident)
        reverify_production(
            feature.id,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(success=True),
            incident_store=incident_store,
        )
        assert incident.state is IncidentState.RESOLVED
        assert incident_store.transitioned
        assert incident_store.transitioned[0][0] is incident
        assert incident_store.transitioned[0][1] is IncidentState.RESOLVED

    def test_a_failed_reverify_leaves_the_incident_open(self, tmp_path):
        from openjarvis.reliability.types import IncidentState

        feature = human_required_shipped_feature()
        store = self._store(tmp_path, feature)
        incident = FakeIncident()
        incident_store = FakeIncidentStore(incident)
        reverify_production(
            feature.id,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(success=False),
            incident_store=incident_store,
        )
        assert incident.state is not IncidentState.RESOLVED
        assert incident_store.transitioned == []

    def test_with_no_incident_store_a_success_still_completes(self, tmp_path):
        # incident_store is optional: a feature reverified outside the
        # reliability system entirely must still be able to reach COMPLETE.
        feature = human_required_shipped_feature()
        store = self._store(tmp_path, feature)
        result = reverify_production(
            feature.id, store=store, github=FakeGitHub(), postship=self._postship(success=True)
        )
        assert result.recovered
        assert store.get(feature.id).state is FeatureState.COMPLETE

    def test_a_successful_reverify_notifies_the_owner_exactly_once(self, tmp_path):
        feature = human_required_shipped_feature()
        store = self._store(tmp_path, feature)
        notifier = FakeOwnerNotifier()
        reverify_production(
            feature.id,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(success=True),
            owner_notifier=notifier,
        )
        assert len(notifier.calls) == 1
        feature_id, kind, _ = notifier.calls[0]
        assert feature_id == feature.id
        assert kind == "feature.shipped"

    def test_a_failed_reverify_still_tells_the_owner_it_needs_them(self, tmp_path):
        feature = human_required_shipped_feature()
        store = self._store(tmp_path, feature)
        notifier = FakeOwnerNotifier()
        reverify_production(
            feature.id,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(success=False),
            owner_notifier=notifier,
        )
        assert len(notifier.calls) == 1
        assert notifier.calls[0][1] == "feature.production_unverified"

    def test_an_idempotent_already_complete_reverify_does_not_notify_again(self, tmp_path):
        feature = shipped_feature(state=FeatureState.COMPLETE)
        feature.pr_number = 7
        store = self._store(tmp_path, feature)
        notifier = FakeOwnerNotifier()
        reverify_production(
            feature.id,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(success=True),
            owner_notifier=notifier,
        )
        assert notifier.calls == []

    def test_a_refusal_before_verification_does_not_notify(self, tmp_path):
        feature = human_required_shipped_feature(pr_number=0)
        store = self._store(tmp_path, feature)
        notifier = FakeOwnerNotifier()
        reverify_production(
            feature.id,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(),
            owner_notifier=notifier,
        )
        assert notifier.calls == []

    def test_it_never_merges_or_touches_the_pull_request(self):
        import inspect

        from openjarvis.wiz.features import postship

        source = inspect.getsource(postship.reverify_production)
        for word in ("merge_pull_request", "create_pull_request", "push"):
            assert word not in source


# ---------------------------------------------------------------------------
# INC-00107 / FEAT-00030: reconciling a merge canonical ship() never performed
# ---------------------------------------------------------------------------


class TestReconcileExternalMerge:
    """A feature whose merge happened outside the pipeline entirely — a
    coding session's own gh pr merge, not a flaky post-ship recheck on a
    merge the pipeline itself made. Every test here starts from a feature
    with no tracked PR, matching real FEAT-00030.
    """

    def _store(self, tmp_path, feature):
        store = FeatureStore(tmp_path / "features.db")
        store.create(feature)
        return store

    def _postship(self, *, success=True):
        return PostShipVerifier(
            deployments=observer(FakeVercel()),
            verifier=verifier(FakeBrowser(success=success)),
            handler=lambda f: "reliability (test)",
        )

    def _unmerged_feature(self):
        return human_required_shipped_feature(pr_number=0)

    def test_refuses_without_owner_acknowledgement(self, tmp_path):
        feature = self._unmerged_feature()
        store = self._store(tmp_path, feature)
        result = reconcile_external_merge(
            feature.id,
            pr_number=251,
            owner_acknowledged=False,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(),
        )
        assert not result.recovered
        assert result.refusals[0].code == "owner_acknowledgement_required"
        # Refusing must not have touched anything.
        assert store.get(feature.id).state is FeatureState.HUMAN_REQUIRED
        assert store.get(feature.id).pr_number == 0

    def test_refuses_without_a_pr_number(self, tmp_path):
        feature = self._unmerged_feature()
        store = self._store(tmp_path, feature)
        result = reconcile_external_merge(
            feature.id,
            pr_number=0,
            owner_acknowledged=True,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(),
        )
        assert not result.recovered
        assert result.refusals[0].code == "no_pull_request"

    def test_refuses_an_unmerged_pr(self, tmp_path):
        feature = self._unmerged_feature()
        store = self._store(tmp_path, feature)
        result = reconcile_external_merge(
            feature.id,
            pr_number=251,
            owner_acknowledged=True,
            store=store,
            github=FakeGitHub(merged=False, state="open"),
            postship=self._postship(),
        )
        assert not result.recovered
        assert result.refusals[0].code == "pull_request_not_merged"

    def test_refuses_a_feature_not_stuck_in_human_required(self, tmp_path):
        feature = shipped_feature(state=FeatureState.READY)
        store = self._store(tmp_path, feature)
        result = reconcile_external_merge(
            feature.id,
            pr_number=251,
            owner_acknowledged=True,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(),
        )
        assert not result.recovered
        assert result.refusals[0].code == "wrong_state"

    def test_an_already_complete_feature_is_idempotent(self, tmp_path):
        feature = shipped_feature(state=FeatureState.COMPLETE)
        store = self._store(tmp_path, feature)
        github = FakeGitHub()
        result = reconcile_external_merge(
            feature.id,
            pr_number=251,
            owner_acknowledged=True,
            store=store,
            github=github,
            postship=self._postship(),
        )
        assert result.recovered
        assert github.calls == []

    def test_success_completes_and_records_the_shipping_path_honestly(self, tmp_path):
        feature = self._unmerged_feature()
        store = self._store(tmp_path, feature)
        result = reconcile_external_merge(
            feature.id,
            pr_number=251,
            owner_acknowledged=True,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(success=True),
        )
        assert result.recovered
        saved = store.get(feature.id)
        assert saved.state is FeatureState.COMPLETE
        assert saved.pr_number == 251
        assert saved.metadata["shipping_path"] == "external_bypass_reconciled"
        assert saved.metadata["external_bypass_pr_number"] == 251
        assert saved.metadata["external_bypass_merge_commit_sha"] == MERGE_SHA

    def test_success_never_journals_the_ordinary_shipped_kind(self, tmp_path):
        # "does NOT pretend ship() performed the merge" — the audit trail
        # must say so, not read identically to an ordinary shipped feature.
        feature = self._unmerged_feature()
        store = self._store(tmp_path, feature)
        journal = FakeJournal()
        reconcile_external_merge(
            feature.id,
            pr_number=251,
            owner_acknowledged=True,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(success=True),
            journal=journal,
        )
        kinds = [e["kind"] for e in journal.entries]
        assert "feature.shipped" not in kinds
        assert "feature.external_bypass_reconciliation_started" in kinds
        assert "feature.external_bypass_reconciled" in kinds

    def test_a_failed_reverify_stays_human_required_and_unmarked(self, tmp_path):
        feature = self._unmerged_feature()
        store = self._store(tmp_path, feature)
        result = reconcile_external_merge(
            feature.id,
            pr_number=251,
            owner_acknowledged=True,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(success=False),
        )
        assert not result.recovered
        saved = store.get(feature.id)
        assert saved.state is FeatureState.HUMAN_REQUIRED
        assert "shipping_path" not in saved.metadata

    def test_success_resolves_the_associated_incident(self, tmp_path):
        from openjarvis.reliability.types import IncidentState

        feature = self._unmerged_feature()
        store = self._store(tmp_path, feature)
        incident = FakeIncident()
        incident_store = FakeIncidentStore(incident)
        reconcile_external_merge(
            feature.id,
            pr_number=251,
            owner_acknowledged=True,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(success=True),
            incident_store=incident_store,
        )
        assert incident.state is IncidentState.RESOLVED

    def test_a_failed_reverify_leaves_the_incident_open(self, tmp_path):
        from openjarvis.reliability.types import IncidentState

        feature = self._unmerged_feature()
        store = self._store(tmp_path, feature)
        incident = FakeIncident()
        incident_store = FakeIncidentStore(incident)
        reconcile_external_merge(
            feature.id,
            pr_number=251,
            owner_acknowledged=True,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(success=False),
            incident_store=incident_store,
        )
        assert incident.state is not IncidentState.RESOLVED

    def test_success_notifies_the_owner_exactly_once(self, tmp_path):
        feature = self._unmerged_feature()
        store = self._store(tmp_path, feature)
        notifier = FakeOwnerNotifier()
        reconcile_external_merge(
            feature.id,
            pr_number=251,
            owner_acknowledged=True,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(success=True),
            owner_notifier=notifier,
        )
        assert len(notifier.calls) == 1
        assert notifier.calls[0][1] == "feature.external_bypass_reconciled"

    def test_the_real_owner_notifier_actually_sends_for_this_kind(self, tmp_path):
        # The FakeOwnerNotifier above proves the *call* happens with the
        # right kind, not that the real FeatureOwnerNotifier recognizes it —
        # found the hard way on FEAT-00030 itself: the fake test passed while
        # the real notifier silently dropped the message, because
        # "feature.external_bypass_reconciled" was in neither SUCCESS_KIND
        # nor NEEDS_OWNER_KINDS. This uses the real class end to end.
        from openjarvis.wiz.features.notify import FeatureOwnerNotifier

        feature = self._unmerged_feature()
        store = self._store(tmp_path, feature)
        sent = []
        notifier = FeatureOwnerNotifier(
            send=sent.append, ledger_path=tmp_path / "ledger.json"
        )
        reconcile_external_merge(
            feature.id,
            pr_number=251,
            owner_acknowledged=True,
            store=store,
            github=FakeGitHub(),
            postship=self._postship(success=True),
            owner_notifier=notifier,
        )
        assert len(sent) == 1
        assert "it's live" in sent[0]

    def test_it_never_merges_or_creates_a_pull_request_itself(self):
        import inspect

        from openjarvis.wiz.features import postship

        source = inspect.getsource(postship.reconcile_external_merge)
        for word in ("merge_pull_request", "create_pull_request"):
            assert word not in source
