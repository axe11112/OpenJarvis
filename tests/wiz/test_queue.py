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
