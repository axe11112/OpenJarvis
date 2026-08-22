"""JARVIS reliability subsystem — autonomous website reliability engineering.

Monitors a production web application, opens incidents with reproducible
evidence, hands precise context to a coding agent, and verifies any repair
independently before it goes anywhere near production.

See ``docs/JARVIS_ARCHITECTURE.md``, ``docs/JARVIS_ROADMAP.md`` and
``docs/JARVIS_SECURITY.md``.
"""

from __future__ import annotations

from openjarvis.reliability.analysis import build_analysis_prompt
from openjarvis.reliability.briefing import (
    Briefing,
    BriefingRefusedError,
    build_briefing,
)
from openjarvis.reliability.checks import CheckSuite, CheckSuiteResult
from openjarvis.reliability.code_agent import CodeAgent, CodeAgentResult
from openjarvis.reliability.correlate import correlate
from openjarvis.reliability.detector import Detection, Detector
from openjarvis.reliability.diagnostic import DiagnosticReport, LiveDiagnostic
from openjarvis.reliability.escalation import EscalationPolicy, EscalationTracker
from openjarvis.reliability.fingerprint import fingerprint, normalize_error
from openjarvis.reliability.flapping import FlappingDetector, FlappingVerdict
from openjarvis.reliability.health import CheckResult, HealthState
from openjarvis.reliability.monitor import ReliabilityMonitor
from openjarvis.reliability.notify import NotificationRouter, Notifier
from openjarvis.reliability.notify_ledger import NotificationLedger
from openjarvis.reliability.outage import Outage, OutageRegistry, classify_family
from openjarvis.reliability.owner_ask import OwnerAsk, build_owner_ask
from openjarvis.reliability.owner_commands import OwnerCommands
from openjarvis.reliability.policy import Decision, SafetyPolicy
from openjarvis.reliability.repair import RepairLoop, RepairOutcome
from openjarvis.reliability.report import IncidentReport, build_report
from openjarvis.reliability.scope import ScopeLimits, ScopeVerdict, assess_scope
from openjarvis.reliability.severity import Classification, classify
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.target import (
    TargetConfig,
    credential_report,
    resolve_target,
)
from openjarvis.reliability.types import (
    Correlation,
    Evidence,
    EvidenceKind,
    Incident,
    IncidentState,
    IncidentTransition,
    InvalidTransitionError,
    ProbeResult,
    RecoveryType,
    RepairAttempt,
    Resolution,
    Severity,
    Signal,
    TrustLevel,
    VerificationResult,
)
from openjarvis.reliability.verify import Verifier
from openjarvis.reliability.watch import (
    RepairGate,
    UnsafeConfigurationError,
    WatchSupervisor,
    assert_safe_to_start,
    startup_banner,
)
from openjarvis.reliability.workspace import RepairWorkspace, WorkspaceError, Worktree

__all__ = [
    "startup_banner",
    "classify",
    "build_report",
    "assert_safe_to_start",
    "WatchSupervisor",
    "UnsafeConfigurationError",
    "RepairGate",
    "RecoveryType",
    "IncidentReport",
    "FlappingVerdict",
    "FlappingDetector",
    "EscalationTracker",
    "EscalationPolicy",
    "Classification",
    "assess_scope",
    "Worktree",
    "WorkspaceError",
    "ScopeVerdict",
    "ScopeLimits",
    "RepairWorkspace",
    "CheckSuiteResult",
    "CheckSuite",
    "Briefing",
    "BriefingRefusedError",
    "CodeAgent",
    "CodeAgentResult",
    "CheckResult",
    "Correlation",
    "Decision",
    "Detection",
    "DiagnosticReport",
    "Detector",
    "Evidence",
    "HealthState",
    "EvidenceKind",
    "Incident",
    "LiveDiagnostic",
    "IncidentState",
    "IncidentStore",
    "IncidentTransition",
    "InvalidTransitionError",
    "NotificationLedger",
    "NotificationRouter",
    "Outage",
    "OutageRegistry",
    "OwnerAsk",
    "OwnerCommands",
    "build_owner_ask",
    "classify_family",
    "Notifier",
    "ProbeResult",
    "RepairAttempt",
    "RepairLoop",
    "RepairOutcome",
    "ReliabilityMonitor",
    "Resolution",
    "SafetyPolicy",
    "Severity",
    "Signal",
    "TargetConfig",
    "TrustLevel",
    "VerificationResult",
    "Verifier",
    "build_analysis_prompt",
    "build_briefing",
    "credential_report",
    "correlate",
    "fingerprint",
    "normalize_error",
    "resolve_target",
]
