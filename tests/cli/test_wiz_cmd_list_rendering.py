"""``jarvis wiz list`` must render a feature in any FeatureState.

Regression for a real crash: ``_STATE_STYLE.get(state, "")`` produced the
Rich markup tag ``[]`` for any state missing from the dict, which Rich parses
as an unrecognised open tag rather than "no style" -- the ``[/]`` that
follows then has nothing to close and raises ``MarkupError``. Found live: a
feature sitting in ``TESTING`` (a state the dict had never covered) took the
whole ``wiz list`` table down.
"""
from __future__ import annotations

from rich.console import Console
from rich.table import Table

from openjarvis.cli.wiz_cmd import _DEFAULT_STYLE, _RISK_STYLE, _STATE_STYLE
from openjarvis.wiz.features.model import FeatureState


def _render_row(state: str, risk: str) -> None:
    table = Table()
    table.add_column("State")
    table.add_column("Risk")
    table.add_row(
        f"[{_STATE_STYLE.get(state, _DEFAULT_STYLE)}]{state}[/]",
        f"[{_RISK_STYLE.get(risk, _DEFAULT_STYLE)}]{risk}[/]",
    )
    console = Console(file=open("/dev/null", "w"))
    console.print(table)


class TestStateStyleCoversEveryFeatureState:
    def test_every_feature_state_has_a_style(self):
        missing = [s.value for s in FeatureState if s.value not in _STATE_STYLE]
        assert not missing, f"_STATE_STYLE is missing: {missing}"

    def test_rendering_a_row_never_raises_for_any_state(self):
        for state in FeatureState:
            _render_row(state.value, "LOW")

    def test_an_unmapped_state_still_renders_instead_of_crashing(self):
        # Simulates a future FeatureState value added to the enum but not yet
        # added to _STATE_STYLE -- the exact shape of the bug this guards.
        _render_row("SOME_FUTURE_STATE", "LOW")

    def test_an_unmapped_risk_still_renders_instead_of_crashing(self):
        _render_row("BUILDING", "SOME_FUTURE_RISK")
