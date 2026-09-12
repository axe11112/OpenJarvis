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

        assert queue.admit_next().admitted
        second = queue.admit_next()
        assert not second.admitted
        assert "slot" in second.reason

    def test_finishing_frees_the_slot(self):
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-1"))
        queue.submit(_feature("FEAT-2"))

        first = queue.admit_next()
        queue.finish(first.task.feature_id)
        assert queue.admit_next().admitted

    def test_an_empty_queue_admits_nothing(self):
        assert not DevelopmentQueue().admit_next().admitted

    def test_zero_concurrency_is_refused_at_construction(self):
        with pytest.raises(ValueError):
            DevelopmentQueue(max_concurrent=0)


class TestOrdering:
    def test_higher_priority_runs_first(self):
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-normal", Priority.P3))
        queue.submit(_feature("FEAT-urgent", Priority.P2))
        assert queue.admit_next().task.feature_id == "FEAT-urgent"

    def test_equal_priority_runs_in_arrival_order(self):
        queue = DevelopmentQueue(max_concurrent=3)
        for n in range(3):
            queue.submit(_feature(f"FEAT-{n}", Priority.P3))
        admitted = [queue.admit_next().task.feature_id for _ in range(3)]
        assert admitted == ["FEAT-0", "FEAT-1", "FEAT-2"]

    def test_maintenance_runs_last(self):
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-maint", Priority.P4))
        queue.submit(_feature("FEAT-normal", Priority.P3))
        assert queue.admit_next().task.feature_id == "FEAT-normal"


class TestProductionWins:
    def test_nothing_is_admitted_while_reliability_is_working(self):
        queue = DevelopmentQueue(production_busy=lambda: True)
        queue.submit(_feature("FEAT-1"))
        decision = queue.admit_next()
        assert not decision.admitted
        assert "reliability" in decision.reason

    def test_work_resumes_once_production_is_quiet(self):
        busy = {"value": True}
        queue = DevelopmentQueue(production_busy=lambda: busy["value"])
        queue.submit(_feature("FEAT-1"))
        assert not queue.admit_next().admitted
        busy["value"] = False
        assert queue.admit_next().admitted

    def test_a_running_feature_is_told_to_yield(self):
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-1"))
        queue.admit_next()

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
        assert not queue.admit_next().admitted


class TestInspection:
    def test_the_snapshot_shows_what_is_running_and_waiting(self):
        queue = DevelopmentQueue()
        queue.submit(_feature("FEAT-1"))
        queue.submit(_feature("FEAT-2"))
        queue.admit_next()

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

    DevelopmentQueue.admit_next() refuses to start feature work while
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
            decision = queue.admit_next()
            assert not decision.admitted, (
                "feature work started while a production change was in flight"
            )
            assert "reliability" in decision.reason

        assert queue.admit_next().admitted, (
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
            assert not queue.admit_next().admitted

            os.kill(holder.pid, signal.SIGKILL)
            holder.join(timeout=10.0)
            time.sleep(0.2)

            assert queue.admit_next().admitted, (
                "a crashed production change stalled every feature permanently"
            )
        finally:
            if holder.is_alive():  # pragma: no cover - defensive
                holder.terminate()
                holder.join(timeout=5.0)

    def test_assemble_supplies_a_default_production_busy(self):
        """Asserted at the wiring, because the wiring is what was missing."""
        import inspect

        from openjarvis.wiz import assemble as assemble_mod

        source = inspect.getsource(assemble_mod.assemble)
        assert "production_busy = _production_lease.is_held" in source, (
            "assemble() no longer supplies a production_busy source, so the "
            "queue's production deferral is inert again"
        )
        code = [
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        ]
        assert not any("current_holder" in line for line in code), (
            "production_busy must not be built on current_holder(): a "
            "SIGKILLed holder's record outlives its lock and would stall "
            "feature work forever"
        )
