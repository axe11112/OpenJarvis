"""Type definitions for the JARVIS reliability subsystem.

Pure data — no I/O, no network, no model calls.  Every type is a dataclass with
``to_dict``/``from_dict`` round-tripping, following the ``ScheduledTask``
convention in :mod:`openjarvis.scheduler.scheduler`.

See ``docs/JARVIS_ARCHITECTURE.md`` for how these fit together.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional

__all__ = [
    "Correlation",
    "Evidence",
    "EvidenceKind",
    "Incident",
    "IncidentState",
    "IncidentTransition",
    "InvalidTransitionError",
    "LEGAL_TRANSITIONS",
    "ProbeResult",
    "RepairAttempt",
    "RecoveryType",
    "Resolution",
    "Severity",
    "Signal",
    "TERMINAL_STATES",
    "TrustLevel",
    "VerificationResult",
    "now_iso",
]


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    """Return a short random identifier for sub-records."""
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    """Incident severity.

    Ordering matters: :meth:`at_least` is used by policy gates to express
    thresholds such as "notify immediately at HIGH or above".
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def rank(self) -> int:
        """Numeric rank — higher means more severe."""
        return _SEVERITY_RANK[self]

    def at_least(self, other: "Severity") -> bool:
        """Return ``True`` when this severity is at least as severe as *other*."""
        return self.rank >= other.rank

    @classmethod
    def parse(cls, value: Any) -> "Severity":
        """Coerce a string (any case) or ``Severity`` into a ``Severity``."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().upper())
        except ValueError as exc:
            raise ValueError(
                f"Unknown severity {value!r}; "
                f"valid values: {', '.join(s.value for s in cls)}"
            ) from exc


_SEVERITY_RANK: Dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


# ---------------------------------------------------------------------------
# Incident state machine
# ---------------------------------------------------------------------------


class IncidentState(str, Enum):
    """Lifecycle state of an incident."""

    DETECTED = "DETECTED"
    INVESTIGATING = "INVESTIGATING"
    REPRODUCING = "REPRODUCING"
    FIXING = "FIXING"
    TESTING = "TESTING"
    VERIFYING = "VERIFYING"
    #: The fix is on the default branch and production has not yet proved it.
    #:
    #: This window exists whether or not it is named, and it is the most
    #: dangerous one in the whole lifecycle: the change is live-bound, and the
    #: only evidence so far came from a preview deployment of a different
    #: commit. Naming it means an operator can see an incident sitting here, a
    #: crash cannot leave one silently counted as fixed, and the transition
    #: graph can enforce that nothing reaches ``RESOLVED`` by merging alone.
    #:
    #: Only reached when automatic merge is enabled *and* the merge succeeded.
    #: The pull-request-only flow goes ``VERIFYING -> RESOLVED`` as it always
    #: has.
    MERGED = "MERGED"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    ROLLED_BACK = "ROLLED_BACK"
    #: JARVIS was interrupted mid-repair — a crash, a restart, a kill.  The
    #: incident is parked here rather than resumed: a process that died during
    #: FIXING may have left a worktree, a branch, or a half-applied change, and
    #: starting a second coding agent on top of that is how one outage becomes
    #: two.  Only a human moves an incident out of this state.
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

    @classmethod
    def parse(cls, value: Any) -> "IncidentState":
        """Coerce a string (any case) or ``IncidentState`` into a state."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().upper())
        except ValueError as exc:
            raise ValueError(
                f"Unknown incident state {value!r}; "
                f"valid values: {', '.join(s.value for s in cls)}"
            ) from exc


#: States from which JARVIS makes no further automatic progress on its own.
#: They are not immutable — a human (or a regression watcher) can move an
#: incident out of them — but the autonomous loop stops here.
TERMINAL_STATES: FrozenSet[IncidentState] = frozenset(
    {
        IncidentState.RESOLVED,
        IncidentState.FAILED,
        IncidentState.HUMAN_REQUIRED,
        IncidentState.ROLLED_BACK,
        IncidentState.RECOVERY_REQUIRED,
    }
)


