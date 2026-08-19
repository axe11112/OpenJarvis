"""Feature shipping: a separate authority from repairing production."""

from __future__ import annotations

from openjarvis.wiz.authority import Authority, AuthorityPolicy, Channel
from openjarvis.wiz.features.model import FeatureRequest, FeatureState
from openjarvis.wiz.features.shipping import (
    FeatureShipper,
    FeatureShippingPolicy,
    evaluate_shipping,
    pull_request_body,
)

VERIFIED_SHA = "abc1234def5678901234567890abcdef12345678"


def ready_feature(risk="LOW", **overrides):
    feature = FeatureRequest(
        id="FEAT-00042",
        title="Add a download button",
        operator_request='Add a "Download report" button to /coach/summary',
        source="control_center",
        actor_id="operator",
        risk=risk,
        state=FeatureState.READY,
        branch="wiz/feature/FEAT-00042",
        preview_url="https://preview.app",
    )
    attempt = feature.next_attempt(at="t1")
    attempt.commit_sha = VERIFIED_SHA
    attempt.changed_files = ["src/components/Summary.tsx"]
    attempt.lines_changed = 40
    attempt.succeeded = True
    feature.metadata["verification"] = {
        "passed": True,
        "summary": "all 6 checks passed on the preview",
        "awaiting_a_person": [],
        "screenshots": {"desktop": ["a.png"], "mobile": ["b.png"]},
    }
    feature.metadata["gates"] = {"summary": "lint, tests and build passed"}
    for key, value in overrides.items():
        setattr(feature, key, value)
    return feature


def open_pr(sha=VERIFIED_SHA, state="open", mergeable=True):
    return {"state": state, "head_sha": sha, "mergeable": mergeable, "number": 7}


class TestTheDefaultIsToShipNothing:
    def test_a_missing_configuration_merges_nothing(self):
        # A missing config file must mean "ship nothing", never "ship whatever
        # the defaults happen to be".
        policy = FeatureShippingPolicy.from_mapping(None)
        assert not policy.merge_low_risk
        assert not policy.merge_medium_risk
        assert not policy.merge_allowed_for("HIGH")

    def test_a_verified_low_risk_feature_still_does_not_merge_by_default(self):
        decision = evaluate_shipping(
            ready_feature(), policy=FeatureShippingPolicy(), pull_request=open_pr()
        )
        assert not decision.allowed
        assert any(g.name == "risk_level_shippable" for g in decision.refusals)

    def test_opening_a_pull_request_is_the_one_thing_on_by_default(self):
        assert FeatureShippingPolicy().create_pull_request is True

    def test_an_unknown_setting_is_ignored_rather_than_honoured(self):
        policy = FeatureShippingPolicy.from_mapping({"merge_low": True})
        assert not policy.merge_low_risk


class TestItIsNotReliabilitysSwitch:
    def test_the_shipping_policy_shares_no_field_with_repair_merging(self):
        # The way this fails is not that somebody decides to conflate them; it
        # is that a refactor notices two similar booleans and merges them.
        shipping = set(FeatureShippingPolicy().to_dict())
        reliability_merge_settings = {
            "enabled",
            "require_status_checks",
            "branch_prefix",
            "base_branch",
        }
        assert shipping.isdisjoint(reliability_merge_settings)

    def test_there_is_no_way_to_configure_merging_high_risk_features(self):
        # HIGH always needs the operator, and the way to keep that true is to
        # give the configuration no way to say otherwise.
        policy = FeatureShippingPolicy.from_mapping(
            {"merge_high_risk": True, "merge_low_risk": True}
        )
        assert not policy.merge_allowed_for("HIGH")
        assert policy.to_dict()["merge_high_risk"] is False

    def test_a_high_risk_feature_needs_the_operator_even_when_everything_passed(self):
        decision = evaluate_shipping(
            ready_feature(risk="HIGH"),
            policy=FeatureShippingPolicy(merge_low_risk=True, merge_medium_risk=True),
            pull_request=open_pr(),
        )
        assert not decision.allowed
        assert "needs your approval" in decision.explain()

    def test_an_approved_high_risk_feature_may_pass_that_gate(self):
        decision = evaluate_shipping(
            ready_feature(risk="HIGH"),
            policy=FeatureShippingPolicy(),
            pull_request=open_pr(),
            operator_approved=True,
        )
        assert all(g.passed for g in decision.gates if g.name == "risk_level_shippable")

    def test_an_unclassified_risk_never_merges(self):
        decision = evaluate_shipping(
            ready_feature(risk="EXPERIMENTAL"),
            policy=FeatureShippingPolicy(merge_low_risk=True, merge_medium_risk=True),
            pull_request=open_pr(),
        )
        assert not decision.allowed


