"""Tests for reliability types and the incident state machine."""

from __future__ import annotations

import pytest

from openjarvis.reliability.types import (
    AUTOMATIC_RESOLVE_PREDECESSOR,
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    Correlation,
    Evidence,
    EvidenceKind,
    Incident,
    IncidentState,
    InvalidTransitionError,
    ProbeResult,
    RepairAttempt,
    Resolution,
    Severity,
    Signal,
    TrustLevel,
    VerificationResult,
)


def _incident(**overrides) -> Incident:
    defaults = dict(
        fingerprint="fp_test",
        severity=Severity.HIGH,
        component="authentication",
        title="Login does not reach the dashboard",
    )
    defaults.update(overrides)
    return Incident(**defaults)


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


class TestSeverity:
    def test_ordering(self):
        assert Severity.CRITICAL.rank > Severity.HIGH.rank
        assert Severity.HIGH.rank > Severity.MEDIUM.rank
        assert Severity.MEDIUM.rank > Severity.LOW.rank

    def test_at_least(self):
        assert Severity.CRITICAL.at_least(Severity.HIGH)
        assert Severity.HIGH.at_least(Severity.HIGH)
        assert not Severity.LOW.at_least(Severity.MEDIUM)

    @pytest.mark.parametrize("raw", ["critical", "CRITICAL", " Critical "])
    def test_parse_case_insensitive(self, raw):
        assert Severity.parse(raw) is Severity.CRITICAL

    def test_parse_passthrough(self):
        assert Severity.parse(Severity.LOW) is Severity.LOW

    def test_parse_rejects_unknown(self):
        with pytest.raises(ValueError, match="Unknown severity"):
            Severity.parse("catastrophic")


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class TestStateMachine:
    def test_every_state_has_a_transition_entry(self):
        assert set(LEGAL_TRANSITIONS) == set(IncidentState)

    def test_happy_path(self):
        incident = _incident()
        for state in (
            IncidentState.INVESTIGATING,
            IncidentState.REPRODUCING,
            IncidentState.FIXING,
            IncidentState.TESTING,
            IncidentState.VERIFYING,
            IncidentState.RESOLVED,
        ):
            incident.transition_to(state)
        assert incident.state is IncidentState.RESOLVED
        assert len(incident.transitions) == 6

    def test_illegal_transition_raises(self):
        incident = _incident()
        with pytest.raises(InvalidTransitionError, match="DETECTED to TESTING"):
            incident.transition_to(IncidentState.TESTING)

    def test_illegal_transition_leaves_state_untouched(self):
        incident = _incident()
        with pytest.raises(InvalidTransitionError):
            incident.transition_to(IncidentState.FIXING)
        assert incident.state is IncidentState.DETECTED
        assert incident.transitions == []

    def test_detected_may_resolve_without_a_code_change(self):
        """A failure that stops reproducing before any repair is legitimately
        resolved — this path never follows a code change, so it does not
        weaken the verification guarantee."""
        incident = _incident()
        incident.transition_to(IncidentState.RESOLVED, reason="no longer reproduces")
        assert incident.state is IncidentState.RESOLVED

    def test_resolved_is_only_reachable_from_verifying_or_human(self):
        """No autonomous path may reach RESOLVED except through VERIFYING.

        This is the structural guarantee behind "never trust the coding agent's
        claim that it fixed something".
        """
        autonomous_predecessors = {
            state
            for state, targets in LEGAL_TRANSITIONS.items()
            if IncidentState.RESOLVED in targets
        }
        # HUMAN_REQUIRED -> RESOLVED is a human action, not an autonomous one.
        # DETECTED/INVESTIGATING/REPRODUCING -> RESOLVED means "stopped
        # reproducing", which never follows a code change.
        assert AUTOMATIC_RESOLVE_PREDECESSOR in autonomous_predecessors
        assert IncidentState.FIXING not in autonomous_predecessors
        assert IncidentState.TESTING not in autonomous_predecessors

    def test_fixing_cannot_skip_testing(self):
        incident = _incident(state=IncidentState.FIXING)
        with pytest.raises(InvalidTransitionError):
            incident.transition_to(IncidentState.VERIFYING)

    def test_retry_loop_verifying_back_to_fixing(self):
        incident = _incident(state=IncidentState.VERIFYING)
        incident.transition_to(IncidentState.FIXING, reason="verification failed")
        assert incident.state is IncidentState.FIXING

    def test_resolved_can_roll_back(self):
        incident = _incident(state=IncidentState.RESOLVED)
        incident.transition_to(IncidentState.ROLLED_BACK, reason="regression")
        assert incident.state is IncidentState.ROLLED_BACK

    def test_verifying_may_go_to_merged(self):
        incident = _incident(state=IncidentState.VERIFYING)
        incident.transition_to(IncidentState.MERGED, reason="merged, production next")
        assert incident.state is IncidentState.MERGED

    def test_merged_may_resolve_or_escalate(self):
        for target in (IncidentState.RESOLVED, IncidentState.HUMAN_REQUIRED):
            incident = _incident(state=IncidentState.MERGED)
            incident.transition_to(target, reason="production had its say")
            assert incident.state is target

    def test_merged_can_never_go_back_to_fixing(self):
        """Once the change is on the default branch, "try again" would stack a
        second unreviewed change on a live one already under suspicion."""
        incident = _incident(state=IncidentState.MERGED)
        with pytest.raises(InvalidTransitionError):
            incident.transition_to(IncidentState.FIXING)

    def test_merged_is_not_terminal(self):
        """It is a state something must still happen to, not a resting place."""
        assert IncidentState.MERGED not in TERMINAL_STATES

    def test_terminal_states(self):
        assert IncidentState.HUMAN_REQUIRED in TERMINAL_STATES
        assert IncidentState.DETECTED not in TERMINAL_STATES
        assert _incident(state=IncidentState.FAILED).is_terminal

    def test_any_active_state_can_escalate_to_human(self):
        active = set(IncidentState) - {
            IncidentState.HUMAN_REQUIRED,
            IncidentState.RESOLVED,
        }
        for state in active:
            assert IncidentState.HUMAN_REQUIRED in LEGAL_TRANSITIONS[state], state

    def test_transition_records_actor_and_reason(self):
        incident = _incident()
        transition = incident.transition_to(
            IncidentState.INVESTIGATING, actor="owner", reason="manual triage"
        )
        assert transition.actor == "owner"
        assert transition.reason == "manual triage"
        assert transition.from_state is IncidentState.DETECTED

    def test_transition_accepts_string(self):
        incident = _incident()
        incident.transition_to("INVESTIGATING")
        assert incident.state is IncidentState.INVESTIGATING

    def test_can_transition_to(self):
        incident = _incident()
        assert incident.can_transition_to(IncidentState.INVESTIGATING)
        assert not incident.can_transition_to(IncidentState.FIXING)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class TestEvidence:
    def test_defaults_to_untrusted(self):
        """External content is the default because most evidence is external."""
        evidence = Evidence(kind=EvidenceKind.LOG, summary="build log")
        assert evidence.trust is TrustLevel.EXTERNAL
        assert evidence.is_external

    def test_trusted_evidence(self):
        evidence = Evidence(
            kind=EvidenceKind.SCREENSHOT,
            trust=TrustLevel.TRUSTED,
            artifact_path="/tmp/shot.png",
        )
        assert not evidence.is_external

    def test_has_no_credential_field(self):
        """Structural exclusion: Evidence cannot carry a credential.

        See docs/JARVIS_SECURITY.md §3.2 layer 1.
        """
        fields = set(Evidence.__dataclass_fields__)
        forbidden = {"password", "token", "secret", "credential", "api_key", "auth"}
        assert fields & forbidden == set()

    def test_round_trip(self):
        original = Evidence(
            kind=EvidenceKind.CONSOLE_ERROR,
            summary="TypeError",
            content="x is not a function",
            source="browser",
            metadata={"line": 12},
        )
        restored = Evidence.from_dict(original.to_dict())
        assert restored == original


