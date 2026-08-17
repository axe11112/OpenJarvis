"""The authority model, tested for the properties it is supposed to guarantee.

These are not tests that the code does what it does. Each one names a promise
made in the brief and fails if that promise stops being true.
"""

from __future__ import annotations

import json

import pytest

from openjarvis.wiz.authority import (
    Actor,
    Authority,
    AuthorityPolicy,
    CHANNEL_CEILING,
    Channel,
    ceiling_for,
    expand,
)


def _actor(channel: Channel, *, authenticated: bool = True) -> Actor:
    return Actor(actor_id="operator", channel=channel, authenticated=authenticated)


class TestDenyByDefault:
    """An authority nobody granted is an authority nobody has."""

    def test_an_empty_policy_grants_nothing(self):
        policy = AuthorityPolicy()
        for channel in Channel:
            for authority in Authority:
                decision = policy.decide(_actor(channel), authority)
                assert not decision.allowed

    def test_the_default_policy_grants_no_write_authority_anywhere(self):
        policy = AuthorityPolicy.default()
        for channel in Channel:
            for authority in (
                Authority.CODE_WRITE,
                Authority.PR_WRITE,
                Authority.PRODUCTION_CHANGE,
                Authority.SECRET_ACCESS,
            ):
                assert not policy.decide(_actor(channel), authority).allowed

    def test_a_refusal_explains_itself(self):
        decision = AuthorityPolicy.default().decide(
            _actor(Channel.VOICE), Authority.CODE_WRITE
        )
        assert not decision
        assert decision.reason


class TestChannelCeilingsAreStructural:
    """Configuration may narrow a ceiling. It may never raise one."""

    def test_voice_can_never_change_production(self):
        # Written the way an operator would write it if they were trying to.
        policy = AuthorityPolicy(
            grants={Channel.VOICE: frozenset({Authority.PRODUCTION_CHANGE})}
        )
        assert not policy.decide(
            _actor(Channel.VOICE), Authority.PRODUCTION_CHANGE
        ).allowed
        assert Authority.PRODUCTION_CHANGE not in policy.granted_to(Channel.VOICE)

    def test_telegram_can_never_change_production(self):
        policy = AuthorityPolicy(
            grants={Channel.TELEGRAM: frozenset({Authority.PRODUCTION_CHANGE})}
        )
        assert not policy.decide(
            _actor(Channel.TELEGRAM), Authority.PRODUCTION_CHANGE
        ).allowed

    def test_a_scheduled_task_can_never_inherit_production_authority(self):
        # §33: no scheduled task may automatically inherit production write
        # authority. Not "does not by default" — cannot.
        policy = AuthorityPolicy(
            grants={Channel.SCHEDULER: frozenset({Authority.PRODUCTION_CHANGE})}
        )
        assert not policy.decide(
            _actor(Channel.SCHEDULER), Authority.PRODUCTION_CHANGE
        ).allowed

    def test_only_the_control_center_may_ever_change_production(self):
        permitted = {
            channel
            for channel, ceiling in CHANNEL_CEILING.items()
            if Authority.PRODUCTION_CHANGE in ceiling
        }
        assert permitted == {Channel.CONTROL_CENTER}

    def test_no_channel_may_ever_access_secrets(self):
        for channel, ceiling in CHANNEL_CEILING.items():
            assert Authority.SECRET_ACCESS not in ceiling, channel

    def test_a_grant_above_the_ceiling_is_dropped_not_honoured(self):
        policy = AuthorityPolicy(
            grants={
                Channel.VOICE: frozenset(
                    {Authority.CODE_WRITE, Authority.PRODUCTION_CHANGE}
                )
            }
        )
        granted = policy.granted_to(Channel.VOICE)
        assert Authority.CODE_WRITE in granted
        assert Authority.PRODUCTION_CHANGE not in granted


class TestAuthentication:
    """An unverified requester reads, and does nothing else."""

    @pytest.mark.parametrize("channel", list(Channel))
    def test_an_unauthenticated_actor_cannot_act(self, channel):
        policy = AuthorityPolicy(
            grants={channel: ceiling_for(channel)}
        )
        for authority in ceiling_for(channel):
            decision = policy.decide(
                _actor(channel, authenticated=False), authority
            )
            if authority is Authority.READ:
                assert decision.allowed
            else:
                assert not decision.allowed


class TestImplication:
    """Granting the power to write code implies the power to read it."""

    def test_code_write_implies_read(self):
        assert Authority.READ in expand({Authority.CODE_WRITE})

    def test_nothing_implies_production_change(self):
        for authority in Authority:
            if authority is Authority.PRODUCTION_CHANGE:
                continue
            assert Authority.PRODUCTION_CHANGE not in expand({authority})

    def test_nothing_implies_secret_access(self):
        for authority in Authority:
            if authority is Authority.SECRET_ACCESS:
                continue
            assert Authority.SECRET_ACCESS not in expand({authority})


class TestThePolicyCannotWidenItself:
    """§23: the autonomous system cannot grant itself more autonomy."""

    def test_there_is_no_method_that_adds_authority(self):
        forbidden = {"grant", "add", "allow", "widen", "escalate", "elevate"}
        surface = {name for name in dir(AuthorityPolicy) if not name.startswith("_")}
        assert not (surface & forbidden)

    def test_the_grants_mapping_cannot_be_replaced(self):
        policy = AuthorityPolicy.default()
        with pytest.raises(Exception):
            policy.grants = {Channel.VOICE: frozenset({Authority.PRODUCTION_CHANGE})}

    def test_mutating_the_returned_grants_does_not_change_the_policy(self):
        policy = AuthorityPolicy.default()
        granted = policy.granted_to(Channel.VOICE)
        assert isinstance(granted, frozenset)  # nothing to mutate in the first place
        assert not policy.decide(
            _actor(Channel.VOICE), Authority.CODE_WRITE
        ).allowed


class TestLoading:
    def test_a_missing_file_is_the_default_policy(self, tmp_path):
        policy = AuthorityPolicy.load(tmp_path / "nothing.json")
        assert policy.to_mapping() == AuthorityPolicy.default().to_mapping()

    def test_a_malformed_file_falls_back_rather_than_guessing(self, tmp_path):
        path = tmp_path / "authority.json"
        path.write_text("{not json")
        policy = AuthorityPolicy.load(path)
        assert policy.to_mapping() == AuthorityPolicy.default().to_mapping()

    def test_a_valid_file_is_honoured(self, tmp_path):
        path = tmp_path / "authority.json"
        path.write_text(
            json.dumps({"grants": {"control_center": ["CODE_WRITE", "PR_WRITE"]}})
        )
        policy = AuthorityPolicy.load(path)
        assert policy.decide(
            _actor(Channel.CONTROL_CENTER), Authority.PR_WRITE
        ).allowed
        # And nothing was granted to anyone else by omission.
        assert not policy.decide(_actor(Channel.CLI), Authority.PR_WRITE).allowed

    def test_an_unknown_authority_name_is_an_error_not_a_shrug(self):
        with pytest.raises(ValueError):
            AuthorityPolicy.from_mapping({"cli": ["ADMIN"]})

    def test_an_unknown_channel_name_is_an_error(self):
        with pytest.raises(ValueError):
            AuthorityPolicy.from_mapping({"carrier_pigeon": ["READ"]})


class TestUnknownChannels:
    def test_every_channel_has_a_ceiling(self):
        # A channel added without a ceiling would fall through to "no
        # authority", which is safe — but silently useless, which is not.
        for channel in Channel:
            assert channel in CHANNEL_CEILING