#: Legal state transitions.  Anything not listed here raises
#: :class:`InvalidTransitionError`, so an incident can never reach ``RESOLVED``
#: without passing through verification.
LEGAL_TRANSITIONS: Dict[IncidentState, FrozenSet[IncidentState]] = {
    IncidentState.DETECTED: frozenset(
        {
            IncidentState.INVESTIGATING,
            IncidentState.RESOLVED,  # failure stopped reproducing on its own
            IncidentState.HUMAN_REQUIRED,
            IncidentState.FAILED,
        }
    ),
    IncidentState.INVESTIGATING: frozenset(
        {
            IncidentState.REPRODUCING,
            IncidentState.RESOLVED,
            IncidentState.HUMAN_REQUIRED,
            IncidentState.FAILED,
        }
    ),
    IncidentState.REPRODUCING: frozenset(
        {
            IncidentState.FIXING,
            IncidentState.RESOLVED,  # could not reproduce — transient
            IncidentState.HUMAN_REQUIRED,
            IncidentState.FAILED,
        }
    ),
    IncidentState.FIXING: frozenset(
        {
            IncidentState.TESTING,
            IncidentState.HUMAN_REQUIRED,
            IncidentState.RECOVERY_REQUIRED,  # JARVIS was interrupted mid-repair
            IncidentState.FAILED,
        }
    ),
    IncidentState.TESTING: frozenset(
        {
            IncidentState.VERIFYING,
            IncidentState.FIXING,  # tests failed — next repair attempt
            IncidentState.HUMAN_REQUIRED,
            IncidentState.RECOVERY_REQUIRED,
            IncidentState.FAILED,
        }
    ),
    IncidentState.VERIFYING: frozenset(
        {
            IncidentState.RESOLVED,  # preview verified, pull request delivered
            IncidentState.MERGED,  # ...or the merge gates let it onto main
            IncidentState.FIXING,  # verification failed — next repair attempt
            IncidentState.HUMAN_REQUIRED,
            IncidentState.RECOVERY_REQUIRED,
            IncidentState.FAILED,
        }
    ),
    #: Deliberately NOT back to FIXING. Once the change is on the default
    #: branch, "try again" is no longer a repair — it is a second unreviewed
    #: change stacked on a live one that is already suspect. Production either
    #: proves the merge or a human takes it.
    IncidentState.MERGED: frozenset(
        {
            IncidentState.RESOLVED,  # production verified the merged fix
            IncidentState.HUMAN_REQUIRED,
            IncidentState.RECOVERY_REQUIRED,  # interrupted mid-verification
            IncidentState.FAILED,
        }
    ),
    IncidentState.RESOLVED: frozenset({IncidentState.ROLLED_BACK}),
    IncidentState.FAILED: frozenset(
        {IncidentState.HUMAN_REQUIRED, IncidentState.INVESTIGATING}
    ),
    IncidentState.HUMAN_REQUIRED: frozenset(
        {
            IncidentState.INVESTIGATING,  # human hands it back
            IncidentState.RESOLVED,  # human fixed it
            IncidentState.FAILED,
        }
    ),
    IncidentState.ROLLED_BACK: frozenset(
        {IncidentState.HUMAN_REQUIRED, IncidentState.RESOLVED}
    ),
    #: Deliberately NOT back to FIXING.  Resuming an interrupted repair is a
    #: decision a human makes explicitly; the route out is INVESTIGATING, which
    #: starts the pipeline again from the beginning with fresh evidence.
    IncidentState.RECOVERY_REQUIRED: frozenset(
        {
            IncidentState.INVESTIGATING,
            IncidentState.HUMAN_REQUIRED,
            IncidentState.RESOLVED,
            IncidentState.FAILED,
        }
    ),
}


class InvalidTransitionError(ValueError):
    """Raised when an incident is moved between states illegally."""


def path_between(
    start: "IncidentState", target: "IncidentState"
) -> List["IncidentState"]:
    """Return the shortest legal sequence of states from *start* to *target*.

    The list excludes *start* and includes *target*; an empty list means they
    are the same state.

    Callers that need to reach a state several steps away (the repair loop moves
    a freshly-DETECTED incident to FIXING) use this to walk the machine properly
    instead of jumping, which keeps the transition log a truthful account of
    what happened.

    Raises
    ------
    InvalidTransitionError
        When no legal path exists.
    """
    if start is target:
        return []
    # Breadth-first search: the state graph is tiny, so this is instant and
    # always yields the shortest legal route.
    queue: List[tuple[IncidentState, List[IncidentState]]] = [(start, [])]
    seen = {start}
    while queue:
        current, route = queue.pop(0)
        for nxt in sorted(LEGAL_TRANSITIONS.get(current, frozenset()), key=str):
            if nxt in seen:
                continue
            extended = [*route, nxt]
            if nxt is target:
                return extended
            seen.add(nxt)
            queue.append((nxt, extended))
    raise InvalidTransitionError(f"no legal path from {start.value} to {target.value}")


