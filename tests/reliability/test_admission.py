"""Tests for cross-process, restart-surviving repair admission.

Every assertion here is about something a single-process test cannot see: a
second watcher, a watcher that restarted, and a watcher that was killed. The
defect these cover is that :class:`RepairGate` arbitrated concurrency and
cooldown entirely inside one Python object, so both of those guarantees were
void the moment this system ran the way it is actually deployed -- a launchd
watcher plus whatever an operator starts by hand during an incident.

The processes in these tests are real subprocesses holding real ``flock``s. A
fake would prove nothing: the whole claim is that the *kernel* arbitrates, and
that the kernel releases a claim when its holder dies.
"""

from __future__ import annotations

import ast
import os
import signal
import subprocess
import sys
import textwrap

from openjarvis.reliability.admission import RepairAdmission
from openjarvis.reliability.watch import RepairGate, admission_dir


def _gate(root, **kwargs) -> RepairGate:
    """A gate as a separate process would build it: its own admission object.

    Deliberately not shared between gates in these tests. Two gates sharing one
    ``RepairAdmission`` instance would pass through the same in-memory handle
    dictionary and could agree by accident; two instances over one directory
    is what two processes actually look like.
    """
    kwargs.setdefault("cooldown_seconds", 0.0)
    kwargs.setdefault("pending_pr_cooldown_seconds", 0.0)
    return RepairGate(admission=RepairAdmission(root=root), **kwargs)


class TestTwoProcessesCannotRepairTheSameThing:
    def test_a_second_process_is_refused_the_same_incident(self, tmp_path):
        first = _gate(tmp_path)
        second = _gate(tmp_path)

        assert first.start("INC-1", fingerprint="fp-a") is True

        allowed, reason = second.may_start("INC-1", fingerprint="fp-a")
        assert allowed is False
        assert "another process" in reason
        assert second.start("INC-1", fingerprint="fp-a") is False

    def test_a_second_process_is_refused_the_same_failure(self, tmp_path):
        """A recurring failure opens a new incident each time it returns.

        Keying only on the incident id would let the same root cause be
        repaired twice concurrently under two ids -- two agents, two branches,
        two pull requests for one problem.
        """
        first = _gate(tmp_path)
        second = _gate(tmp_path)

        assert first.start("INC-1", fingerprint="same-root-cause") is True
        assert second.start("INC-2", fingerprint="same-root-cause") is False

    def test_the_concurrency_limit_is_machine_wide(self, tmp_path):
        first = _gate(tmp_path, max_concurrent=1)
        second = _gate(tmp_path, max_concurrent=1)

        assert first.start("INC-1", fingerprint="fp-a") is True
        allowed, reason = second.may_start("INC-2", fingerprint="fp-b")
        assert allowed is False
        assert "machine-wide concurrency limit" in reason

    def test_the_limit_counts_repairs_not_lock_files(self, tmp_path):
        """A fingerprinted repair takes two slots and is still one repair."""
        first = _gate(tmp_path, max_concurrent=2)
        second = _gate(tmp_path, max_concurrent=2)

        assert first.start("INC-1", fingerprint="fp-a") is True
        assert second.start("INC-2", fingerprint="fp-b") is True

    def test_a_finished_repair_frees_the_slot_for_another_process(self, tmp_path):
        first = _gate(tmp_path)
        second = _gate(tmp_path)

        assert first.start("INC-1", fingerprint="fp-a") is True
        first.finish("INC-1", fingerprint="fp-a", succeeded=True)

        assert second.start("INC-1", fingerprint="fp-a") is True

    def test_a_clean_repair_still_gives_its_slot_back(self, tmp_path):
        """The outcome that earns no cooldown is the one that used to leak.

        ``finish`` returned early for a repair that succeeded without opening a
        pull request, because there was nothing to cool down. With the slot
        held on disk, that early return would strand a claim per clean repair.
        """
        gate = _gate(tmp_path)
        assert gate.start("INC-1", fingerprint="fp-a") is True
        assert RepairAdmission(root=tmp_path).snapshot()["running"] == 1

        gate.finish(
            "INC-1", fingerprint="fp-a", succeeded=True, opened_pull_request=False
        )

        assert RepairAdmission(root=tmp_path).snapshot()["running"] == 0