class TestOnlyTheVerifiedCommit:
    def test_a_branch_that_moved_since_verification_does_not_merge(self):
        # The whole reason for pinning the SHA: a push between verification and
        # merge means the thing that was proved is not the thing that ships.
        decision = evaluate_shipping(
            ready_feature(),
            policy=FeatureShippingPolicy(merge_low_risk=True),
            pull_request=open_pr(sha="9" * 40),
        )
        assert not decision.allowed
        assert "moved since I checked it" in decision.explain()

    def test_a_base_branch_that_moved_does_not_merge(self):
        decision = evaluate_shipping(
            ready_feature(),
            policy=FeatureShippingPolicy(merge_low_risk=True),
            pull_request=open_pr(),
            base_sha_at_verification="base1",
            observed_base_sha="base2",
        )
        assert not decision.allowed
        assert "branch this would merge into has changed" in decision.explain()

    def test_a_conflicted_pull_request_does_not_merge(self):
        decision = evaluate_shipping(
            ready_feature(),
            policy=FeatureShippingPolicy(merge_low_risk=True),
            pull_request=open_pr(mergeable=False),
        )
        assert not decision.allowed

    def test_a_closed_pull_request_does_not_merge(self):
        decision = evaluate_shipping(
            ready_feature(),
            policy=FeatureShippingPolicy(merge_low_risk=True),
            pull_request=open_pr(state="closed"),
        )
        assert not decision.allowed


class TestOnlyAVerifiedFeature:
    def test_a_feature_that_never_reached_ready_does_not_merge(self):
        feature = ready_feature()
        feature.state = FeatureState.VERIFYING
        decision = evaluate_shipping(
            feature,
            policy=FeatureShippingPolicy(merge_low_risk=True),
            pull_request=open_pr(),
        )
        assert not decision.allowed

    def test_a_feature_whose_acceptance_did_not_pass_does_not_merge(self):
        feature = ready_feature()
        feature.metadata["verification"]["passed"] = False
        decision = evaluate_shipping(
            feature,
            policy=FeatureShippingPolicy(merge_low_risk=True),
            pull_request=open_pr(),
        )
        assert not decision.allowed

    def test_a_feature_still_needing_a_person_does_not_merge(self):
        feature = ready_feature()
        feature.metadata["verification"]["awaiting_a_person"] = [
            "somebody reads the wording"
        ]
        decision = evaluate_shipping(
            feature,
            policy=FeatureShippingPolicy(merge_low_risk=True),
            pull_request=open_pr(),
        )
        assert not decision.allowed
        assert "reads the wording" in decision.explain()


class TestContinuousIntegration:
    def test_a_required_context_that_is_missing_refuses(self):
        # A verdict that does not answer the question asked is refused rather
        # than accepted on its summary.
        decision = evaluate_shipping(
            ready_feature(),
            policy=FeatureShippingPolicy(
                merge_low_risk=True, required_status_contexts=("ci/build",)
            ),
            pull_request=open_pr(),
            status={"contexts": {}},
        )
        assert not decision.allowed
        assert "ci/build is missing" in decision.explain()

    def test_all_required_contexts_green_and_everything_else_in_place_merges(self):
        decision = evaluate_shipping(
            ready_feature(),
            policy=FeatureShippingPolicy(
                merge_low_risk=True, required_status_contexts=("ci/build",)
            ),
            pull_request=open_pr(),
            status={"contexts": {"ci/build": "success"}},
        )
        assert decision.allowed, decision.explain()


