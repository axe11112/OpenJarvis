"""Claude Code is the only coding engine, and it is never trusted."""

from __future__ import annotations

import inspect

import pytest

from openjarvis.reliability.code_agent import CodeAgentResult
from openjarvis.wiz.features.engineer import (
    PLANNING_TOOLS,
    ClaudeCodeEngineeringAgent,
    CodingEngineUnavailable,
    ContextPack,
)


class _RecordingAgent:
    """A stand-in that records how it was invoked."""

    agent_id = "recording"

    def __init__(self, allowed_tools, *, available=True, result=None):
        self.allowed_tools = list(allowed_tools)
        self._available = available
        self._result = result or CodeAgentResult(claim="did the thing")
        self.tasks = []

    def available(self):
        return self._available

    def run(self, task, *, workspace, timeout=1800):
        self.tasks.append({"task": task, "workspace": workspace, "timeout": timeout})
        return self._result


def _agent(**kwargs):
    made = []

    def factory(*, allowed_tools):
        agent = _RecordingAgent(allowed_tools, **kwargs)
        made.append(agent)
        return agent

    return ClaudeCodeEngineeringAgent(agent_factory=factory), made


def _pack(**kwargs):
    defaults = dict(goal="Add a coach dashboard")
    defaults.update(kwargs)
    return ContextPack(**defaults)


class TestPlanningIsReadOnly:
    def test_a_planning_session_cannot_write(self):
        engineer, made = _agent()
        engineer.plan(_pack(), workspace="/tmp/wt")
        planning = made[-1]
        assert "Edit" not in planning.allowed_tools
        assert "Write" not in planning.allowed_tools

    def test_a_planning_session_has_no_shell(self):
        # Bash is a write primitive; a read-only session that has it is not one.
        assert "Bash" not in PLANNING_TOOLS

    def test_a_planning_session_can_still_investigate(self):
        assert {"Read", "Grep", "Glob"} <= set(PLANNING_TOOLS)

    def test_the_planning_prompt_says_it_is_read_only(self):
        engineer, made = _agent()
        engineer.plan(_pack(), workspace="/tmp/wt")
        assert "READ-ONLY" in made[-1].tasks[0]["task"]


class TestBuilding:
    def test_a_building_session_may_write(self):
        engineer, made = _agent()
        engineer.build(_pack(), workspace="/tmp/wt")
        assert {"Edit", "Write"} <= set(made[-1].allowed_tools)

    def test_the_build_runs_in_the_workspace_it_was_given(self):
        engineer, made = _agent()
        engineer.build(_pack(), workspace="/tmp/feature-worktree")
        assert made[-1].tasks[0]["workspace"] == "/tmp/feature-worktree"

    def test_the_building_prompt_says_the_claim_is_not_evidence(self):
        engineer, made = _agent()
        engineer.build(_pack(), workspace="/tmp/wt")
        task = made[-1].tasks[0]["task"]
        assert "not evidence" in task
        assert "read from git" in task


class TestNoFallback:
    def test_a_missing_cli_stops_the_work(self):
        engineer, _ = _agent(available=False)
        with pytest.raises(CodingEngineUnavailable):
            engineer.build(_pack(), workspace="/tmp/wt")

    def test_a_missing_cli_stops_planning_too(self):
        engineer, _ = _agent(available=False)
        with pytest.raises(CodingEngineUnavailable):
            engineer.plan(_pack(), workspace="/tmp/wt")

    def test_the_error_says_there_is_no_fallback(self):
        engineer, _ = _agent(available=False)
        with pytest.raises(CodingEngineUnavailable) as exc:
            engineer.build(_pack(), workspace="/tmp/wt")
        assert "will not fall back" in str(exc.value)

    def test_the_module_reaches_for_no_other_coding_service(self):
        from openjarvis.wiz.features import engineer as module

        source = inspect.getsource(module).lower()
        for forbidden in ("openai", "gpt-", "gemini", "anthropic.messages", "codex"):
            assert forbidden not in source

    def test_the_engine_is_the_reliability_claude_wrapper(self):
        # Reused rather than reimplemented: a second way of invoking the coding
        # agent would be a second set of guarantees.
        from openjarvis.wiz.features import engineer as module

        assert "ClaudeCliAgent" in inspect.getsource(module)


class TestContextPack:
    def test_the_goal_is_always_present(self):
        assert "Add a coach dashboard" in _pack().render()

    def test_previous_failures_are_included_verbatim(self):
        pack = _pack(
            previous_attempts=[
                {
                    "number": 1,
                    "hypothesis": "the grid is not responsive",
                    "failure": "mobile viewport still overflows at 375px",
                }
            ]
        )
        rendered = pack.render()
        assert "mobile viewport still overflows at 375px" in rendered
        assert "Do not repeat an approach listed here" in rendered

    def test_acceptance_criteria_are_stated_up_front(self):
        rendered = _pack(acceptance=["/coach/dashboard renders"]).render()
        assert "/coach/dashboard renders" in rendered

    def test_protected_paths_are_stated(self):
        rendered = _pack(protected_paths=[".github/workflows/"]).render()
        assert "Do not modify" in rendered
        assert ".github/workflows/" in rendered

    def test_the_repository_is_not_dumped_into_the_prompt(self):
        # §17: the pack carries what Claude cannot discover, not the codebase.
        # A pack with a handful of hints must stay small.
        pack = _pack(
            relevant_paths=[f"src/file{n}.ts" for n in range(5)],
            architecture_notes="Next.js app router.",
        )
        assert len(pack.render()) < 2000

    def test_paths_are_offered_as_starting_points_not_as_the_answer(self):
        rendered = _pack(relevant_paths=["src/app/coach/page.tsx"]).render()
        assert "not an exhaustive list" in rendered


class TestClaimsAreNotEvidence:
    def test_the_session_records_the_claim_separately_from_the_diff(self):
        engineer, made = _agent(
            result=CodeAgentResult(
                claim="I rewrote authentication and it is perfect",
                changed_files=["README.md"],
            )
        )
        session = engineer.build(_pack(), workspace="/tmp/wt")
        # What it said and what it changed are different fields, and the second
        # is what anything downstream is allowed to act on.
        assert session.claim.startswith("I rewrote authentication")
        assert session.changed_files == ["README.md"]
