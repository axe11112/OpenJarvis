"""``assemble``'s own composition: what every intake channel gets for free.

Regression for a real gap: ``ProductVerbs.runner`` -- what ``jarvis wiz
build`` and every other ``feature.build`` caller actually invokes -- called
only ``pipeline.run()``, which stops dead at READY by design. Only
``wiz_routes.py``'s HTTP-only ``_start()`` also called
``auto_ship_if_eligible()`` afterward. On a machine with no FastAPI installed
(an optional extra), that HTTP path never runs at all, so a genuinely
LOW-risk feature built through the CLI -- the only intake channel actually
reachable there -- could reach READY and then sit forever: the shipping
policy said yes, and nothing ever asked it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from openjarvis.wiz.assemble import (
    _audit_healthy,
    _emergency_stop_engaged,
    _reliability_busy,
    _run_and_auto_ship,
)
from openjarvis.wiz.features.model import FeatureState


@dataclass
class _FakeFeature:
    state: FeatureState


@dataclass
class _FakePipeline:
    """Records what it was asked to do, in order."""

    run_result: _FakeFeature
    auto_ship_result: Any = None
    journal: Any = None
    queue: Any = None
    calls: List[str] = field(default_factory=list)
    auto_ship_kwargs: Dict[str, Any] = field(default_factory=dict)

    def run(self, feature_id: str) -> _FakeFeature:
        self.calls.append(f"run:{feature_id}")
        return self.run_result

    def auto_ship_if_eligible(self, feature_id: str, **kwargs: Any) -> Any:
        self.calls.append(f"auto_ship:{feature_id}")
        self.auto_ship_kwargs = kwargs
        return self.auto_ship_result


class TestRunAndAutoShip:
    def test_ready_calls_run_then_auto_ship_in_order(self):
        shipped = _FakeFeature(state=FeatureState.COMPLETE)
        pipeline = _FakePipeline(
            run_result=_FakeFeature(state=FeatureState.READY),
            auto_ship_result=shipped,
        )
        result = _run_and_auto_ship(pipeline, config=None, feature_id="FEAT-1")
        assert pipeline.calls == ["run:FEAT-1", "auto_ship:FEAT-1"]
        assert result is shipped

    def test_not_ready_never_calls_auto_ship(self):
        """HUMAN_REQUIRED, BUILDING, MERGING, ... -- anything but READY."""
        for state in FeatureState:
            if state is FeatureState.READY:
                continue
            pipeline = _FakePipeline(run_result=_FakeFeature(state=state))
            result = _run_and_auto_ship(pipeline, config=None, feature_id="FEAT-1")
            assert pipeline.calls == ["run:FEAT-1"], state
            assert result is pipeline.run_result

    def test_the_three_guards_are_wired_through(self):
        """auto_ship_if_eligible must receive real, callable guards -- not be
        left to its own permissive defaults (all-clear) by accident."""
        pipeline = _FakePipeline(
            run_result=_FakeFeature(state=FeatureState.READY),
            auto_ship_result=_FakeFeature(state=FeatureState.COMPLETE),
        )
        _run_and_auto_ship(pipeline, config=None, feature_id="FEAT-1")
        kwargs = pipeline.auto_ship_kwargs
        assert set(kwargs) == {
            "emergency_stop_engaged",
            "reliability_busy",
            "audit_healthy",
        }
        assert all(callable(v) for v in kwargs.values())


class TestEmergencyStopEngaged:
    def test_engaged_when_the_flag_file_exists(self, tmp_path, monkeypatch):
        flag = tmp_path / "STOPPED"
        flag.write_text("stopped")
        monkeypatch.setattr(
            "openjarvis.reliability.watch.stop_flag_path", lambda config: flag
        )
        assert _emergency_stop_engaged(config=None) is True

    def test_not_engaged_when_no_flag_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "openjarvis.reliability.watch.stop_flag_path",
            lambda config: tmp_path / "STOPPED",
        )
        assert _emergency_stop_engaged(config=None) is False

    def test_unreadable_is_treated_as_engaged(self, monkeypatch):
        def _raise(config):
            raise OSError("no permission")

        monkeypatch.setattr("openjarvis.reliability.watch.stop_flag_path", _raise)
        assert _emergency_stop_engaged(config=None) is True


class TestAuditHealthy:
    def test_no_journal_is_not_healthy(self):
        assert _audit_healthy(None) is False

    def test_an_intact_chain_is_healthy(self):
        class _Journal:
            def verify(self):
                return True, None

        assert _audit_healthy(_Journal()) is True

    def test_a_broken_chain_is_not_healthy(self):
        class _Journal:
            def verify(self):
                return False, 3

        assert _audit_healthy(_Journal()) is False

    def test_a_journal_that_cannot_answer_is_not_healthy(self):
        class _Journal:
            def verify(self):
                raise RuntimeError("db is locked")

        assert _audit_healthy(_Journal()) is False


class TestReliabilityBusy:
    def test_no_queue_is_not_busy(self):
        assert _reliability_busy(pipeline=type("P", (), {"queue": None})()) is False

    def test_production_busy_flag_is_honoured(self):
        class _Queue:
            def snapshot(self):
                return {"production_busy": True}

        pipeline = type("P", (), {"queue": _Queue()})()
        assert _reliability_busy(pipeline) is True

    def test_production_not_busy(self):
        class _Queue:
            def snapshot(self):
                return {"production_busy": False}

        pipeline = type("P", (), {"queue": _Queue()})()
        assert _reliability_busy(pipeline) is False

    def test_a_queue_that_cannot_answer_defers_to_busy(self):
        class _Queue:
            def snapshot(self):
                raise RuntimeError("queue is gone")

        pipeline = type("P", (), {"queue": _Queue()})()
        assert _reliability_busy(pipeline) is True


class TestAssembleHonoursItsSandbox:
    """`assemble(home=...)` must not reach for the operator's real state.

    The Wiz-private lease it replaced was `ProcessLease(root / "ship.lock")`,
    where root honours home=. production_change_lease() defaults to the config
    root instead -- correctly, since the reliability repair loop resolves the
    same place -- so a caller that passed a scratch home= would have contended
    on the real /~.openjarvis/production-change.lock: a test, or a second
    instance pointed at a sandbox, blocking or being blocked by a real ship.
    """

    def _lease_call_roots(self):
        import ast
        import inspect
        import textwrap

        from openjarvis.wiz import assemble as assemble_mod

        tree = ast.parse(textwrap.dedent(inspect.getsource(assemble_mod.assemble)))
        roots = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "production_change_lease":
                continue
            roots.append({kw.arg for kw in node.keywords})
        return roots

    def test_every_lease_in_assemble_is_sandbox_aware(self):
        roots = self._lease_call_roots()
        assert roots, "assemble() no longer builds a production-change lease"
        for keywords in roots:
            assert "root" in keywords, (
                "a production_change_lease call in assemble() ignores home=, so "
                "a sandboxed instance would contend on the operator's real lock"
            )

    def test_a_sandboxed_home_keeps_the_lease_inside_it(self, tmp_path):
        """Behaviour, not just the call shape."""
        from openjarvis.core.proclock import production_change_lease

        sandboxed = production_change_lease(owner="wiz@test", root=tmp_path)
        real = production_change_lease(owner="wiz@test")
        assert sandboxed.path != real.path
        assert tmp_path in sandboxed.path.parents

    def test_the_default_still_matches_what_the_repair_loop_resolves(self):
        """Sandboxing must not split the two subsystems' default lock apart."""
        from openjarvis.core.proclock import production_change_lease

        assert (
            production_change_lease(owner="wiz@target").path
            == production_change_lease(owner="reliability-repair").path
        )