#: The single automatic predecessor of ``RESOLVED``.  Guarded by a test: no
#: autonomous path may reach ``RESOLVED`` except through ``VERIFYING``.
AUTOMATIC_RESOLVE_PREDECESSOR = IncidentState.VERIFYING


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class TrustLevel(str, Enum):
    """Provenance of a piece of evidence.

    ``EXTERNAL`` content is attacker-influenceable (page text, logs, database
    rows, PR bodies) and must be fenced before it reaches any model prompt.
    """

    TRUSTED = "trusted"  # produced by JARVIS itself
    EXTERNAL = "external"  # captured from an outside system — untrusted


class EvidenceKind(str, Enum):
    """What a piece of evidence is."""

    CONSOLE_ERROR = "console_error"
    NETWORK_FAILURE = "network_failure"
    HTTP_ERROR = "http_error"
    UNEXPECTED_REDIRECT = "unexpected_redirect"
    SCREENSHOT = "screenshot"
    TRACE = "trace"
    LOG = "log"
    BUILD_LOG = "build_log"
    TEST_OUTPUT = "test_output"
    DIFF = "diff"
    DEPLOYMENT = "deployment"
    COMMIT = "commit"
    PROBE_STEP = "probe_step"
    INJECTION_FINDING = "injection_finding"
    NOTE = "note"


