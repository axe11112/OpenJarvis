"""The coding engine adapter.

JARVIS is the monitor, orchestrator and verifier; the coding agent is the
engineer.  This module is the seam between them, and it is deliberately thin —
everything JARVIS knows about the agent is: give it a workspace and a task, get
back a claim and a set of changed files.

Two implementations:

* :class:`ClaudeCliAgent` drives the ``claude`` CLI in headless mode.  This is
  the recommended path: it reuses whatever Claude Code entitlement the owner
  already has, needs no bundled Node runner, and takes permission flags
  directly.  (The framework's ``ClaudeCodeAgent`` wraps a bundled Node runner
  that ships no compiled ``dist/`` — see ``docs/JARVIS_ARCHITECTURE.md`` §7.)
* :class:`FakeCodeAgent` is used by the tests to drive the whole repair loop
  deterministically, including the case that matters most: an agent that
  confidently claims success while the fix does not work.

Whatever the implementation, its ``claim`` carries no authority.  Only
independent verification decides whether a repair worked.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "CodeAgent",
    "CodeAgentError",
    "CodeAgentResult",
    "ClaudeCliAgent",
    "FakeCodeAgent",
    "TRANSIENT_CAPACITY_PATTERN",
    "changed_files",
    "diff_stat",
]


class CodeAgentError(RuntimeError):
    """Raised when the coding agent cannot be run at all."""


#: Claude Code being out of capacity for now, not the session failing to
#: understand or accomplish the task. Matched across stdout *and* stderr: in
#: ``--output-format text`` mode this message is the CLI's own final assistant
#: text and lands on stdout, so a check that only looked at stderr (or only at
#: the exit code) would silently turn a known, transient capacity limit into
#: an opaque "exit code 1" — which is exactly what happened to FEAT-00017.
TRANSIENT_CAPACITY_PATTERN = re.compile(
    r"(session|usage) limit|rate limit exceeded", re.IGNORECASE
)


def _transient_capacity_message(stdout: str, stderr: str) -> Optional[str]:
    """The line describing a known transient-capacity failure, if present.

    stderr is checked first: if the CLI ever does put this on stderr, that is
    the more deliberate signal. Falls back to stdout for the headless
    text-output case where it is the last thing Claude said, not an error
    stream write.
    """
    for text in (stderr, stdout):
        for line in text.splitlines():
            if TRANSIENT_CAPACITY_PATTERN.search(line):
                return line.strip()
    return None


@dataclass(slots=True)
class CodeAgentResult:
    """What the coding agent reported.

    ``claim`` is the agent's own account of what it did.  It is recorded as an
    assertion and never treated as evidence.
    """

    claim: str = ""
    succeeded: bool = True  # the process ran, not that the fix works
    error: str = ""
    changed_files: List[str] = field(default_factory=list)
    diff_stat: str = ""
    metadata: dict = field(default_factory=dict)


class CodeAgent(ABC):
    """Runs a coding task against a workspace."""

    agent_id: str

    @abstractmethod
    def run(self, task: str, *, workspace: str, timeout: int = 1800) -> CodeAgentResult:
        """Execute *task* in *workspace* and report what happened."""


# ---------------------------------------------------------------------------
# Git helpers — how JARVIS learns what actually changed
# ---------------------------------------------------------------------------


def _git(args: Sequence[str], *, cwd: str, timeout: int = 60) -> str:
    """Run a git command, returning stdout (empty string on failure)."""
    if shutil.which("git") is None:
        raise CodeAgentError("git is not on PATH")
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("git %s timed out in %s", " ".join(args), cwd)
        return ""
    if proc.returncode != 0:
        logger.debug("git %s failed: %s", " ".join(args), proc.stderr.strip())
        return ""
    return proc.stdout


def changed_files(workspace: str) -> List[str]:
    """Return files modified in *workspace* relative to HEAD.

    Read from git rather than from the agent's own account, because the agent's
    account is exactly the thing JARVIS does not trust.
    """
    out = _git(["status", "--porcelain"], cwd=workspace)
    paths: List[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        # Renames appear as "old -> new"; the new path is what changed.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"'))
    return sorted(set(paths))


def diff_stat(workspace: str) -> str:
    """Return a compact diffstat for the working tree."""
    return _git(["diff", "--stat", "HEAD"], cwd=workspace).strip()


# ---------------------------------------------------------------------------
# Claude CLI
# ---------------------------------------------------------------------------


class ClaudeCliAgent(CodeAgent):
    """Drives the ``claude`` CLI in headless mode.

    Parameters
    ----------
    executable:
        CLI binary name or path.
    allowed_tools:
        Tools the agent may use.  Constrained by JARVIS, not by the agent.
    disallowed_tools:
        Explicitly refused tools.  ``WebFetch`` is refused by default so the
        agent cannot be talked into fetching an attacker-controlled URL by
        content it read in the evidence.
    extra_args:
        Escape hatch for CLI flags this wrapper does not model.
    """

    agent_id = "claude_cli"

    DEFAULT_ALLOWED_TOOLS = ("Read", "Edit", "Write", "Grep", "Glob", "Bash")
    DEFAULT_DISALLOWED_TOOLS = ("WebFetch", "WebSearch")

    #: Environment variables never passed through to the agent.  The repair
    #: workspace is a checkout of the *target* application; the agent has no
    #: reason to hold JARVIS's own read tokens, and a subprocess that cannot see
    #: a credential cannot leak it.  Matched as substrings, case-insensitively.
    STRIPPED_ENV_PARTS = (
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "API_KEY",
        "APIKEY",
        "CREDENTIAL",
        "PRIVATE_KEY",
        "SUPABASE",
        "VERCEL",
        "TELEGRAM",
    )

    #: Kept even though they match the list above: without them the CLI cannot
    #: authenticate, and the whole agent is inert.
    #:
    #: The descriptor variants are not hypothetical. Claude Code authenticates
    #: through ``CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`` on managed hosts, and
    #: an allowlist naming only ``CLAUDE_CODE_OAUTH_TOKEN`` silently stripped it
    #: — which would have left the agent unauthenticated on exactly the machines
    #: it is meant to run on. Found by running the real CLI, not by reading.
    KEEP_ENV = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    )

    #: Session plumbing that is deliberately *not* kept. A repair agent should
    #: get its own session rather than inheriting the identity, socket and
    #: message bus of whatever launched JARVIS.
    DROPPED_SESSION_ENV = (
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_SESSION_INGRESS_TOKEN_FILE",
    )

    def __init__(
        self,
        *,
        executable: str = "claude",
        allowed_tools: Optional[Sequence[str]] = None,
        disallowed_tools: Optional[Sequence[str]] = None,
        extra_args: Optional[Sequence[str]] = None,
        runner: Optional[Callable[..., Any]] = None,
        strip_environment: bool = True,
    ) -> None:
        self._executable = executable
        self._allowed = list(
            allowed_tools if allowed_tools is not None else self.DEFAULT_ALLOWED_TOOLS
        )
        self._disallowed = list(
            disallowed_tools
            if disallowed_tools is not None
            else self.DEFAULT_DISALLOWED_TOOLS
        )
        self._extra_args = list(extra_args or [])
        self._runner = runner or subprocess.run
        self._strip_environment = strip_environment

    @classmethod
    def scrubbed_environment(cls, source: Optional[dict] = None) -> dict:
        """Return *source* without JARVIS's own credentials.

        The agent inherits PATH, HOME and everything the target project needs to
        build, but not the tokens JARVIS uses to read GitHub, Vercel or Supabase.
        """
        import os

        env = dict(os.environ if source is None else source)
        for name in list(env):
            if name in cls.KEEP_ENV:
                continue
            upper = name.upper()
            if any(part in upper for part in cls.STRIPPED_ENV_PARTS):
                env.pop(name, None)
        return env

    def available(self) -> bool:
        """Whether the CLI can be found on PATH."""
        return shutil.which(self._executable) is not None

    def run(self, task: str, *, workspace: str, timeout: int = 1800) -> CodeAgentResult:
        """Run the task, then read the resulting diff from git."""
        if not Path(workspace).is_dir():
            raise CodeAgentError(f"workspace does not exist: {workspace}")
        if not self.available():
            raise CodeAgentError(
                f"'{self._executable}' is not on PATH. Install Claude Code, or "
                "configure a different coding agent."
            )

        command = [self._executable, "-p", task, "--output-format", "text"]
        if self._allowed:
            command += ["--allowedTools", ",".join(self._allowed)]
        if self._disallowed:
            command += ["--disallowedTools", ",".join(self._disallowed)]
        command += self._extra_args

        # The task text is passed as an argv element, never through a shell, so
        # evidence content cannot become a command however it is punctuated.
        try:
            proc = self._runner(
                command,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=(self.scrubbed_environment() if self._strip_environment else None),
            )
        except subprocess.TimeoutExpired:
            return CodeAgentResult(
                succeeded=False,
                error=f"the coding agent timed out after {timeout}s",
                changed_files=changed_files(workspace),
                diff_stat=diff_stat(workspace),
            )
        except OSError as exc:
            raise CodeAgentError(f"could not start the coding agent: {exc}") from exc

        files = changed_files(workspace)
        stat = diff_stat(workspace)
        returncode = getattr(proc, "returncode", 1)
        stdout = getattr(proc, "stdout", "") or ""
        stderr = getattr(proc, "stderr", "") or ""

        if returncode != 0:
            transient = _transient_capacity_message(stdout, stderr)
            metadata: dict = {}
            if transient is not None:
                error_text = transient
                metadata["transient_reason"] = "usage_limit"
            else:
                error_text = stderr.strip() or f"exit code {returncode}"
            return CodeAgentResult(
                claim=stdout.strip(),
                succeeded=False,
                error=error_text[:2000],
                changed_files=files,
                diff_stat=stat,
                metadata=metadata,
            )

        return CodeAgentResult(
            claim=stdout.strip(),
            succeeded=True,
            changed_files=files,
            diff_stat=stat,
            metadata={"returncode": returncode},
        )


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


class FakeCodeAgent(CodeAgent):
    """A scripted coding agent, for testing the repair loop end to end.

    Parameters
    ----------
    scripts:
        One entry per attempt.  Each is a ``CodeAgentResult`` returned in order;
        the last entry repeats once exhausted.
    on_run:
        Optional side effect (e.g. writing a file into the workspace).
    """

    agent_id = "fake"

    def __init__(
        self,
        scripts: Optional[Sequence[CodeAgentResult]] = None,
        *,
        on_run: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self._scripts = list(scripts or [CodeAgentResult(claim="Fixed it.")])
        self._on_run = on_run
        self.calls: List[str] = []

    def run(self, task: str, *, workspace: str, timeout: int = 1800) -> CodeAgentResult:
        """Return the next scripted result."""
        index = len(self.calls)
        self.calls.append(task)
        if self._on_run is not None:
            self._on_run(workspace, index)
        script = self._scripts[min(index, len(self._scripts) - 1)]
        # Return a copy so the caller cannot mutate the script.
        return CodeAgentResult(
            claim=script.claim,
            succeeded=script.succeeded,
            error=script.error,
            changed_files=list(script.changed_files),
            diff_stat=script.diff_stat,
            metadata=dict(script.metadata),
        )
