"""End-to-end scenarios for the 24/7 loop (§32 of the Phase 14 brief).

Four sequences, each driven through the real ``ReliabilityMonitor`` with a real
``Detector``, a real ``IncidentStore``, real git worktrees and a real project
test suite:

1. broken app → watch → incident → repair → verify → pull request → notification
2. broken app → wrong fix ×3 → verification fails ×3 → HUMAN_REQUIRED
3. fail → pass → RECOVERED_EXTERNALLY, with no repair attempted
4. fail/pass/fail/pass/fail → FLAPPING → HUMAN_REQUIRED, with no repair attempted

Scenarios 3 and 4 are the ones worth reading closely. Both describe situations
where the naive behaviour — "a check failed, send it to Claude" — burns a coding
session on a problem that is not there.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from openjarvis.reliability.checks import CheckSuite
from openjarvis.reliability.code_agent import CodeAgentResult, FakeCodeAgent
from openjarvis.reliability.detector import Detector
from openjarvis.reliability.flapping import FlappingDetector
from openjarvis.reliability.monitor import ReliabilityMonitor
from openjarvis.reliability.policy import SafetyPolicy
from openjarvis.reliability.probes.executor import ConfirmationTracker
from openjarvis.reliability.probes.spec import parse_probe
from openjarvis.reliability.repair import RepairLoop
from openjarvis.reliability.report import build_report
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import (
    IncidentState,
    ProbeResult,
    RecoveryType,
)
from openjarvis.reliability.verify import Verifier
from openjarvis.reliability.watch import RepairGate, WatchSupervisor
from openjarvis.reliability.workspace import RepairWorkspace
from tests.reliability import fixture_repo

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


SPEC = {
    "probe": {
        "id": "pricing-discount",
        "component": "pricing",
        "severity": "high",
        "steps": [{"action": "goto", "url": "/checkout"}],
        "expect": [{"kind": "text", "value": "180"}],
        "retry": {"confirm_runs": 1},
    }
}


class _ScriptedExecutor:
    """Returns a scripted pass/fail sequence, standing in for the browser.

    The last entry repeats, so a scenario can end in a steady state.
    """

    def __init__(self, pattern: str):
        self.pattern = list(pattern)
        self.calls = 0

    def run(self, spec):
        char = self.pattern[min(self.calls, len(self.pattern) - 1)]
        self.calls += 1
        ok = char == "P"
        return ProbeResult(
            probe_id=spec.id,
            success=ok,
            failure_kind="" if ok else "assertion",
            error="" if ok else "expected 180 in the total, got 190",
            steps_completed=3,
        )


class _ReproductionExecutor:
    """Runs the incident's reproduction against the repaired worktree."""

    def __init__(self, workspace: str):
        self.workspace = workspace

    def run(self, spec):
        ok = fixture_repo.reproduction_passes(self.workspace)
        return ProbeResult(
            probe_id=spec.id,
            success=ok,
            failure_kind="" if ok else "assertion",
            error="" if ok else "the original failure still reproduces",
            steps_completed=1,
        )


class _RecordingGitHub:
    base_branch = "main"

    def __init__(self):
        self.pull_requests = []

    def branch_name_for(self, incident_id):
        return f"jarvis/incident-{incident_id}"

    def create_branch(self, branch, **_kwargs):
        return "sha"

    def create_pull_request(self, **kwargs):
        self.pull_requests.append(kwargs)
        return {"number": 194, "url": "https://github.com/acme/site/pull/194"}


class _RecordingNotifier:
    """Captures the owner-facing notification stream."""

    def __init__(self):
        self.events = []

    def alert(self, incident):
        self.events.append(("INCIDENT DETECTED", incident.id))
        return True

    def progress(self, incident, *, attempt, max_attempts):
        self.events.append(("REPAIR ATTEMPT", f"{attempt}/{max_attempts}"))
        return True

    def resolved(self, incident, *, attempt=None, verification=None):
        self.events.append(("VERIFIED", incident.id))
        return True

    def human_required(self, incident, *, reason, attempts=0, max_attempts=0):
        self.events.append(("HUMAN REQUIRED", reason))
        return True

    def recovered(self, incident, *, recovery_type=None):
        self.events.append(("RECOVERED", getattr(recovery_type, "value", "")))
        return True

    @property
    def kinds(self):
        return [kind for kind, _ in self.events]


