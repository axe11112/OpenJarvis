"""The registry: what exists, what is configured, and the difference."""

from __future__ import annotations

import pytest

from openjarvis.wiz.authority import Authority
from openjarvis.wiz.capabilities import (
    AUTONOMOUS_RISK,
    Availability,
    CapabilityRegistry,
    CapabilitySpec,
    Risk,
    UnknownCapability,
)


def _spec(name="thing.read", **kwargs) -> CapabilitySpec:
    defaults = dict(
        summary="a thing", authority=Authority.READ, risk=Risk.LOW
    )
    defaults.update(kwargs)
    return CapabilitySpec(name=name, **defaults)


class TestRegistry:
    def test_an_unknown_capability_raises_rather_than_returning_none(self):
        with pytest.raises(UnknownCapability):
            CapabilityRegistry().get("thing.invented")

    def test_a_capability_cannot_be_silently_redefined(self):
        registry = CapabilityRegistry([_spec()])
        with pytest.raises(ValueError):
            registry.register(_spec(authority=Authority.PRODUCTION_CHANGE))

    def test_the_original_authority_survives_a_redefinition_attempt(self):
        registry = CapabilityRegistry([_spec()])
        with pytest.raises(ValueError):
            registry.register(_spec(authority=Authority.PRODUCTION_CHANGE))
        assert registry.get("thing.read").authority is Authority.READ


class TestAvailabilityIsHonest:
    def test_a_capability_with_no_probe_is_available(self):
        assert _spec().availability().configured

    def test_a_missing_dependency_makes_it_unavailable_with_a_reason(self):
        spec = _spec(probe=lambda: Availability.missing("no claude CLI"))
        available = spec.availability()
        assert not available.configured
        assert "claude" in available.detail

    def test_a_probe_that_raises_means_unavailable_not_available(self):
        # Failing open here would have Wiz claim a capability precisely when it
        # cannot tell whether it has it.
        def broken():
            raise RuntimeError("cannot tell")

        assert not _spec(probe=broken).availability().configured

    def test_configured_excludes_the_unavailable(self):
        registry = CapabilityRegistry(
            [
                _spec("a.ready"),
                _spec("b.missing", probe=lambda: Availability.missing("nope")),
            ]
        )
        assert [s.name for s in registry.configured()] == ["a.ready"]
        assert len(registry) == 2

    def test_describe_reports_both_halves(self):
        registry = CapabilityRegistry(
            [
                _spec("a.ready"),
                _spec("b.missing", probe=lambda: Availability.missing("nope")),
            ]
        )
        described = {d["name"]: d for d in registry.describe()}
        assert described["a.ready"]["configured"] is True
        assert described["b.missing"]["configured"] is False
        assert described["b.missing"]["detail"] == "nope"


class TestRiskPolicy:
    def test_high_risk_is_never_autonomous(self):
        assert Risk.HIGH not in AUTONOMOUS_RISK

    def test_low_and_medium_are_autonomous(self):
        assert Risk.LOW in AUTONOMOUS_RISK
        assert Risk.MEDIUM in AUTONOMOUS_RISK