@dataclass(slots=True)
class Evidence:
    """A single observation attached to an incident.

    There is deliberately **no field capable of holding a credential**.  Probe
    credentials are resolved inside the probe runner and never leave it; see
    ``docs/JARVIS_SECURITY.md`` §3.2 (layer 1, structural exclusion).

    ``content`` holds captured text and ``artifact_path`` points at a file on
    disk (screenshot, trace, HAR).  Content sourced from outside JARVIS must be
    recorded with ``trust=TrustLevel.EXTERNAL``.
    """

    kind: EvidenceKind
    summary: str = ""
    content: str = ""
    artifact_path: str = ""
    source: str = ""
    trust: TrustLevel = TrustLevel.EXTERNAL
    id: str = field(default_factory=_new_id)
    created_at: str = field(default_factory=now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_external(self) -> bool:
        """``True`` when this evidence must be treated as untrusted input."""
        return self.trust is TrustLevel.EXTERNAL

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "id": self.id,
            "kind": self.kind.value,
            "summary": self.summary,
            "content": self.content,
            "artifact_path": self.artifact_path,
            "source": self.source,
            "trust": self.trust.value,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Evidence":
        """Deserialize from a plain dict."""
        return cls(
            kind=EvidenceKind(d["kind"]),
            summary=d.get("summary", ""),
            content=d.get("content", ""),
            artifact_path=d.get("artifact_path", ""),
            source=d.get("source", ""),
            trust=TrustLevel(d.get("trust", TrustLevel.EXTERNAL.value)),
            id=d.get("id") or _new_id(),
            created_at=d.get("created_at") or now_iso(),
            metadata=dict(d.get("metadata") or {}),
        )


# ---------------------------------------------------------------------------
# Signals and probe results
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Signal:
    """A raw observation from an infrastructure source (Vercel, Supabase, GitHub).

    A signal is not yet an incident — it is an input to detection.
    """

    source: str  # "vercel" | "supabase" | "github" | ...
    kind: str  # source-specific, e.g. "deployment_failed"
    title: str = ""
    detail: str = ""
    severity: Severity = Severity.LOW
    component: str = ""
    external_id: str = ""  # deployment id, run id, ...
    occurred_at: str = field(default_factory=now_iso)
    trust: TrustLevel = TrustLevel.EXTERNAL
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "source": self.source,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "severity": self.severity.value,
            "component": self.component,
            "external_id": self.external_id,
            "occurred_at": self.occurred_at,
            "trust": self.trust.value,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Signal":
        """Deserialize from a plain dict."""
        return cls(
            source=d["source"],
            kind=d["kind"],
            title=d.get("title", ""),
            detail=d.get("detail", ""),
            severity=Severity.parse(d.get("severity", Severity.LOW)),
            component=d.get("component", ""),
            external_id=d.get("external_id", ""),
            occurred_at=d.get("occurred_at") or now_iso(),
            trust=TrustLevel(d.get("trust", TrustLevel.EXTERNAL.value)),
            metadata=dict(d.get("metadata") or {}),
        )


@dataclass(slots=True)
class ProbeResult:
    """Outcome of running one probe spec once.

    Carries no credentials: the probe runner resolves ``value_from`` references
    internally and never copies the resolved value into a result.
    """

    probe_id: str
    success: bool
    failure_kind: str = ""  # "assertion" | "timeout" | "http_error" | ...
    error: str = ""
    duration_seconds: float = 0.0
    final_url: str = ""
    http_status: int = 0
    steps_completed: int = 0
    evidence: List[Evidence] = field(default_factory=list)
    started_at: str = field(default_factory=now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "probe_id": self.probe_id,
            "success": self.success,
            "failure_kind": self.failure_kind,
            "error": self.error,
            "duration_seconds": self.duration_seconds,
            "final_url": self.final_url,
            "http_status": self.http_status,
            "steps_completed": self.steps_completed,
            "evidence": [e.to_dict() for e in self.evidence],
            "started_at": self.started_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ProbeResult":
        """Deserialize from a plain dict."""
        return cls(
            probe_id=d["probe_id"],
            success=bool(d["success"]),
            failure_kind=d.get("failure_kind", ""),
            error=d.get("error", ""),
            duration_seconds=float(d.get("duration_seconds", 0.0)),
            final_url=d.get("final_url", ""),
            http_status=int(d.get("http_status", 0)),
            steps_completed=int(d.get("steps_completed", 0)),
            evidence=[Evidence.from_dict(e) for e in d.get("evidence") or []],
            started_at=d.get("started_at") or now_iso(),
            metadata=dict(d.get("metadata") or {}),
        )


# ---------------------------------------------------------------------------
# Repair records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class VerificationResult:
    """Independent verification of a repair.

    Produced by re-running the probe spec that opened the incident against a
    preview deployment.  ``passed`` is decided by comparing declared
    expectations against observed behaviour — never by asking a model.
    """

    passed: bool
    probe_id: str = ""
    target_url: str = ""
    expected: str = ""
    actual: str = ""
    notes: str = ""
    checked_at: str = field(default_factory=now_iso)
    evidence: List[Evidence] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "passed": self.passed,
            "probe_id": self.probe_id,
            "target_url": self.target_url,
            "expected": self.expected,
            "actual": self.actual,
            "notes": self.notes,
            "checked_at": self.checked_at,
            "evidence": [e.to_dict() for e in self.evidence],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "VerificationResult":
        """Deserialize from a plain dict."""
        return cls(
            passed=bool(d["passed"]),
            probe_id=d.get("probe_id", ""),
            target_url=d.get("target_url", ""),
            expected=d.get("expected", ""),
            actual=d.get("actual", ""),
            notes=d.get("notes", ""),
            checked_at=d.get("checked_at") or now_iso(),
            evidence=[Evidence.from_dict(e) for e in d.get("evidence") or []],
        )


@dataclass(slots=True)
class RepairAttempt:
    """One pass through the repair loop.

    ``claim`` records what the coding agent *said* it did.  It is stored as an
    assertion and carries no authority: only ``verification`` decides whether a
    repair worked.
    """

    number: int
    branch: str = ""
    briefing_hash: str = ""  # hash of the task text, not the text itself
    changed_files: List[str] = field(default_factory=list)
    diff_stat: str = ""
    tests_passed: Optional[bool] = None
    test_summary: str = ""
    claim: str = ""
    verification: Optional[VerificationResult] = None
    outcome: str = ""  # "verified" | "verification_failed" | "no_diff" | ...
    started_at: str = field(default_factory=now_iso)
    finished_at: str = ""
    #: The commit the isolated worktree was cut from. Recorded before the agent
    #: runs so the audit log can say exactly what the repair was based on,
    #: rather than inferring it afterwards from a branch that has since moved.
    base_commit: str = ""
    #: The commit the agent's work was recorded as, inside the worktree.
    commit_sha: str = ""
    #: Filesystem path of the isolated worktree, for post-mortem inspection.
    worktree_path: str = ""
    #: Preview deployment this attempt was verified against.
    preview_url: str = ""
    #: Serialized :class:`~openjarvis.reliability.checks.CheckSuiteResult`.
    checks: Dict[str, Any] = field(default_factory=dict)
    #: Serialized :class:`~openjarvis.reliability.scope.ScopeVerdict`.
    scope: Dict[str, Any] = field(default_factory=dict)
    #: Insertions plus deletions against the base commit, for the scope guard.
    lines_changed_total: int = 0
    #: Which hypothesis this attempt was working from — a key from
    #: :data:`openjarvis.reliability.playbook.STRATEGIES`. Recorded so the next
    #: attempt can try a *different* idea rather than the same one louder, and
    #: so a handover can say which ideas have already been eliminated.
    strategy: str = ""
    #: Test files the agent added or modified — the regression-test question.
    regression_tests: List[str] = field(default_factory=list)

    @property
    def produced_changes(self) -> bool:
        """``True`` when the agent actually modified files."""
        return bool(self.changed_files)

    @property
    def verified(self) -> bool:
        """``True`` only when independent verification passed."""
        return self.verification is not None and self.verification.passed

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "number": self.number,
            "branch": self.branch,
            "briefing_hash": self.briefing_hash,
            "changed_files": list(self.changed_files),
            "diff_stat": self.diff_stat,
            "tests_passed": self.tests_passed,
            "test_summary": self.test_summary,
            "claim": self.claim,
            "verification": (
                self.verification.to_dict() if self.verification is not None else None
            ),
            "outcome": self.outcome,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "base_commit": self.base_commit,
            "commit_sha": self.commit_sha,
            "worktree_path": self.worktree_path,
            "preview_url": self.preview_url,
            "checks": dict(self.checks),
            "scope": dict(self.scope),
            "lines_changed_total": self.lines_changed_total,
            "regression_tests": list(self.regression_tests),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RepairAttempt":
        """Deserialize from a plain dict."""
        raw_verification = d.get("verification")
        return cls(
            number=int(d["number"]),
            branch=d.get("branch", ""),
            briefing_hash=d.get("briefing_hash", ""),
            changed_files=list(d.get("changed_files") or []),
            diff_stat=d.get("diff_stat", ""),
            tests_passed=d.get("tests_passed"),
            test_summary=d.get("test_summary", ""),
            claim=d.get("claim", ""),
            verification=(
                VerificationResult.from_dict(raw_verification)
                if raw_verification
                else None
            ),
            outcome=d.get("outcome", ""),
            started_at=d.get("started_at") or now_iso(),
            finished_at=d.get("finished_at", ""),
            base_commit=d.get("base_commit", ""),
            commit_sha=d.get("commit_sha", ""),
            worktree_path=d.get("worktree_path", ""),
            preview_url=d.get("preview_url", ""),
            checks=dict(d.get("checks") or {}),
            scope=dict(d.get("scope") or {}),
            lines_changed_total=int(d.get("lines_changed_total", 0) or 0),
            regression_tests=list(d.get("regression_tests") or []),
        )


# ---------------------------------------------------------------------------
# Correlation and resolution
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Correlation:
    """Likely cause of an incident, as a ranked guess rather than a claim."""

    deployment_id: str = ""
    commit_sha: str = ""
    pr_number: int = 0
    branch: str = ""
    changed_files: List[str] = field(default_factory=list)
    confidence: float = 0.0  # 0.0-1.0
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "deployment_id": self.deployment_id,
            "commit_sha": self.commit_sha,
            "pr_number": self.pr_number,
            "branch": self.branch,
            "changed_files": list(self.changed_files),
            "confidence": self.confidence,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Correlation":
        """Deserialize from a plain dict."""
        return cls(
            deployment_id=d.get("deployment_id", ""),
            commit_sha=d.get("commit_sha", ""),
            pr_number=int(d.get("pr_number", 0) or 0),
            branch=d.get("branch", ""),
            changed_files=list(d.get("changed_files") or []),
            confidence=float(d.get("confidence", 0.0) or 0.0),
            notes=d.get("notes", ""),
        )


class RecoveryType(str, Enum):
    """How an incident stopped being a problem.

    The distinction is not cosmetic.  "JARVIS proposed a fix that a human
    merged" and "the failure went away while we were looking at it" are
    different facts about the system, and conflating them would let JARVIS take
    credit for recoveries it had nothing to do with — which in turn would make
    its own effectiveness impossible to measure.
    """

    #: Independent verification passed after a JARVIS repair.
    VERIFIED_REPAIR = "VERIFIED_REPAIR"
    #: The probe passed again without JARVIS changing anything: someone else
    #: deployed a fix, or the cause was transient.
    RECOVERED_EXTERNALLY = "RECOVERED_EXTERNALLY"
    #: A human closed it.
    HUMAN_RESOLVED = "HUMAN_RESOLVED"
    #: Not resolved, or resolved before this field existed.
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class Resolution:
    """How an incident ended."""

    root_cause: str = ""
    fix_summary: str = ""
    pr_url: str = ""
    deployed_at: str = ""
    attempts_used: int = 0
    recovery_type: RecoveryType = RecoveryType.UNKNOWN
    resolved_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "root_cause": self.root_cause,
            "fix_summary": self.fix_summary,
            "pr_url": self.pr_url,
            "deployed_at": self.deployed_at,
            "attempts_used": self.attempts_used,
            "recovery_type": self.recovery_type.value,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Resolution":
        """Deserialize from a plain dict."""
        raw = d.get("recovery_type") or RecoveryType.UNKNOWN.value
        try:
            recovery = RecoveryType(str(raw).upper())
        except ValueError:
            recovery = RecoveryType.UNKNOWN
        return cls(
            root_cause=d.get("root_cause", ""),
            fix_summary=d.get("fix_summary", ""),
            pr_url=d.get("pr_url", ""),
            deployed_at=d.get("deployed_at", ""),
            attempts_used=int(d.get("attempts_used", 0) or 0),
            recovery_type=recovery,
            resolved_at=d.get("resolved_at", ""),
        )


@dataclass(slots=True)
class IncidentTransition:
    """One entry in an incident's append-only state history."""

    from_state: IncidentState
    to_state: IncidentState
    actor: str = "jarvis"
    reason: str = ""
    at: str = field(default_factory=now_iso)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "actor": self.actor,
            "reason": self.reason,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IncidentTransition":
        """Deserialize from a plain dict."""
        return cls(
            from_state=IncidentState.parse(d["from_state"]),
            to_state=IncidentState.parse(d["to_state"]),
            actor=d.get("actor", "jarvis"),
            reason=d.get("reason", ""),
            at=d.get("at") or now_iso(),
        )


# ---------------------------------------------------------------------------
# Incident
# ---------------------------------------------------------------------------


@dataclass
class Incident:
    """A detected production problem and everything JARVIS knows about it."""

    fingerprint: str
    severity: Severity
    component: str
    title: str
    id: str = ""  # assigned by IncidentStore: "INC-00042"
    summary: str = ""
    environment: str = "production"
    source: str = "probe"  # "probe" | "vercel" | "supabase" | "github"
    probe_id: str = ""
    state: IncidentState = IncidentState.DETECTED
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    occurrences: int = 1
    last_seen_at: str = field(default_factory=now_iso)
    repro_steps: List[str] = field(default_factory=list)
    evidence: List[Evidence] = field(default_factory=list)
    attempts: List[RepairAttempt] = field(default_factory=list)
    transitions: List[IncidentTransition] = field(default_factory=list)
    correlation: Correlation = field(default_factory=Correlation)
    resolution: Resolution = field(default_factory=Resolution)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # -- state machine ----------------------------------------------------

    def can_transition_to(self, state: IncidentState) -> bool:
        """Return ``True`` when moving to *state* is legal from the current one."""
        return state in LEGAL_TRANSITIONS.get(self.state, frozenset())

    def transition_to(
        self,
        state: IncidentState,
        *,
        actor: str = "jarvis",
        reason: str = "",
    ) -> IncidentTransition:
        """Move the incident to *state*, appending to its history.

        Raises :class:`InvalidTransitionError` when the move is not legal.  The
        transition log is append-only: entries are never rewritten.
        """
        state = IncidentState.parse(state)
        if not self.can_transition_to(state):
            allowed = sorted(
                s.value for s in LEGAL_TRANSITIONS.get(self.state, frozenset())
            )
            raise InvalidTransitionError(
                f"Cannot move incident {self.id or '<unsaved>'} from "
                f"{self.state.value} to {state.value}; "
                f"legal next states: {', '.join(allowed) or 'none'}"
            )
        transition = IncidentTransition(
            from_state=self.state,
            to_state=state,
            actor=actor,
            reason=reason,
        )
        self.state = state
        self.transitions.append(transition)
        self.updated_at = transition.at
        return transition

    @property
    def is_terminal(self) -> bool:
        """``True`` when the autonomous loop makes no further progress."""
        return self.state in TERMINAL_STATES

    @property
    def is_open(self) -> bool:
        """``True`` while the incident still represents an unresolved problem."""
        return self.state not in (IncidentState.RESOLVED,)

    # -- content ----------------------------------------------------------

    def add_evidence(self, evidence: Evidence) -> Evidence:
        """Attach a piece of evidence and bump ``updated_at``."""
        self.evidence.append(evidence)
        self.updated_at = now_iso()
        return evidence

    def add_attempt(self, attempt: RepairAttempt) -> RepairAttempt:
        """Record a repair attempt and bump ``updated_at``."""
        self.attempts.append(attempt)
        self.updated_at = now_iso()
        return attempt

    def record_occurrence(self, at: Optional[str] = None) -> int:
        """Note that the same failure was observed again.

        Repeat observations increment a counter rather than opening a duplicate
        incident.  Returns the new occurrence count.
        """
        self.occurrences += 1
        self.last_seen_at = at or now_iso()
        self.updated_at = self.last_seen_at
        return self.occurrences

    @property
    def attempts_used(self) -> int:
        """Number of repair attempts made so far."""
        return len(self.attempts)

    @property
    def external_evidence(self) -> List[Evidence]:
        """Evidence that must be fenced before reaching a model prompt."""
        return [e for e in self.evidence if e.is_external]

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "id": self.id,
            "fingerprint": self.fingerprint,
            "severity": self.severity.value,
            "component": self.component,
            "title": self.title,
            "summary": self.summary,
            "environment": self.environment,
            "source": self.source,
            "probe_id": self.probe_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "occurrences": self.occurrences,
            "last_seen_at": self.last_seen_at,
            "repro_steps": list(self.repro_steps),
            "evidence": [e.to_dict() for e in self.evidence],
            "attempts": [a.to_dict() for a in self.attempts],
            "transitions": [t.to_dict() for t in self.transitions],
            "correlation": self.correlation.to_dict(),
            "resolution": self.resolution.to_dict(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Incident":
        """Deserialize from a plain dict."""
        return cls(
            fingerprint=d["fingerprint"],
            severity=Severity.parse(d["severity"]),
            component=d.get("component", ""),
            title=d.get("title", ""),
            id=d.get("id", ""),
            summary=d.get("summary", ""),
            environment=d.get("environment", "production"),
            source=d.get("source", "probe"),
            probe_id=d.get("probe_id", ""),
            state=IncidentState.parse(d.get("state", IncidentState.DETECTED)),
            created_at=d.get("created_at") or now_iso(),
            updated_at=d.get("updated_at") or now_iso(),
            occurrences=int(d.get("occurrences", 1) or 1),
            last_seen_at=d.get("last_seen_at") or now_iso(),
            repro_steps=list(d.get("repro_steps") or []),
            evidence=[Evidence.from_dict(e) for e in d.get("evidence") or []],
            attempts=[RepairAttempt.from_dict(a) for a in d.get("attempts") or []],
            transitions=[
                IncidentTransition.from_dict(t) for t in d.get("transitions") or []
            ],
            correlation=Correlation.from_dict(d.get("correlation") or {}),
            resolution=Resolution.from_dict(d.get("resolution") or {}),
            metadata=dict(d.get("metadata") or {}),
        )