class TestCooldownsSurviveARestart:
    def test_a_pending_pull_request_cooldown_outlives_the_process(self, tmp_path):
        """The cooldown that stops one outage becoming six pull requests.

        It lived in memory, so restarting the watcher -- which is what an
        operator does during an incident -- put the system straight back into
        the state this cooldown exists to prevent.
        """
        before = _gate(tmp_path, pending_pr_cooldown_seconds=3600.0)
        assert before.start("INC-1", fingerprint="fp-a") is True
        before.finish(
            "INC-1", fingerprint="fp-a", succeeded=True, opened_pull_request=True
        )

        after = _gate(tmp_path, pending_pr_cooldown_seconds=3600.0)
        allowed, reason = after.may_start("INC-1", fingerprint="fp-a")
        assert allowed is False
        assert "pull request is already open" in reason

    def test_a_failure_cooldown_outlives_the_process(self, tmp_path):
        before = _gate(tmp_path, cooldown_seconds=600.0)
        assert before.start("INC-1", fingerprint="fp-a") is True
        before.finish("INC-1", fingerprint="fp-a", succeeded=False)

        after = _gate(tmp_path, cooldown_seconds=600.0)
        allowed, reason = after.may_start("INC-9", fingerprint="fp-a")
        assert allowed is False
        assert "failed repair" in reason

    def test_a_cooldown_that_has_expired_does_not_block(self, tmp_path):
        clock = [1_000_000.0]
        admission = RepairAdmission(root=tmp_path, clock=lambda: clock[0])
        admission.release(
            "INC-1", fingerprint="fp-a", cooldown_seconds=60.0, reason="x"
        )

        assert admission.why_not("INC-1", fingerprint="fp-a") != ""
        clock[0] += 61.0
        assert admission.why_not("INC-1", fingerprint="fp-a") == ""

    def test_an_owner_clearing_a_cooldown_clears_it_on_disk(self, tmp_path):
        before = _gate(tmp_path, cooldown_seconds=600.0)
        assert before.start("INC-1", fingerprint="fp-a") is True
        before.finish("INC-1", fingerprint="fp-a", succeeded=False)

        blocked, _why = _gate(tmp_path).may_start("INC-1", fingerprint="fp-a")
        assert blocked is False, "the cooldown has to be on disk for this to mean much"

        assert "fp-a" in before.clear_cooldown("INC-1", "fp-a")

        after = _gate(tmp_path, cooldown_seconds=600.0)
        allowed, _reason = after.may_start("INC-1", fingerprint="fp-a")
        assert allowed is True

    def test_a_corrupt_cooldown_file_is_not_read_as_a_block(self, tmp_path):
        """A truncated write from a killed process must not be fatal.

        It costs the durability of the cooldowns it held, which is logged; it
        must not make every future admission unreadable, which would stop
        repairs for good.
        """
        admission = RepairAdmission(root=tmp_path)
        admission.release("INC-1", cooldown_seconds=600.0, reason="x")
        (tmp_path / "cooldowns.json").write_text("{not json")

        assert admission.why_not("INC-1") == ""


class TestAKilledHolderDoesNotBlockForever:
    def test_a_sigkilled_process_releases_its_claim(self, tmp_path):
        """The reason this is ``flock`` and not a PID file with a TTL.

        A recorded-owner scheme has to decide whether a holder that has gone
        quiet is dead, and a live-but-slow repair looks identical from outside.
        The kernel does not have to decide: it knows.
        """
        script = textwrap.dedent(
            f"""
            import time
            from openjarvis.reliability.admission import RepairAdmission
            a = RepairAdmission(root={str(tmp_path)!r})
            assert a.claim("INC-1", fingerprint="fp-a") is True
            print("claimed", flush=True)
            time.sleep(120)
            """
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout.readline().strip() == "claimed"
            here = RepairAdmission(root=tmp_path)
            assert here.claim("INC-1", fingerprint="fp-a") is False

            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=10)

            # No stale-owner sweep, no timeout, no grace period: the claim is
            # already gone by the time the process is reaped.
            assert here.claim("INC-1", fingerprint="fp-a") is True
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)

    def test_a_killed_holder_leaves_its_record_but_not_its_claim(self, tmp_path):
        """Liveness is asked of the kernel, never read out of the record.

        The record is written after the lock is taken and truncated before it
        is released, so a killed holder's record outlives its lock. Trusting
        the record would turn one crash into a permanent refusal to repair
        that failure again.
        """
        script = textwrap.dedent(
            f"""
            import time
            from openjarvis.reliability.admission import RepairAdmission
            a = RepairAdmission(root={str(tmp_path)!r})
            a.claim("INC-1", fingerprint="fp-a")
            print("claimed", flush=True)
            time.sleep(120)
            """
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
        )
        try:
            assert child.stdout.readline().strip() == "claimed"
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()

        slots = list((tmp_path / "slots").glob("*.lock"))
        assert slots and any(path.read_text().strip() for path in slots), (
            "the test is meaningless unless a record was actually left behind"
        )
        assert RepairAdmission(root=tmp_path).why_not("INC-1", fingerprint="fp-a") == ""


