"""Tests for Wiz memory (persistent feature storage)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from openjarvis.wiz.memory import WizMemory
from openjarvis.wiz.models import FeatureRequest, FeatureState, RiskLevel


def test_memory_save_and_load():
    """Test saving and loading features from memory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory = WizMemory(Path(tmpdir))

        # Create feature
        request = FeatureRequest(description="test feature")
        request.risk_level = RiskLevel.LOW

        # Save
        memory.save_feature(request)
        assert (Path(tmpdir) / f"{request.id}.json").exists()

        # Load
        loaded = memory.get_feature(request.id)
        assert loaded is not None
        assert loaded.id == request.id
        assert loaded.description == request.description


def test_memory_list_features():
    """Test listing features."""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory = WizMemory(Path(tmpdir))

        # Create multiple features
        requests = [
            FeatureRequest(description="feature 1"),
            FeatureRequest(description="feature 2"),
            FeatureRequest(description="feature 3"),
        ]

        for req in requests:
            memory.save_feature(req)

        # List all
        all_features = memory.list_features()
        assert len(all_features) == 3


def test_memory_audit_trail():
    """Test audit trail tracking."""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory = WizMemory(Path(tmpdir))

        request = FeatureRequest(description="test")
        memory.save_feature(request)

        # Add audit entries
        memory.add_audit(request.id, "Created feature request")
        memory.add_audit(request.id, "Dispatched to implementation")

        # Reload and check
        entry = memory.features.get(request.id)
        assert entry is not None
        assert len(entry.audit_trail) == 2
        assert "Created feature request" in entry.audit_trail[0]
        assert "Dispatched to implementation" in entry.audit_trail[1]


def test_memory_persistence():
    """Test that memory persists across instances."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        # First instance - save feature
        memory1 = WizMemory(tmppath)
        request1 = FeatureRequest(description="persistent feature")
        memory1.save_feature(request1)
        feature_id = request1.id

        # Second instance - load feature
        memory2 = WizMemory(tmppath)
        request2 = memory2.get_feature(feature_id)

        assert request2 is not None
        assert request2.description == "persistent feature"
        assert request2.id == feature_id


def test_memory_filter_by_state():
    """Test filtering features by state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory = WizMemory(Path(tmpdir))

        # Create features in different states
        req1 = FeatureRequest(description="creating")
        req1.state = FeatureState.CREATED
        memory.save_feature(req1)

        req2 = FeatureRequest(description="implementing")
        req2.state = FeatureState.IMPLEMENTING
        memory.save_feature(req2)

        # Filter by state
        creating = memory.list_features(FeatureState.CREATED)
        implementing = memory.list_features(FeatureState.IMPLEMENTING)

        assert len(creating) == 1
        assert len(implementing) == 1
