"""The queue, and the rule that an outage outranks a dashboard."""

from __future__ import annotations

import pytest

from openjarvis.wiz.features.model import FeatureRequest, Priority
from openjarvis.wiz.features.queue import DevelopmentQueue


def _feature(feature_id: str, priority: Priority = Priority.P3) -> FeatureRequest:
    return FeatureRequest(id=feature_id, title=feature_id, priority=priority)


class TestConcurrency:
    def test_only_one_code_task_runs_by_default(self):
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-1"))
        queue.submit(_feature("FEAT-2"))

        assert queue.admit("FEAT-1").admitted
        second = queue.admit("FEAT-2")
        assert not second.admitted
        assert "slot" in second.reason

    def test_finishing_frees_the_slot(self):
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-1"))
        queue.submit(_feature("FEAT-2"))

        first = queue.admit("FEAT-1")
        queue.finish(first.task.feature_id)
        assert queue.admit("FEAT-2").admitted

    def test_a_feature_that_was_never_submitted_is_still_admitted(self):
        """The waiting list is in memory; the features are on disk.

        After a restart nothing is waiting, so refusing what was never
        submitted would mean a restart silently stopped every feature the
        machine was working on.
        """
        decision = DevelopmentQueue().admit("FEAT-restored")
        assert decision.admitted
        assert decision.task.feature_id == "FEAT-restored"

    def test_a_feature_that_already_holds_the_slot_is_admitted_again(self):
        """Re-running one is ordinary. Refusing the caller the slot it already
        holds would be a deadlock against itself."""
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-1"))
        assert queue.admit("FEAT-1").admitted
        again = queue.admit("FEAT-1")
        assert again.admitted
        assert "already holds" in again.reason

    def test_zero_concurrency_is_refused_at_construction(self):
        with pytest.raises(ValueError):
            DevelopmentQueue(max_concurrent=0)


class TestOrdering:
    """Priorities order what a person should look at next.

    They do not start anything: admission is by name, because something has
    always already decided which feature to work on by the time the queue is
    asked.
    """

    def test_higher_priority_is_next(self):
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-normal", Priority.P3))
        queue.submit(_feature("FEAT-urgent", Priority.P2))
        assert queue.next_waiting().feature_id == "FEAT-urgent"

    def test_equal_priority_keeps_arrival_order(self):
        queue = DevelopmentQueue(max_concurrent=3)
        for n in range(3):
            queue.submit(_feature(f"FEAT-{n}", Priority.P3))
        assert [t.feature_id for t in queue.waiting()] == [
            "FEAT-0",
            "FEAT-1",
            "FEAT-2",
        ]

    def test_maintenance_is_last(self):
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-maint", Priority.P4))
        queue.submit(_feature("FEAT-normal", Priority.P3))
        assert queue.next_waiting().feature_id == "FEAT-normal"

    def test_nothing_waiting_has_no_next(self):
        assert DevelopmentQueue().next_waiting() is None

    def test_admitting_takes_the_task_off_the_waiting_list(self):
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-1"))
        queue.admit("FEAT-1")
        assert queue.waiting() == []


class TestProductionWins:
    def test_nothing_is_admitted_while_reliability_is_working(self):
        queue = DevelopmentQueue(production_busy=lambda: True)
        queue.submit(_feature("FEAT-1"))
        decision = queue.admit("FEAT-1")
        assert not decision.admitted
        assert "reliability" in decision.reason

    def test_work_resumes_once_production_is_quiet(self):
        busy = {"value": True}
        queue = DevelopmentQueue(production_busy=lambda: busy["value"])
        queue.submit(_feature("FEAT-1"))
        assert not queue.admit("FEAT-1").admitted
        busy["value"] = False
        assert queue.admit("FEAT-1").admitted

    def test_a_running_feature_is_told_to_yield(self):
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-1"))
        queue.admit("FEAT-1")

        yielding = queue.yield_to_production("the site is down")
        assert [t.feature_id for t in yielding] == ["FEAT-1"]
        assert queue.must_yield("FEAT-1")

    def test_a_feature_that_has_not_started_is_not_told_to_yield(self):
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-1"))
        assert queue.yield_to_production() == []
        assert not queue.must_yield("FEAT-1")


class TestReliabilityPrioritiesAreReserved:
    @pytest.mark.parametrize("priority", [Priority.P0, Priority.P1])
    def test_a_feature_cannot_claim_a_reliability_priority(self, priority):
        # Otherwise the word "urgent" in a chat message outranks an outage.
        queue = DevelopmentQueue()
        task = queue.submit(_feature("FEAT-pushy", priority))
        assert task.priority is Priority.P2

    def test_a_demoted_feature_still_loses_to_reliability(self):
        queue = DevelopmentQueue(production_busy=lambda: True)
        queue.submit(_feature("FEAT-pushy", Priority.P0))
        assert not queue.admit("FEAT-pushy").admitted