# ---------------------------------------------------------------------------
# Repair records
# ---------------------------------------------------------------------------


class TestRepairAttempt:
    def test_unverified_without_verification(self):
        attempt = RepairAttempt(number=1, claim="I fixed the login redirect.")
        assert not attempt.verified

    def test_claim_alone_does_not_verify(self):
        """A confident claim plus passing tests is still not verification."""
        attempt = RepairAttempt(
            number=1,
            claim="Fixed and fully tested.",
            tests_passed=True,
            changed_files=["app/auth.ts"],
        )
        assert not attempt.verified

    def test_failed_verification_is_not_verified(self):
        attempt = RepairAttempt(
            number=1,
            verification=VerificationResult(passed=False, probe_id="auth-login"),
        )
        assert not attempt.verified

    def test_passed_verification(self):
        attempt = RepairAttempt(
            number=1,
            verification=VerificationResult(passed=True, probe_id="auth-login"),
        )
        assert attempt.verified

    def test_produced_changes(self):
        assert not RepairAttempt(number=1).produced_changes
        assert RepairAttempt(number=1, changed_files=["a.ts"]).produced_changes

    def test_round_trip_with_verification(self):
        original = RepairAttempt(
            number=2,
            branch="jarvis/incident-INC-00001",
            changed_files=["app/auth.ts"],
            tests_passed=True,
            verification=VerificationResult(
                passed=True, probe_id="auth-login", expected="/dashboard"
            ),
        )
        restored = RepairAttempt.from_dict(original.to_dict())
        assert restored == original

    def test_round_trip_without_verification(self):
        original = RepairAttempt(number=1, outcome="no_diff")
        restored = RepairAttempt.from_dict(original.to_dict())
        assert restored.verification is None
        assert restored == original