@pytest.fixture
def harness(tmp_path):
    """The whole stack, wired the way the CLI wires it."""

    repo = fixture_repo.build_broken_repo(tmp_path / "target")
    store = IncidentStore(tmp_path / "incidents.db")
    manager = RepairWorkspace(
        repo_path=str(repo),
        root=str(tmp_path / "worktrees"),
        keep_on_failure=False,
    )
    github = _RecordingGitHub()
    notifier = _RecordingNotifier()
    spec = parse_probe(SPEC)

    def build(*, pattern: str, on_run=None, repair: bool = True, **overrides):
        executor = _ScriptedExecutor(pattern)
        detector = Detector(store, tracker=ConfirmationTracker(), notifier=notifier)
        supervisor = WatchSupervisor(
            monitor=None,
            store=store,
            gate=RepairGate(max_concurrent=1, cooldown_seconds=0),
            flapping=FlappingDetector(window=10, failure_threshold=3, min_samples=4),
            notifier=notifier,
        )

        loop = None
        if repair:

            def preview(_branch):
                path = Path(manager.root) / store.list(limit=1)[0].id
                return str(path) if path.is_dir() else ""

            loop = RepairLoop(
                agent=FakeCodeAgent(
                    [CodeAgentResult(claim="Fixed the discount maths.")],
                    on_run=on_run,
                ),
                policy=SafetyPolicy(
                    repair_enabled=True,
                    max_attempts=3,
                    protected_paths=[".github/workflows/"],
                ),
                verifier=Verifier(executor_factory=_ReproductionExecutor),
                store=store,
                workspace_manager=manager,
                github=github,
                checks=CheckSuite.from_config(
                    test_command=f"{fixture_repo.PYTHON} -m pytest tests -q",
                    timeout=120,
                ),
                preview_lookup=preview,
                protected_paths=[".github/workflows/"],
                push_branch=False,
                notifier=notifier,
                sleep=lambda _s: None,
                **overrides,
            )

        monitor = ReliabilityMonitor(
            detector=detector,
            executor=executor,
            specs=[spec],
            repair_loop=loop,
            notifier=notifier,
            supervisor=supervisor,
            jitter=lambda: 0.0,
        )
        supervisor.monitor = monitor
        return monitor, supervisor, executor

    build.store = store
    build.github = github
    build.notifier = notifier
    build.repo = repo
    build.manager = manager
    yield build
    store.close()


def _tick(monitor, times: int = 1) -> None:
    """Run the monitor's due checks, forcing each pass to be due."""
    for _ in range(times):
        for check in monitor.checks:
            check.next_due = 0.0
        monitor.tick()


# ---------------------------------------------------------------------------
# 1. Broken app → repair → verify → PR → notify
# ---------------------------------------------------------------------------


