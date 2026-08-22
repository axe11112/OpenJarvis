"""Wiz feature memory: Persistent storage of feature requests and state."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from openjarvis.wiz.models import FeatureRequest, FeatureState, RiskLevel

logger = logging.getLogger(__name__)


class EnumEncoder(json.JSONEncoder):
    """JSON encoder that handles Enum values."""

    def default(self, obj):
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


@dataclass
class FeatureMemoryEntry:
    """In-memory representation of a feature with audit trail."""

    feature_id: str
    feature: FeatureRequest
    created_at: datetime
    updated_at: datetime
    audit_trail: list[str] = None

    def __post_init__(self):
        """Initialize defaults."""
        if self.audit_trail is None:
            self.audit_trail = []

    def add_audit_entry(self, entry: str):
        """Add audit trail entry."""
        timestamp = datetime.utcnow().isoformat()
        self.audit_trail.append(f"[{timestamp}] {entry}")
        self.updated_at = datetime.utcnow()


class WizMemory:
    """Persistent memory store for Wiz features."""

    def __init__(self, memory_dir: Optional[Path] = None):
        """Initialize memory store.

        Args:
            memory_dir: Directory to store feature state (default: ~/.wiz/memory)
        """
        if memory_dir is None:
            memory_dir = Path.home() / ".wiz" / "memory"

        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.features: dict[str, FeatureMemoryEntry] = {}
        self._load_features()

    def _load_features(self):
        """Load all features from disk."""
        for file in self.memory_dir.glob("*.json"):
            try:
                with open(file) as f:
                    data = json.load(f)
                    feature_dict = data["feature"]
                    # Reconstruct FeatureRequest from dict
                    state_val = feature_dict.get("state", "created")
                    if isinstance(state_val, dict) and "value" in state_val:
                        state_val = state_val["value"]
                    risk_val = feature_dict.get("risk_level", "unknown")
                    if isinstance(risk_val, dict) and "value" in risk_val:
                        risk_val = risk_val["value"]

                    request = FeatureRequest(
                        id=feature_dict["id"],
                        description=feature_dict.get("description", ""),
                        owner_input=feature_dict.get("owner_input", ""),
                        state=FeatureState(state_val),
                        risk_level=RiskLevel(risk_val),
                    )
                    # Copy over other fields
                    for key, value in feature_dict.items():
                        if key in ("id", "description", "owner_input", "state", "risk_level"):
                            continue
                        if hasattr(request, key):
                            setattr(request, key, value)

                    entry = FeatureMemoryEntry(
                        feature_id=data["feature_id"],
                        feature=request,
                        created_at=datetime.fromisoformat(data["created_at"]),
                        updated_at=datetime.fromisoformat(data["updated_at"]),
                        audit_trail=data.get("audit_trail", []),
                    )
                    self.features[entry.feature_id] = entry
                    logger.info(f"Loaded feature {entry.feature_id}")
            except Exception as e:
                logger.error(f"Failed to load {file}: {e}")

    def save_feature(self, feature: FeatureRequest):
        """Save feature to persistent storage."""
        entry = self.features.get(feature.id)
        if entry is None:
            entry = FeatureMemoryEntry(
                feature_id=feature.id,
                feature=feature,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            self.features[feature.id] = entry

        entry.feature = feature
        entry.updated_at = datetime.utcnow()

        # Serialize to disk
        file_path = self.memory_dir / f"{feature.id}.json"
        data = {
            "feature_id": entry.feature_id,
            "feature": asdict(feature),
            "created_at": entry.created_at.isoformat(),
            "updated_at": entry.updated_at.isoformat(),
            "audit_trail": entry.audit_trail,
        }

        with open(file_path, "w") as f:
            json.dump(data, f, indent=2, cls=EnumEncoder)

        logger.info(f"Saved feature {feature.id} to {file_path}")

    def get_feature(self, feature_id: str) -> Optional[FeatureRequest]:
        """Get a feature by ID."""
        entry = self.features.get(feature_id)
        return entry.feature if entry else None

    def list_features(self, state: Optional[FeatureState] = None) -> list[FeatureRequest]:
        """List features, optionally filtered by state."""
        features = []
        for entry in self.features.values():
            if state is None or entry.feature.state == state:
                features.append(entry.feature)
        return sorted(features, key=lambda f: f.created_at, reverse=True)

    def add_audit(self, feature_id: str, entry: str):
        """Add audit trail entry for a feature."""
        feature_entry = self.features.get(feature_id)
        if feature_entry:
            feature_entry.add_audit_entry(entry)
            # Re-save with new audit entry
            self.save_feature(feature_entry.feature)


__all__ = ["WizMemory", "FeatureMemoryEntry"]
