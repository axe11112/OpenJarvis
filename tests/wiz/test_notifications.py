"""Tests for Wiz notifications."""

from __future__ import annotations

from openjarvis.wiz.notifications import (
    NotificationChannel,
    NotificationManager,
    NotificationSeverity,
    OwnerNotification,
)


def test_notification_creation():
    """Test creating owner notification."""
    notification = OwnerNotification(
        feature_id="WIZE-123",
        message="Test notification",
        severity=NotificationSeverity.SUCCESS,
    )

    assert notification.feature_id == "WIZE-123"
    assert notification.severity == NotificationSeverity.SUCCESS
    assert notification.created_at is not None


def test_notification_severity_levels():
    """Test notification severity levels."""
    severities = {s.value for s in NotificationSeverity}
    expected = {"info", "success", "warning", "error"}
    assert expected.issubset(severities)


def test_notification_channels():
    """Test notification channels."""
    channels = {c.value for c in NotificationChannel}
    expected = {"telegram", "email", "slack", "log"}
    assert expected.issubset(channels)


def test_notification_manager():
    """Test notification manager."""
    manager = NotificationManager()
    assert len(manager.sent_notifications) == 0
