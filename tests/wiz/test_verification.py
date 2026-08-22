"""Tests for Wiz verification systems."""

from __future__ import annotations

from openjarvis.wiz.verification import DeploymentInfo, DeploymentState


def test_deployment_ready_state():
    """Test deployment ready state."""
    deployment = DeploymentInfo(
        deployment_id="dpl123",
        url="https://preview.example.com",
        state=DeploymentState.READY,
        sha="abc123",
    )

    assert deployment.is_ready
    assert deployment.state == DeploymentState.READY


def test_deployment_not_ready():
    """Test deployment not ready states."""
    for state in [
        DeploymentState.QUEUED,
        DeploymentState.BUILDING,
        DeploymentState.ERROR,
        DeploymentState.UNKNOWN,
    ]:
        deployment = DeploymentInfo(
            deployment_id="dpl123",
            url="https://preview.example.com",
            state=state,
        )
        assert not deployment.is_ready