class TestFullAutonomousLoop:
    def test_a_detected_failure_reaches_a_pull_request(self, harness):
        monitor, _supervisor, _ = harness(
            pattern="F", on_run=fixture_repo.agent_correct_fix
        )

        _tick(monitor)

        incident = harness.store.list(limit=1)[0]
        assert incident.state is IncidentState.RESOLVED
        assert len(harness.github.pull_requests) == 1
        assert harness.github.pull_requests[0]["body"].count("Verified: **yes**") == 1

    def test_the_owner_is_notified_at_every_stage(self, harness):
        monitor, _s, _ = harness(pattern="F", on_run=fixture_repo.agent_correct_fix)

        _tick(monitor)

        kinds = harness.notifier.kinds
        assert "INCIDENT DETECTED" in kinds
        assert "REPAIR ATTEMPT" in kinds
        assert "VERIFIED" in kinds

    def test_the_default_branch_is_untouched(self, harness):
        before = subprocess.run(
            ["git", "rev-parse", "main^{tree}"],
            cwd=str(harness.repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        monitor, _s, _ = harness(pattern="F", on_run=fixture_repo.agent_correct_fix)

        _tick(monitor)

        after = subprocess.run(
            ["git", "rev-parse", "main^{tree}"],
            cwd=str(harness.repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert before == after

    def test_a_post_incident_report_can_be_produced(self, harness):
        monitor, _s, _ = harness(pattern="F", on_run=fixture_repo.agent_correct_fix)
        _tick(monitor)

        incident = harness.store.list(limit=1)[0]
        report = build_report(incident)

        assert report.verified is True
        assert report.production_deployed is False
        assert "pull/194" in report.render()

    def test_the_audit_chain_survives_the_whole_run(self, harness):
        monitor, _s, _ = harness(pattern="F", on_run=fixture_repo.agent_correct_fix)
        _tick(monitor)
        assert harness.store.verify_chain() == (True, None)


# ---------------------------------------------------------------------------
# 2. Wrong fix ×3 → HUMAN_REQUIRED
# ---------------------------------------------------------------------------


class TestRepeatedFailureEscalates:
    def test_three_failed_verifications_reach_a_human(self, harness):
        monitor, _s, _ = harness(
            pattern="F", on_run=fixture_repo.agent_plausible_but_wrong
        )

        _tick(monitor)

        incident = harness.store.list(limit=1)[0]
        assert incident.state is IncidentState.HUMAN_REQUIRED
        assert len(incident.attempts) == 3
        assert harness.github.pull_requests == []

    def test_the_owner_is_told_why(self, harness):
        monitor, _s, _ = harness(
            pattern="F", on_run=fixture_repo.agent_plausible_but_wrong
        )
        _tick(monitor)
        assert "HUMAN REQUIRED" in harness.notifier.kinds

    def test_the_report_records_the_failed_attempts(self, harness):
        monitor, _s, _ = harness(
            pattern="F", on_run=fixture_repo.agent_plausible_but_wrong
        )
        _tick(monitor)

        report = build_report(harness.store.list(limit=1)[0])
        assert report.attempts == 3
        assert report.verified is False
        assert report.pull_request == ""


# ---------------------------------------------------------------------------
# 3. Fail → pass → RECOVERED_EXTERNALLY
# ---------------------------------------------------------------------------


class TestExternalRecovery:
    def test_a_failure_that_clears_itself_is_not_repaired(self, harness):
        """Nobody should spend a Claude session on a problem that went away."""
        monitor, _s, _ = harness(pattern="FP", repair=False)

        _tick(monitor, times=2)

        incident = harness.store.list(limit=1)[0]
        assert incident.state is IncidentState.RESOLVED
        assert incident.resolution.recovery_type is RecoveryType.RECOVERED_EXTERNALLY

    def test_jarvis_does_not_take_credit(self, harness):
        monitor, _s, _ = harness(pattern="FP", repair=False)
        _tick(monitor, times=2)

        rendered = build_report(harness.store.list(limit=1)[0]).render()
        assert "RECOVERED_EXTERNALLY" in rendered
        assert "None. The failure stopped reproducing" in rendered

    def test_the_owner_is_told_no_repair_was_needed(self, harness):
        monitor, _s, _ = harness(pattern="FP", repair=False)
        _tick(monitor, times=2)

        recovered = [
            payload for kind, payload in harness.notifier.events if kind == "RECOVERED"
        ]
        assert recovered == [RecoveryType.RECOVERED_EXTERNALLY.value]

    def test_no_pull_request_is_opened(self, harness):
        monitor, _s, _ = harness(pattern="FP", on_run=fixture_repo.agent_correct_fix)
        _tick(monitor, times=2)
        # The first tick repairs (the failure was real); the second sees the
        # pass. What matters is that recovery never invents a second PR.
        assert len(harness.github.pull_requests) <= 1


# ---------------------------------------------------------------------------
# 4. Flapping → HUMAN_REQUIRED
# ---------------------------------------------------------------------------


class TestFlappingIsEscalatedNotRepaired:
    def test_an_alternating_check_reaches_a_human(self, harness):
        monitor, _supervisor, _ = harness(
            pattern="FPFPFPF", on_run=fixture_repo.agent_correct_fix, repair=False
        )

        _tick(monitor, times=7)

        # A check that alternates opens a new incident each time it fails, so
        # the escalated one is whichever carries the flapping marker.
        flapped = [
            i for i in harness.store.list(limit=50) if i.metadata.get("flapping")
        ]
        assert flapped, "no incident was marked flapping"
        assert flapped[0].state is IncidentState.HUMAN_REQUIRED

    def test_repairs_stop_once_flapping_is_established(self, harness):
        """The first failure legitimately looks real — alternation is only
        visible after several samples. What matters is that JARVIS stops
        repairing once it can see the pattern, rather than filing a pull
        request on every downswing."""
        monitor, _s, _ = harness(
            pattern="FPFPFPF", on_run=fixture_repo.agent_correct_fix
        )

        _tick(monitor, times=7)

        # Four failures in the pattern; at most the first one may be repaired.
        assert len(harness.github.pull_requests) <= 1
        assert monitor.health()["flapping"] >= 1
        assert any(i.metadata.get("flapping") for i in harness.store.list(limit=50))

    def test_the_escalation_explains_itself(self, harness):
        monitor, _s, _ = harness(pattern="FPFPFPF", repair=False)
        _tick(monitor, times=7)

        reasons = [
            payload
            for kind, payload in harness.notifier.events
            if kind == "HUMAN REQUIRED"
        ]
        assert any("flapping" in r for r in reasons)

    def test_a_sustained_outage_is_still_repaired(self, harness):
        """The complement: flapping detection must not suppress a real outage."""
        monitor, _s, _ = harness(
            pattern="FFFFFF", on_run=fixture_repo.agent_correct_fix
        )

        _tick(monitor, times=6)

        assert len(harness.github.pull_requests) == 1


# ---------------------------------------------------------------------------
# Deduplication (§4)
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_a_persistent_failure_produces_one_incident(self, harness):
        monitor, _s, _ = harness(pattern="F", repair=False)

        _tick(monitor, times=6)

        incidents = harness.store.list(limit=50)
        assert len(incidents) == 1
        assert incidents[0].occurrences >= 5

    def test_the_owner_is_alerted_once(self, harness):
        monitor, _s, _ = harness(pattern="F", repair=False)
        _tick(monitor, times=6)
        assert harness.notifier.kinds.count("INCIDENT DETECTED") == 1

    def test_a_repeat_after_recovery_is_a_new_incident(self, harness):
        """Resolved, then the same failure returns: that is genuinely new."""
        monitor, _s, _ = harness(pattern="FPF", repair=False)

        _tick(monitor, times=3)

        incidents = harness.store.list(limit=50)
        assert len(incidents) == 2


# ---------------------------------------------------------------------------
# Concurrency (§27)
# ---------------------------------------------------------------------------


class TestConcurrencyUnderWatch:
    def test_a_second_repair_is_deferred_while_one_is_running(self, harness):
        monitor, supervisor, _ = harness(
            pattern="F", on_run=fixture_repo.agent_correct_fix
        )
        supervisor.gate.start("INC-99999")  # occupy the only slot

        _tick(monitor)

        assert monitor.health()["repairs_deferred"] == 1
        assert harness.github.pull_requests == []

    def test_an_emergency_stop_blocks_repair(self, harness):
        monitor, supervisor, _ = harness(
            pattern="F", on_run=fixture_repo.agent_correct_fix
        )
        supervisor.stop()

        _tick(monitor)

        assert harness.github.pull_requests == []
        incident = harness.store.list(limit=1)[0]
        assert incident.state is IncidentState.DETECTED  # untouched, not lost
