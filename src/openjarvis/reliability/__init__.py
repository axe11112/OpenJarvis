"""JARVIS reliability subsystem — autonomous website reliability engineering.

Monitors a production web application, opens incidents with reproducible
evidence, hands precise context to a coding agent, and verifies any repair
independently before it goes anywhere near production.

See ``docs/JARVIS_ARCHITECTURE.md``, ``docs/JARVIS_ROADMAP.md`` and
``docs/JARVIS_SECURITY.md``.
"""

from __future__ import annotations

from openjarvis.reliability.fingerprint import fingerprint, normalize_error
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import (
    Correlation,
    Evidence,
    EvidenceKind,
    Incident,
    IncidentState,
    IncidentTransition,
    InvalidTransitionError,
    ProbeResult,
    RepairAttempt,
    Resolution,
    Severity,
    Signal,
    TrustLevel,
    VerificationResult,
)

__all__ = [
    "Correlation",
    "Evidence",
    "EvidenceKind",
    "Incident",
    "IncidentState",
    "IncidentStore",
    "IncidentTransition",
    "InvalidTransitionError",
    "ProbeResult",
    "RepairAttempt",
    "Resolution",
    "Severity",
    "Signal",
    "TrustLevel",
    "VerificationResult",
    "fingerprint",
    "normalize_error",
]