# ---------------------------------------------------------------------------
# Incident content
# ---------------------------------------------------------------------------


class TestIncident:
    def test_record_occurrence_increments(self):
        incident = _incident()
        assert incident.occurrences == 1
        assert incident.record_occurrence() == 2
        assert incident.record_occurrence() == 3

    def test_add_evidence_and_attempt(self):
        incident = _incident()
        incident.add_evidence(Evidence(kind=EvidenceKind.NOTE, summary="hi"))
        incident.add_attempt(RepairAttempt(number=1))
        assert len(incident.evidence) == 1
        assert incident.attempts_used == 1

    def test_external_evidence_filter(self):
        incident = _incident()
        incident.add_evidence(Evidence(kind=EvidenceKind.LOG, summary="external"))
        incident.add_evidence(
            Evidence(
                kind=EvidenceKind.SCREENSHOT,
                summary="ours",
                trust=TrustLevel.TRUSTED,
            )
        )
        external = incident.external_evidence
        assert len(external) == 1
        assert external[0].summary == "external"

    def test_is_open(self):
        assert _incident().is_open
        assert not _incident(state=IncidentState.RESOLVED).is_open

    def test_round_trip(self):
        incident = _incident(
            repro_steps=["Open /login", "Submit credentials"],
            correlation=Correlation(commit_sha="abc123", confidence=0.7),
            resolution=Resolution(root_cause="bad redirect", attempts_used=1),
            metadata={"probe_run": 4},
        )
        incident.transition_to(IncidentState.INVESTIGATING)
        incident.add_evidence(Evidence(kind=EvidenceKind.HTTP_ERROR, summary="500"))
        incident.add_attempt(RepairAttempt(number=1, branch="jarvis/x"))

        restored = Incident.from_dict(incident.to_dict())
        assert restored.to_dict() == incident.to_dict()
        assert restored.state is IncidentState.INVESTIGATING
        assert restored.correlation.commit_sha == "abc123"
        assert restored.evidence[0].kind is EvidenceKind.HTTP_ERROR


# ---------------------------------------------------------------------------
# Signals and probe results
# ---------------------------------------------------------------------------


class TestSignalAndProbeResult:
    def test_signal_round_trip(self):
        original = Signal(
            source="vercel",
            kind="deployment_failed",
            title="Build failed",
            severity=Severity.HIGH,
            external_id="dpl_123",
        )
        assert Signal.from_dict(original.to_dict()) == original

    def test_signal_defaults_to_external_trust(self):
        assert Signal(source="github", kind="pr").trust is TrustLevel.EXTERNAL

    def test_probe_result_round_trip(self):
        original = ProbeResult(
            probe_id="auth-login",
            success=False,
            failure_kind="assertion",
            error="expected /dashboard, got /login",
            final_url="https://example.com/login",
            http_status=200,
            steps_completed=3,
            evidence=[Evidence(kind=EvidenceKind.CONSOLE_ERROR, summary="TypeError")],
        )
        restored = ProbeResult.from_dict(original.to_dict())
        assert restored == original

    def test_probe_result_has_no_credential_field(self):
        fields = set(ProbeResult.__dataclass_fields__)
        forbidden = {"password", "token", "secret", "credential", "api_key"}
        assert fields & forbidden == set()
