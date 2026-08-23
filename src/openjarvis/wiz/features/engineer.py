"""Claude Code is the only thing that writes code.

Wiz orchestrates: it decides what should be built, assembles the context, runs
the gates, reads the diff and decides whether the result is good enough. It does
not generate source. Every code-changing task in this package goes through the
operator's installed ``claude`` CLI, which is already wrapped — with credential
scrubbing and a constrained tool list — by
:class:`openjarvis.reliability.code_agent.ClaudeCliAgent`. That wrapper is
reused rather than reimplemented; a second, slightly different way of invoking
the coding agent would be a second, slightly different set of guarantees.

Two sessions, deliberately different:

**Planning** runs read-only. ``Read``, ``Grep`` and ``Glob``, and nothing else —
not even ``Bash``, because a shell is a write primitive. The point is to spend a
cheap investigation working out what a change involves before spending an
expensive one making it, and to give Wiz a plan it can check against the
authority and risk rules *before* anything is modified.

**Building** runs write-enabled inside an isolated worktree, and only after the
plan has been checked.

Neither session is trusted. What Claude says it did is recorded as a claim; what
actually changed is read from git. That distinction is already the reliability
subsystem's rule and it does not weaken here just because the work was asked for
rather than discovered.

If the CLI is missing or unauthenticated, feature work stops and says so. There
is no fallback to an API key and no second coding model — §18 of the brief, and
also the only way the operator's subscription stays the single cost of running
this.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from openjarvis.reliability.code_agent import (
    ClaudeCliAgent,
    CodeAgent,
    CodeAgentError,
    CodeAgentResult,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ClaudeCodeEngineeringAgent",
    "ContextPack",
    "EngineeringSession",
    "PLANNING_TOOLS",
    "BUILDING_TOOLS",
]

#: A planning session may look and may not touch. ``Bash`` is absent on purpose:
#: it is the tool that turns a read-only session into a write-enabled one.
PLANNING_TOOLS: Sequence[str] = ("Read", "Grep", "Glob")

#: A building session works in an isolated worktree. ``WebFetch`` and
#: ``WebSearch`` remain refused by the underlying wrapper, so nothing the agent
#: reads in the repository can talk it into fetching an attacker-controlled URL.
BUILDING_TOOLS: Sequence[str] = ("Read", "Edit", "Write", "Grep", "Glob", "Bash")


class CodingEngineUnavailable(CodeAgentError):
    """The Claude CLI cannot be run, so no code will be written.

    Its own exception type because the correct handling is specific: pause the
    feature, tell the operator what is wrong with their CLI, and do not look for
    another way to generate the code.
    """


@dataclass(slots=True)
class ContextPack:
    """What Claude is told, and nothing more.

    §17 of the brief: do not dump the repository into every request. Claude Code
    can read the repository itself — that is the whole advantage of using it
    rather than an API — so the pack carries the things it *cannot* discover: what
    the operator wanted, what has already been tried, and exactly how the last
    attempt failed.
    """

    goal: str
    repository: str = ""
    base_sha: str = ""
    branch: str = ""

    #: Where to start looking. Hints, not an exhaustive list.
    relevant_paths: List[str] = field(default_factory=list)

    architecture_notes: str = ""

    #: The gates that will judge the work. Told up front so the agent can aim at
    #: them rather than discovering them by failing.
    gates: List[str] = field(default_factory=list)

    #: Machine-checkable criteria the change must satisfy.
    acceptance: List[str] = field(default_factory=list)

    #: What must not be touched.
    protected_paths: List[str] = field(default_factory=list)

    #: Previous attempts, most recent last: what was tried and what went wrong.
    previous_attempts: List[Dict[str, Any]] = field(default_factory=list)

    def render(self) -> str:
        """The brief, as prose the agent reads."""
        lines: List[str] = []

        lines.append("# Goal")
        lines.append(self.goal.strip() or "(not stated)")

        if self.repository or self.base_sha or self.branch:
            lines.append("\n# Repository")
            if self.repository:
                lines.append(f"- repository: {self.repository}")
            if self.branch:
                lines.append(f"- branch: {self.branch}")
            if self.base_sha:
                lines.append(f"- base commit: {self.base_sha}")

        if self.architecture_notes.strip():
            lines.append("\n# Context")
            lines.append(self.architecture_notes.strip())

        if self.relevant_paths:
            lines.append("\n# Where to start looking")
            lines.append(
                "These are starting points, not an exhaustive list. "
                "Investigate the repository yourself."
            )
            lines.extend(f"- {p}" for p in self.relevant_paths)

        if self.acceptance:
            lines.append("\n# This change is only correct if")
            lines.extend(f"- {a}" for a in self.acceptance)

        if self.gates:
            lines.append("\n# It will be checked with")
            lines.extend(f"- {g}" for g in self.gates)

        if self.protected_paths:
            lines.append("\n# Do not modify")
            lines.extend(f"- {p}" for p in self.protected_paths)

        if self.previous_attempts:
            lines.append("\n# What has already been tried")
            lines.append(
                "Do not repeat an approach listed here. If the same fix looks "
                "right again, the diagnosis is probably wrong."
            )
            for attempt in self.previous_attempts:
                number = attempt.get("number", "?")
                lines.append(f"\n## Attempt {number}")
                if attempt.get("hypothesis"):
                    lines.append(f"Believed: {attempt['hypothesis']}")
                if attempt.get("claim"):
                    lines.append(f"Claude reported: {attempt['claim']}")
                if attempt.get("failure"):
                    lines.append(f"Failed because: {attempt['failure']}")

        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class EngineeringSession:
    """What came back from one Claude session."""

    mode: str  # "plan" or "build"
    succeeded: bool

    #: Claude's own account. A claim, not evidence.
    claim: str = ""

    error: str = ""

    #: Read from git afterwards, not from the agent.
    changed_files: List[str] = field(default_factory=list)
    diff_stat: str = ""

    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_result(cls, mode: str, result: CodeAgentResult) -> "EngineeringSession":
        return cls(
            mode=mode,
            succeeded=result.succeeded,
            claim=result.claim,
            error=result.error,
            changed_files=list(result.changed_files),
            diff_stat=result.diff_stat,
            metadata=dict(result.metadata),
        )


class ClaudeCodeEngineeringAgent:
    """Wiz's interface to the coding engine.

    Wraps :class:`ClaudeCliAgent` rather than replacing it, so the credential
    scrubbing, the refused tools and the "read the diff from git" discipline are
    the same code the repair loop already relies on.
    """

    def __init__(
        self,
        *,
        executable: str = "claude",
        agent_factory: Optional[Any] = None,
        planning_timeout: int = 900,
        building_timeout: int = 2700,
    ) -> None:
        self._executable = executable
        self._agent_factory = agent_factory or self._default_factory
        self._planning_timeout = planning_timeout
        self._building_timeout = building_timeout

    def _default_factory(self, *, allowed_tools: Sequence[str]) -> CodeAgent:
        return ClaudeCliAgent(
            executable=self._executable, allowed_tools=list(allowed_tools)
        )

    # -- availability ------------------------------------------------------

    def available(self) -> bool:
        """Whether the CLI is present.

        Checked before a feature is admitted rather than discovered halfway
        through, so that "I cannot write code right now" is something Wiz says
        before opening a worktree.
        """
        agent = self._agent_factory(allowed_tools=PLANNING_TOOLS)
        probe = getattr(agent, "available", None)
        if callable(probe):
            try:
                return bool(probe())
            except Exception:
                return False
        # An injected agent with no availability probe is a test double, and a
        # test double is available by definition.
        return True

    def _require_available(self) -> None:
        if not self.available():
            raise CodingEngineUnavailable(
                f"the '{self._executable}' CLI is not available, so no code can "
                "be written. Wiz will not fall back to another coding model."
            )

    # -- sessions ----------------------------------------------------------

    def plan(self, pack: ContextPack, *, workspace: str) -> EngineeringSession:
        """Investigate, and produce a plan. Changes nothing."""
        self._require_available()
        agent = self._agent_factory(allowed_tools=PLANNING_TOOLS)
        task = self._planning_prompt(pack)
        result = agent.run(task, workspace=workspace, timeout=self._planning_timeout)
        return EngineeringSession.from_result("plan", result)

    def build(self, pack: ContextPack, *, workspace: str) -> EngineeringSession:
        """Implement the change in *workspace*, which must be an isolated worktree."""
        self._require_available()
        agent = self._agent_factory(allowed_tools=BUILDING_TOOLS)
        task = self._building_prompt(pack)
        result = agent.run(task, workspace=workspace, timeout=self._building_timeout)
        return EngineeringSession.from_result("build", result)

    # -- prompts -----------------------------------------------------------

    @staticmethod
    def _planning_prompt(pack: ContextPack) -> str:
        return (
            "You are investigating a repository to plan a change. "
            "This session is READ-ONLY: do not modify any file.\n\n"
            f"{pack.render()}\n\n"
            "# What to produce\n"
            "A plan covering:\n"
            "- what already exists that is relevant, with file paths\n"
            "- the approach you would take, and why\n"
            "- the files you expect to change\n"
            "- the tests that should prove it works\n"
            "- database, migration or schema implications, if any\n"
            "- anything that makes this riskier than it looks\n\n"
            "Investigate before answering. If the request is ambiguous in a way "
            "that changes what should be built, say so instead of choosing.\n\n"
            "# Acceptance criteria (optional, additive)\n"
            "A generic contract is derived from the request automatically — it "
            "cannot know what you now know from reading the repository. If you "
            "can name something more specific worth checking (real button "
            "text, a route only visible after investigating, a specific "
            "interaction and what should happen after it, an API endpoint and "
            "the status it should return), append ONE fenced block:\n\n"
            "```acceptance-criteria\n"
            "[\n"
            '  {"kind": "CONTENT", "route": "/path", "text": "exact text", '
            '"description": "what this proves"},\n'
            '  {"kind": "INTERACTION", "route": "/path", "selector": '
            '"button[name=save]", "then_text": "Saved", "description": '
            '"what this proves"}\n'
            "]\n"
            "```\n\n"
            "Valid kinds: CONTENT, INTERACTION, VIEWPORT, CONSOLE, NETWORK, "
            "ENDPOINT, UNAUTHORIZED. This is additive only — it can make the "
            "bar higher, never lower one already set. Omit the block entirely "
            "rather than propose something vague; nothing here is required."
        )

    @staticmethod
    def _building_prompt(pack: ContextPack) -> str:
        return (
            "Implement the following change in this worktree.\n\n"
            f"{pack.render()}\n\n"
            "# How this will be judged\n"
            "Your description of what you did is recorded but is not evidence. "
            "The diff is read from git and the checks above are run against it. "
            "Write the tests that prove the change works.\n\n"
            "Stay within the scope described. If doing this properly requires "
            "changing something outside that scope, stop and explain rather "
            "than widening the change."
        )