class TestInspection:
    def test_the_snapshot_shows_what_is_running_and_waiting(self):
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-1"))
        queue.submit(_feature("FEAT-2"))
        queue.admit("FEAT-1")

        snapshot = queue.snapshot()
        assert [r["feature_id"] for r in snapshot["running"]] == ["FEAT-1"]
        assert [w["feature_id"] for w in snapshot["waiting"]] == ["FEAT-2"]
        assert snapshot["waiting"][0]["position"] == 1
        assert snapshot["max_concurrent"] == 1

    def test_cancelling_removes_a_waiting_task(self):
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-1"))
        assert queue.cancel("FEAT-1")
        assert queue.waiting() == []
        assert not queue.cancel("FEAT-nonexistent")


class TestProductionBusyIsConnectedToSomething:
    """The deferral was a closed loop answering "not busy" forever.

    DevelopmentQueue.admit() refuses to start feature work while
    production_busy() is true, and assemble._reliability_busy() answers the
    same question for auto_ship_if_eligible by reading it back out of the
    queue's snapshot -- where it had come from this same callback. No caller of
    assemble() ever passed one, so it defaulted to `lambda: False` and the
    whole mechanism was inert. Every test above passes because it constructs
    the queue with a callback itself; nothing tested the wiring, because there
    was none.

    The source of truth is the cross-process production-change lease, so these
    drive a real one.
    """

    def test_a_held_production_lease_defers_feature_work(self, tmp_path):
        from openjarvis.core.proclock import production_change_lease

        lease = production_change_lease(owner="a-ship-in-progress", root=tmp_path)
        probe = production_change_lease(owner="wiz-queue-probe", root=tmp_path)
        queue = DevelopmentQueue(production_busy=probe.is_held)
        queue.submit(_feature("FEAT-1"))

        with lease.acquire(timeout=2.0):
            decision = queue.admit("FEAT-1")
            assert not decision.admitted, (
                "feature work started while a production change was in flight"
            )
            assert "reliability" in decision.reason

        assert queue.admit("FEAT-1").admitted, (
            "feature work did not resume once production was free again"
        )

    def test_a_crashed_holder_does_not_stall_feature_work_forever(self, tmp_path):
        """The trap this must not be built on.

        The lease's holder *record* outlives a SIGKILLed process; its lock does
        not. A probe reading the record would defer every feature for the rest
        of the machine's uptime after one crash -- silently, and with no way to
        tell it from "production is legitimately busy".
        """
        import multiprocessing
        import os
        import signal
        import time

        from openjarvis.core.proclock import production_change_lease
        from tests.wiz.test_proclock import _hold_until_killed

        lease_path = production_change_lease(owner="x", root=tmp_path).path
        ready = multiprocessing.Event()
        holder = multiprocessing.Process(
            target=_hold_until_killed, args=(str(lease_path), ready)
        )
        holder.start()
        try:
            assert ready.wait(timeout=10.0)
            probe = production_change_lease(owner="wiz-queue-probe", root=tmp_path)
            queue = DevelopmentQueue(production_busy=probe.is_held)
            queue.submit(_feature("FEAT-1"))
            assert not queue.admit("FEAT-1").admitted

            os.kill(holder.pid, signal.SIGKILL)
            holder.join(timeout=10.0)
            time.sleep(0.2)

            assert queue.admit("FEAT-1").admitted, (
                "a crashed production change stalled every feature permanently"
            )
        finally:
            if holder.is_alive():  # pragma: no cover - defensive
                holder.terminate()
                holder.join(timeout=5.0)

    def test_assemble_supplies_a_default_production_busy(self):
        """Asserted at the wiring, because the wiring is what was missing.

        Parsed rather than grepped: a substring search is satisfied by the
        comment explaining the wiring, which is the failure mode this whole
        class is named after.
        """
        import ast
        import inspect
        import textwrap

        from openjarvis.wiz import assemble as assemble_mod

        tree = ast.parse(textwrap.dedent(inspect.getsource(assemble_mod.assemble)))

        wired = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "DevelopmentQueue":
                continue
            for kw in node.keywords:
                if kw.arg != "production_busy":
                    continue
                if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                    continue
                wired = True
        assert wired, (
            "assemble() builds the queue with no production_busy, so the "
            "production deferral is inert again"
        )

        # The default must come from is_held, never current_holder: a SIGKILLed
        # holder's record outlives its lock and would stall feature work for the
        # rest of the machine's uptime.
        attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        assert "is_held" in attributes, (
            "production_busy is no longer sourced from ProcessLease.is_held"
        )
        assert "current_holder" not in attributes, (
            "production_busy must not be built on current_holder(): a "
            "SIGKILLed holder's record outlives its lock and would stall "
            "feature work forever"
        )
