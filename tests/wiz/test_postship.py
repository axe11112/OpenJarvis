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
)
from openjarvis.wiz.features.preview import PreviewObserver
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
