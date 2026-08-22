"""Owner notifications for Wiz features."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class NotificationSeverity(Enum):
    """Severity of notification."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class NotificationChannel(Enum):
    """Channel to send notification through."""

    TELEGRAM = "telegram"
    EMAIL = "email"
    SLACK = "slack"
    LOG = "log"


@dataclass
class OwnerNotification:
    """Notification to send to owner."""

    feature_id: str
    message: str
    severity: NotificationSeverity
    channel: NotificationChannel = NotificationChannel.TELEGRAM
    created_at: datetime = None

    def __post_init__(self):
        """Initialize defaults."""
        if self.created_at is None:
            self.created_at = datetime.utcnow()


class NotificationManager:
    """Manage owner notifications."""

    def __init__(self, telegram_token: Optional[str] = None):
        """Initialize notification manager.

        Args:
            telegram_token: Telegram bot token (optional)
        """
        self.telegram_token = telegram_token
        self.sent_notifications: list[OwnerNotification] = []

    async def send_feature_complete(
        self, feature_id: str, production_url: str
    ) -> bool:
        """Notify owner that feature is complete and live.

        Args:
            feature_id: Feature ID
            production_url: Production URL where feature is deployed

        Returns:
            True if notification sent successfully
        """
        notification = OwnerNotification(
            feature_id=feature_id,
            message=f"Sir, it's live.\n{feature_id} has been deployed to production and is working normally.\nProduction: {production_url}",
            severity=NotificationSeverity.SUCCESS,
        )
        return await self._send(notification)

    async def send_feature_requires_approval(
        self, feature_id: str, preview_url: str, reason: str
    ) -> bool:
        """Notify owner that feature requires approval.

        Args:
            feature_id: Feature ID
            preview_url: Vercel Preview URL
            reason: Reason it requires approval

        Returns:
            True if notification sent successfully
        """
        notification = OwnerNotification(
            feature_id=feature_id,
            message=f"Sir, I need your review.\n{feature_id}: {reason}\nPreview: {preview_url}",
            severity=NotificationSeverity.WARNING,
        )
        return await self._send(notification)

    async def send_feature_failed(
        self, feature_id: str, error: str
    ) -> bool:
        """Notify owner that feature implementation failed.

        Args:
            feature_id: Feature ID
            error: Error description

        Returns:
            True if notification sent successfully
        """
        notification = OwnerNotification(
            feature_id=feature_id,
            message=f"Sir, {feature_id} failed.\n{error}\nI've stopped making changes.",
            severity=NotificationSeverity.ERROR,
        )
        return await self._send(notification)

    async def _send(self, notification: OwnerNotification) -> bool:
        """Send notification to owner.

        Args:
            notification: Notification to send

        Returns:
            True if sent successfully
        """
        self.sent_notifications.append(notification)
        logger.info(f"Would send notification: {notification.message}")
        # TODO: Implement actual Telegram/Email/Slack sending
        return True


__all__ = ["NotificationManager", "OwnerNotification", "NotificationSeverity"]