class TestAnUnreadableRegistryRefuses:
    def test_a_registry_that_cannot_be_read_refuses_admission(self, tmp_path):
        """Fail closed. A repair deferred is a delay; two is the failure."""
        script = textwrap.dedent(
            f"""
            import time
            from openjarvis.reliability.admission import RepairAdmission
            a = RepairAdmission(root={str(tmp_path)!r})
            with a._registry():
                print("holding", flush=True)
                time.sleep(120)
            """
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
        )
        try:
            assert child.stdout.readline().strip() == "holding"
            blocked = RepairAdmission(root=tmp_path, lease_timeout=0.2)
            reason = blocked.why_not("INC-1", fingerprint="fp-a")
            assert "could not be read" in reason
            assert blocked.claim("INC-1", fingerprint="fp-a") is False
        finally:
            child.kill()
            child.wait(timeout=10)

    def test_a_gate_reports_the_refusal_rather_than_admitting(self, tmp_path):
        class Unreadable(RepairAdmission):
            def why_not(self, incident_id, *, fingerprint="", max_concurrent=1):
                return "the repair admission registry could not be read (boom)"

        gate = RepairGate(admission=Unreadable(root=tmp_path))
        allowed, reason = gate.may_start("INC-1", fingerprint="fp-a")
        assert allowed is False
        assert "could not be read" in reason

    def test_a_refused_claim_records_no_local_slot(self, tmp_path):
        """A local claim recorded against a global one that never happened
        would block the incident until the process restarted."""

        class Refusing(RepairAdmission):
            def claim(self, incident_id, *, fingerprint="", max_concurrent=1):
                return False

        gate = RepairGate(admission=Refusing(root=tmp_path))
        assert gate.start("INC-1", fingerprint="fp-a") is False
        assert gate.active == []


class TestWithoutAnAdmissionStoreNothingChanges:
    def test_a_gate_with_no_admission_behaves_as_before(self, tmp_path):
        gate = RepairGate(cooldown_seconds=0.0)
        assert gate.start("INC-1", fingerprint="fp-a") is True
        assert gate.start("INC-1", fingerprint="fp-a") is False
        gate.finish("INC-1", fingerprint="fp-a", succeeded=True)
        assert gate.start("INC-1", fingerprint="fp-a") is True
        assert gate.snapshot()["machine"] is None


class TestTheWatcherActuallyGetsOne:
    """The gate is only as good as its wiring.

    This exact class of defect -- a safety collaborator that defaults to
    ``None``, is injected by every test, and is omitted where the real object
    is built -- has been found five times in this codebase. Matched against the
    parsed call, not against the source text, because a string search is
    satisfied by a comment mentioning the keyword and by ``admission=None``.
    """

    def _gate_call(self) -> ast.Call:
        import openjarvis.cli.reliability_cmd as mod

        tree = ast.parse(open(mod.__file__).read())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "_build_supervised_monitor"
            ):
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "RepairGate"
                    ):
                        return inner
        raise AssertionError("_build_supervised_monitor no longer builds a RepairGate")

    def test_the_watcher_gate_is_given_an_admission_store(self):
        call = self._gate_call()
        given = {kw.arg: kw.value for kw in call.keywords}
        assert "admission" in given, (
            "the watcher's RepairGate is built without cross-process admission; "
            "two watchers would each admit the same repair"
        )
        value = given["admission"]
        assert not (isinstance(value, ast.Constant) and value.value is None)
        assert isinstance(value, ast.Call), "admission must be an actual store"
        assert getattr(value.func, "id", "") == "RepairAdmission"

    def test_the_watcher_and_the_cli_resolve_one_directory(self, tmp_path):
        call = self._gate_call()
        given = {kw.arg: kw.value for kw in call.keywords}
        root = {kw.arg: kw.value for kw in given["admission"].keywords}["root"]
        assert isinstance(root, ast.Call)
        assert getattr(root.func, "id", "") == "admission_dir", (
            "the watcher must resolve the admission directory the same way every "
            "other reader does, or two processes have two empty registries"
        )

    def test_the_admission_directory_follows_the_incident_database(self, tmp_path):
        class _R:
            db_path = str(tmp_path / "state" / "incidents.db")

        class _C:
            reliability = _R()

        assert admission_dir(_C()) == tmp_path / "state" / "admission"


class TestTheStoreItself:
    def test_claiming_twice_in_one_process_is_refused(self, tmp_path):
        admission = RepairAdmission(root=tmp_path)
        assert admission.claim("INC-1", fingerprint="fp-a") is True
        assert admission.claim("INC-1", fingerprint="fp-a") is False

    def test_releasing_an_unheld_claim_is_harmless(self, tmp_path):
        RepairAdmission(root=tmp_path).release("INC-nothing")

    def test_a_snapshot_names_the_holder(self, tmp_path):
        admission = RepairAdmission(root=tmp_path, owner="watcher-a")
        admission.claim("INC-1", fingerprint="fp-a")
        snap = RepairAdmission(root=tmp_path).snapshot()
        assert snap["running"] == 1
        assert any("watcher-a" in line for line in snap["active"])
        assert any(str(os.getpid()) in line for line in snap["active"])

    def test_a_snapshot_never_raises(self, tmp_path):
        admission = RepairAdmission(root=tmp_path / "nope", lease_timeout=0.0)
        assert isinstance(admission.snapshot(), dict)

    def test_two_keys_that_differ_get_two_slots(self, tmp_path):
        admission = RepairAdmission(root=tmp_path)
        assert admission.claim("INC-1", fingerprint="fp-a") is True
        assert len(list((tmp_path / "slots").glob("*.lock"))) == 2

    def test_an_unfingerprinted_repair_gets_one_slot(self, tmp_path):
        admission = RepairAdmission(root=tmp_path)
        assert admission.claim("INC-1") is True
        assert len(list((tmp_path / "slots").glob("*.lock"))) == 1