class TestAuthorityHasTheLastWord:
    def test_a_channel_without_production_change_cannot_merge(self):
        authority = AuthorityPolicy(
            grants={Channel.CONTROL_CENTER: frozenset({Authority.PR_WRITE})}
        )
        decision = evaluate_shipping(
            ready_feature(),
            policy=FeatureShippingPolicy(merge_low_risk=True),
            authority=authority,
            pull_request=open_pr(),
        )
        assert not decision.allowed

    def test_a_voice_request_can_never_merge_whatever_the_policy_says(self):
        # Voice's ceiling is in source, not configuration.
        authority = AuthorityPolicy(
            grants={Channel.VOICE: frozenset({Authority.PRODUCTION_CHANGE})}
        )
        feature = ready_feature()
        feature.source = "voice"
        decision = evaluate_shipping(
            feature,
            policy=FeatureShippingPolicy(merge_low_risk=True),
            authority=authority,
            pull_request=open_pr(),
        )
        assert not decision.allowed


class TestPullRequests:
    class FakeGitHub:
        def __init__(self, fail=False):
            self.calls = []
            self.fail = fail

        def create_pull_request(self, **kwargs):
            self.calls.append(kwargs)
            if self.fail:
                raise RuntimeError("403 Forbidden")
            return {"html_url": "https://github.com/a/b/pull/7", "number": 7}

    def test_a_pull_request_is_opened_when_permitted(self):
        github = self.FakeGitHub()
        shipper = FeatureShipper(
            policy=FeatureShippingPolicy(),
            github=github,
            authority=AuthorityPolicy(
                grants={Channel.CONTROL_CENTER: frozenset({Authority.PR_WRITE})}
            ),
        )
        feature = ready_feature()
        result = shipper.open_pull_request(feature)
        assert result["created"]
        assert feature.pr_number == 7
        assert github.calls[0]["head"] == "wiz/feature/FEAT-00042"
        assert github.calls[0]["base"] == "main"

    def test_a_channel_without_pr_write_opens_nothing(self):
        github = self.FakeGitHub()
        shipper = FeatureShipper(
            policy=FeatureShippingPolicy(),
            github=github,
            authority=AuthorityPolicy(
                grants={Channel.CONTROL_CENTER: frozenset({Authority.READ})}
            ),
        )
        assert not shipper.open_pull_request(ready_feature())["created"]
        assert github.calls == []

    def test_a_refused_request_is_reported_not_raised(self):
        shipper = FeatureShipper(
            policy=FeatureShippingPolicy(), github=self.FakeGitHub(fail=True)
        )
        result = shipper.open_pull_request(ready_feature())
        assert not result["created"]
        assert "403" in result["reason"]

    def test_the_shipper_never_pushes_to_the_base_branch(self):
        # There is no method that could. The absence is the guarantee.
        assert not hasattr(FeatureShipper, "push_to_main")
        assert not hasattr(FeatureShipper, "commit_to_base")


class TestTheDescription:
    def test_it_says_what_was_asked_what_changed_and_what_proved_it(self):
        body = pull_request_body(ready_feature())
        assert "Download report" in body
        assert "src/components/Summary.tsx" in body
        assert "https://preview.app" in body
        assert "all 6 checks passed" in body

    def test_it_names_the_risk_level(self):
        body = pull_request_body(ready_feature(risk="MEDIUM"))
        assert "Risk: MEDIUM" in body

    def test_it_says_what_still_needs_a_person(self):
        feature = ready_feature()
        feature.metadata["verification"]["awaiting_a_person"] = ["check the wording"]
        assert "check the wording" in pull_request_body(feature)

    def test_it_carries_the_attribution_footer(self):
        assert "Generated by [Claude Code]" in pull_request_body(ready_feature())

    def test_it_stays_short(self):
        feature = ready_feature()
        feature.attempts[-1].changed_files = [f"src/file{i}.tsx" for i in range(500)]
        body = pull_request_body(feature, max_chars=3000)
        assert len(body) <= 3100

    def test_an_advisory_review_is_labelled_advisory(self):
        feature = ready_feature()
        feature.metadata["review"] = {"ran": True, "text": "Looks fine to me."}
        assert "advisory" in pull_request_body(feature).lower()
