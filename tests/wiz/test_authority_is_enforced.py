"""The channel authority ceiling is enforced in the assembled system.

Both authority gates read their collaborator as Optional and skip the check
entirely when it is None:

    FeaturePipeline._authority_denial:  ``if self.policy is None: return ""``
    evaluate_shipping:                  ``if authority is not None:``

assemble() built the pipeline and the shipper without either. So in the real
system the channel ceiling did not exist: the gate that stops a request
arriving over Telegram or voice from having code written for it, and the gate
that stops one reaching a production merge, were both structurally absent --
while every unit test of them passed, because those tests construct the
pipeline and the shipper with an AuthorityPolicy themselves.

That is the failure mode these tests exist for. They assert the wiring at the
place that does the wiring, not the behaviour of a hand-built object.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openjarvis.wiz.authority import Actor, Authority, AuthorityPolicy, Channel
from openjarvis.wiz.brain import Request
from openjarvis.wiz.runtime import build_wiz


class _FakePipeline:
    def __init__(self):
        self.policy = None
        self.journal = None
        self.owner_notifier = None
        self.postship = None
        self.shipper = _FakeShipper()


class _FakeShipper:
    def __init__(self):
        self.authority = None


class _FakeProduct:
    """Only what build_wiz's back-fill actually touches.

    Deliberately not a real ProductVerbs: the point is to observe what
    build_wiz assigns onto the pipeline and the shipper, without an engineering
    target, a git checkout or a feature database being configured.
    """

    def __init__(self):
        self.pipeline = _FakePipeline()
        self.memory = None
        self.runner = None

    def handlers(self):
        return {}


def _build(tmp_path: Path, product, policy=None):
    return build_wiz(home=tmp_path, product=product, policy=policy)


def _health(runtime):
    """describe_health as the Control Center asks it."""
    return runtime.describe_health(
        Request(
            text="",
            actor=Actor(
                actor_id="control_center",
                channel=Channel.CONTROL_CENTER,
                authenticated=True,
            ),
        )
    )


class TestTheAuthorityPolicyReachesTheGates:
    def test_the_pipeline_gets_the_policy(self, tmp_path):
        product = _FakeProduct()
        policy = AuthorityPolicy.default()
        _build(tmp_path, product, policy=policy)
        assert product.pipeline.policy is policy, (
            "FeaturePipeline.policy is still None, so _authority_denial "
            "returns '' for every build and the channel ceiling is a no-op"
        )

    def test_the_shipper_gets_the_policy(self, tmp_path):
        product = _FakeProduct()
        policy = AuthorityPolicy.default()
        _build(tmp_path, product, policy=policy)
        assert product.pipeline.shipper.authority is policy, (
            "FeatureShipper.authority is still None, so evaluate_shipping "
            "skips its authority gate and a production merge is ungated"
        )

    def test_a_caller_that_supplied_its_own_keeps_it(self, tmp_path):
        """Back-filled only when unset, exactly like the journal."""
        product = _FakeProduct()
        mine = AuthorityPolicy(grants={Channel.CLI: frozenset({Authority.CODE_WRITE})})
        product.pipeline.policy = mine
        product.pipeline.shipper.authority = mine
        _build(tmp_path, product, policy=AuthorityPolicy.default())
        assert product.pipeline.policy is mine
        assert product.pipeline.shipper.authority is mine

    def test_a_product_without_a_shipper_is_not_a_problem(self, tmp_path):
        """GitHub unconfigured is a normal shape, not an error."""
        product = _FakeProduct()
        product.pipeline.shipper = None
        _build(tmp_path, product, policy=AuthorityPolicy.default())
        assert product.pipeline.policy is not None


class TestTheDefaultPolicyGrantsNoFeatureWork:
    """Load-bearing for the test above: enforcing the gate is a real change.

    If the default policy happened to grant these, wiring it in would be
    invisible and these tests would prove nothing.
    """

    @pytest.mark.parametrize(
        "authority",
        [Authority.CODE_WRITE, Authority.PR_WRITE, Authority.PRODUCTION_CHANGE],
    )
    def test_no_channel_gets_it_by_default(self, authority):
        policy = AuthorityPolicy.default()
        for channel in Channel:
            assert authority not in policy.granted_to(channel), (
                f"{authority.value} is granted to {channel.value} by default; "
                "writing code must be an operator's deliberate opt-in"
            )

    def test_telegram_can_never_change_production_even_if_configured(self):
        """The ceiling, not the grant: this one is not configurable."""
        asking = AuthorityPolicy(
            grants={Channel.TELEGRAM: frozenset({Authority.PRODUCTION_CHANGE})}
        )
        assert Authority.PRODUCTION_CHANGE not in asking.granted_to(Channel.TELEGRAM)

    def test_voice_can_never_change_production_even_if_configured(self):
        asking = AuthorityPolicy(
            grants={Channel.VOICE: frozenset({Authority.PRODUCTION_CHANGE})}
        )
        assert Authority.PRODUCTION_CHANGE not in asking.granted_to(Channel.VOICE)


class TestTheDoctorNamesTheGap:
    """A refused build must be diagnosable before it is refused.

    Enforcing the gate means a machine with no authority.json now refuses every
    build. ``authority_policy: "loaded"`` is a hardcoded string that would keep
    insisting everything was fine, so describe() now says what is missing.
    """

    def test_a_default_policy_reports_the_missing_grants(self, tmp_path):
        runtime = _build(tmp_path, _FakeProduct(), policy=AuthorityPolicy.default())
        described = _health(runtime)
        gaps = described["authority_gaps"]
        assert gaps, "a default policy cannot build anything; describe() said nothing"
        assert any("CODE_WRITE" in g for g in gaps)
        assert any("PRODUCTION_CHANGE" in g for g in gaps)
        assert described["authority_granted"], "the actual grants are not reported"

    def test_a_fully_granted_policy_reports_no_gaps(self, tmp_path):
        policy = AuthorityPolicy(
            grants={
                Channel.CONTROL_CENTER: frozenset(
                    {
                        Authority.CODE_WRITE,
                        Authority.PR_WRITE,
                        Authority.PRODUCTION_CHANGE,
                    }
                ),
                Channel.CLI: frozenset({Authority.CODE_WRITE}),
            }
        )
        runtime = _build(tmp_path, _FakeProduct(), policy=policy)
        assert _health(runtime)["authority_gaps"] == []
