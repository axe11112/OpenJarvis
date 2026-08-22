"""Tests for request dispatcher."""

from __future__ import annotations

from openjarvis.wiz.dispatcher import RequestDispatcher
from openjarvis.wiz.models import RiskLevel


def test_dispatcher_creates_feature_request():
    """Test dispatcher creates FeatureRequest from owner input."""
    dispatcher = RequestDispatcher()
    request = dispatcher.dispatch("Add dark mode to dashboard")

    assert request.description
    assert request.owner_input
    assert request.id.startswith("WIZE-")


def test_risk_estimation_low():
    """Test LOW risk classification."""
    dispatcher = RequestDispatcher()
    request = dispatcher.dispatch("Add a tooltip to the progress widget")

    assert request.risk_level == RiskLevel.LOW


def test_risk_estimation_medium():
    """Test MEDIUM risk classification."""
    dispatcher = RequestDispatcher()
    request = dispatcher.dispatch("Update the dashboard API to return new fields")

    assert request.risk_level == RiskLevel.MEDIUM


def test_risk_estimation_high():
    """Test HIGH risk classification."""
    dispatcher = RequestDispatcher()
    request = dispatcher.dispatch("Migrate user database schema and update authentication")

    assert request.risk_level == RiskLevel.HIGH


def test_risk_estimation_auth_keyword():
    """Test AUTH keyword triggers HIGH risk."""
    dispatcher = RequestDispatcher()
    request = dispatcher.dispatch("Fix authentication bug")

    assert request.risk_level == RiskLevel.HIGH


def test_risk_estimation_payment_keyword():
    """Test PAYMENT keyword triggers HIGH risk."""
    dispatcher = RequestDispatcher()
    request = dispatcher.dispatch("Update billing and payment processing")

    assert request.risk_level == RiskLevel.HIGH


def test_risk_estimation_schema_keyword():
    """Test SCHEMA keyword triggers HIGH risk."""
    dispatcher = RequestDispatcher()
    request = dispatcher.dispatch("Change database schema for workouts table")

    assert request.risk_level == RiskLevel.HIGH
