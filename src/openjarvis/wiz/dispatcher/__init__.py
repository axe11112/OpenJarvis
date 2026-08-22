"""Request dispatcher: Parse owner requests and create FeatureRequests."""

from __future__ import annotations

from typing import Optional

from openjarvis.wiz.models import FeatureRequest, FeatureState, RiskLevel


class RequestDispatcher:
    """Parse natural language requests from owner and create FeatureRequests."""

    def dispatch(self, owner_input: str) -> FeatureRequest:
        """Transform owner input into a structured FeatureRequest.

        Args:
            owner_input: Natural language request from owner

        Returns:
            FeatureRequest ready for planning
        """
        request = FeatureRequest(
            owner_input=owner_input,
            description=owner_input,
            state=FeatureState.CREATED,
        )

        # Initial risk assessment from request text
        request.risk_level = self._estimate_risk(owner_input)

        return request

    def _estimate_risk(self, text: str) -> RiskLevel:
        """Preliminary risk classification from request text.

        This is only initial; final risk is determined from actual diff.
        """
        text_lower = text.lower()

        # HIGH risk keywords
        high_risk_keywords = [
            "auth",
            "payment",
            "billing",
            "database",
            "schema",
            "migrate",
            "delete",
            "permission",
            "rls",
            "security",
            "password",
            "credentials",
            "secret",
            "infrastructure",
            "deployment",
            "ci/cd",
        ]

        # MEDIUM risk keywords
        medium_risk_keywords = [
            "api",
            "endpoint",
            "data",
            "model",
            "workflow",
            "logic",
        ]

        for keyword in high_risk_keywords:
            if keyword in text_lower:
                return RiskLevel.HIGH

        for keyword in medium_risk_keywords:
            if keyword in text_lower:
                return RiskLevel.MEDIUM

        return RiskLevel.LOW


__all__ = ["RequestDispatcher"]
