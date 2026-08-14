"""Tests for the health-state vocabulary.

These encode §19 of the Phase 10 brief: JARVIS must never report HEALTHY for
something it did not actually check.
"""

from __future__ import annotations

import pytest

from openjarvis.reliability.health import (
    CheckResult,
    HealthState,
    aggregate,
    worst,
)


class TestHealthState:
    def test_only_healthy_is_good_news(self):
        assert HealthState.HEALTHY.is_good_news
        for state in HealthState:
            if state is not HealthState.HEALTHY:
                assert not state.is_good_news, state

    def test_was_checked_excludes_the_blind_states(self):
        assert HealthState.HEALTHY.was_checked
        assert HealthState.DEGRADED.was_checked
        assert HealthState.FAILED.was_checked
        assert not HealthState.UNKNOWN.was_checked
        assert not HealthState.NOT_CONFIGURED.was_checked
        assert not HealthState.NOT_CHECKED.was_checked

    def test_only_failed_justifies_an_incident(self):
        """A missing credential must never open an incident about production."""
        assert HealthState.FAILED.justifies_incident
        for state in HealthState:
            if state is not HealthState.FAILED:
                assert not state.justifies_incident, state

    def test_every_state_has_an_icon(self):
        assert all(state.icon for state in HealthState)


class TestWorst:
    def test_failed_dominates(self):
        assert (
            worst([HealthState.HEALTHY, HealthState.FAILED, HealthState.UNKNOWN])
            is HealthState.FAILED
        )

    def test_unknown_beats_healthy(self):
        """An unverified check must not be averaged away into a green result."""
        assert worst([HealthState.HEALTHY, HealthState.UNKNOWN]) is HealthState.UNKNOWN

    def test_not_configured_beats_healthy(self):
        assert (
            worst([HealthState.HEALTHY, HealthState.NOT_CONFIGURED])
            is HealthState.NOT_CONFIGURED
        )

    def test_empty_is_not_checked(self):
        assert worst([]) is HealthState.NOT_CHECKED


class TestDeriveState:
    def _check(self, **capabilities):
        parent = CheckResult(name="parent")
        for name, state in capabilities.items():
            parent.add(CheckResult(name=name, state=state))
        return parent

    def test_all_healthy(self):
        parent = self._check(a=HealthState.HEALTHY, b=HealthState.HEALTHY)
        assert parent.derive_state() is HealthState.HEALTHY

    def test_partial_permissions_are_degraded_not_healthy(self):
        """The headline case: a token that reads commits but not Actions."""
        parent = self._check(commits=HealthState.HEALTHY, actions=HealthState.UNKNOWN)
        assert parent.derive_state() is HealthState.DEGRADED

    def test_any_failure_dominates(self):
        parent = self._check(a=HealthState.HEALTHY, b=HealthState.FAILED)
        assert parent.derive_state() is HealthState.FAILED

    def test_everything_unconfigured(self):
        parent = self._check(a=HealthState.NOT_CONFIGURED, b=HealthState.NOT_CONFIGURED)
        assert parent.derive_state() is HealthState.NOT_CONFIGURED

    def test_nothing_checked_is_not_healthy(self):
        parent = self._check(a=HealthState.UNKNOWN, b=HealthState.NOT_CONFIGURED)
        assert parent.derive_state() is not HealthState.HEALTHY

    def test_no_capabilities_keeps_its_own_state(self):
        check = CheckResult(name="x", state=HealthState.HEALTHY)
        assert check.derive_state() is HealthState.HEALTHY

    def test_unchecked_capabilities_are_listed(self):
        parent = self._check(
            ok=HealthState.HEALTHY,
            blind=HealthState.UNKNOWN,
            absent=HealthState.NOT_CONFIGURED,
        )
        assert parent.unchecked_capabilities == ["absent", "blind"]


class TestConstructors:
    def test_not_configured_names_what_is_missing(self):
        result = CheckResult.not_configured("vercel", missing="VERCEL_PROJECT")
        assert result.state is HealthState.NOT_CONFIGURED
        assert "VERCEL_PROJECT" in result.summary

    def test_unknown_explains_itself(self):
        result = CheckResult.unknown("github", reason="403 from the API")
        assert result.state is HealthState.UNKNOWN
        assert "403" in result.summary

    def test_round_trip(self):
        parent = CheckResult(name="p", state=HealthState.DEGRADED, summary="s")
        parent.add(CheckResult(name="c", state=HealthState.UNKNOWN))
        payload = parent.to_dict()
        assert payload["state"] == "DEGRADED"
        assert payload["capabilities"]["c"]["state"] == "UNKNOWN"


class TestAggregate:
    def test_names_both_what_was_and_was_not_checked(self):
        overall = aggregate(
            [
                CheckResult(name="github", state=HealthState.HEALTHY),
                CheckResult(name="vercel", state=HealthState.NOT_CONFIGURED),
            ]
        )
        assert "checked: github" in overall.summary
        assert "NOT checked: vercel" in overall.summary

    def test_one_unconfigured_integration_prevents_a_green_overall(self):
        overall = aggregate(
            [
                CheckResult(name="a", state=HealthState.HEALTHY),
                CheckResult(name="b", state=HealthState.HEALTHY),
                CheckResult(name="c", state=HealthState.NOT_CONFIGURED),
            ]
        )
        assert overall.state is not HealthState.HEALTHY

    def test_all_healthy_aggregates_to_healthy(self):
        overall = aggregate(
            [
                CheckResult(name="a", state=HealthState.HEALTHY),
                CheckResult(name="b", state=HealthState.HEALTHY),
            ]
        )
        assert overall.state is HealthState.HEALTHY

    @pytest.mark.parametrize("state", list(HealthState))
    def test_aggregate_never_upgrades_a_state(self, state):
        overall = aggregate([CheckResult(name="only", state=state)])
        if state is not HealthState.HEALTHY:
            assert overall.state is not HealthState.HEALTHY
