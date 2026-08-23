"""Tests for Wiz production monitoring and verification."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openjarvis.wiz.production import HealthCheck, ProductionHealth, ProductionMonitor, ProductionVerifier


def test_health_check_creation():
    """Test creating a health check result."""
    check = HealthCheck(
        name="HTTP Status",
        passed=True,
        error=None,
        duration_ms=42.5,
    )

    assert check.name == "HTTP Status"
    assert check.passed is True
    assert check.error is None
    assert check.duration_ms == 42.5
    assert check.checked_at is not None


def test_health_check_default_timestamp():
    """Test that health check gets default timestamp."""
    check = HealthCheck(
        name="Test",
        passed=True,
    )
    assert check.checked_at is not None
    assert isinstance(check.checked_at, datetime)


def test_production_health_creation():
    """Test creating production health status."""
    checks = [
        HealthCheck("HTTP Status", True),
        HealthCheck("Response Time", True),
    ]

    health = ProductionHealth(
        healthy=True,
        checks=checks,
        timestamp=datetime.utcnow(),
        summary="All checks passed",
    )

    assert health.healthy is True
    assert health.passed_count == 2
    assert health.total_count == 2


def test_production_health_mixed_results():
    """Test production health with mixed pass/fail results."""
    checks = [
        HealthCheck("HTTP Status", True),
        HealthCheck("Response Time", False),
    ]

    health = ProductionHealth(
        healthy=False,
        checks=checks,
        timestamp=datetime.utcnow(),
        summary="Some checks failed",
    )

    assert health.healthy is False
    assert health.passed_count == 1
    assert health.total_count == 2


def test_production_monitor_creation():
    """Test creating a production monitor."""
    monitor = ProductionMonitor("https://example.com")
    assert monitor.production_url == "https://example.com"
    assert monitor._last_health is None


def test_production_monitor_last_health():
    """Test getting last health check result."""
    monitor = ProductionMonitor("https://example.com")
    assert monitor.get_last_health() is None
    assert monitor.is_production_healthy() is False

    # Set a health result
    health = ProductionHealth(
        healthy=True,
        checks=[HealthCheck("Test", True)],
        timestamp=datetime.utcnow(),
        summary="Healthy",
    )
    monitor._last_health = health

    assert monitor.get_last_health() == health
    assert monitor.is_production_healthy() is True


@pytest.mark.asyncio
async def test_production_monitor_http_health_pass():
    """Test HTTP health check when production responds."""
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"<html>...</html>"

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)

        monitor = ProductionMonitor("https://example.com")
        checks = await monitor._check_http_health()

        assert len(checks) == 2  # Status + Content checks
        assert all(c.passed for c in checks)


def test_production_verifier_creation():
    """Test creating a production verifier."""
    verifier = ProductionVerifier(
        production_url="https://example.com",
        deployment_sha="abc123def456",
    )
    assert verifier.production_url == "https://example.com"
    assert verifier.deployment_sha == "abc123def456"


@pytest.mark.asyncio
async def test_production_monitor_empty_response():
    """Test HTTP health check with empty response."""
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b""  # Empty content

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)

        monitor = ProductionMonitor("https://example.com")
        checks = await monitor._check_http_health()

        # Status check should pass, content check should fail
        assert any(c.name == "HTTP Status" and c.passed for c in checks)
        assert any(c.name == "Response Content" and not c.passed for c in checks)


def test_health_check_with_error():
    """Test health check with error message."""
    check = HealthCheck(
        name="Test",
        passed=False,
        error="Connection refused",
    )

    assert check.passed is False
    assert "Connection refused" in check.error


def test_production_health_summary():
    """Test production health summary generation."""
    checks = [
        HealthCheck("Check 1", True),
        HealthCheck("Check 2", True),
        HealthCheck("Check 3", False),
    ]

    health = ProductionHealth(
        healthy=False,
        checks=checks,
        timestamp=datetime.utcnow(),
        summary="2/3 checks passed",
    )

    assert "2" in health.summary
    assert "3" in health.summary
