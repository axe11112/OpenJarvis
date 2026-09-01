"""Local verification gates: tests, lint, type checking, build.

These run inside the isolated worktree after the coding agent has finished and
before anything leaves the machine. They are *not* the independent verification
that decides whether a repair worked — that is :mod:`openjarvis.reliability.verify`,
which re-runs the original reproduction against a preview deployment. These are
the cheap checks that catch a broken repair before it costs a deployment.

A check that is not configured is reported as **not run**, never as passed. The
distinction matters: "we have no type checker" and "the types are fine" are
different facts, and only one of them justifies opening a pull request.

Failures are captured as text so they can be fed back to the agent on the next
attempt. That feedback loop is what makes attempt two different from attempt one.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "CheckCommand",
    "CheckResult",
    "CheckSuite",
    "CheckSuiteResult",
    "run_check",
]

#: Keep the tail: compilers, test runners and bundlers all put the reason for
#: the failure at the end of their output.
_MAX_OUTPUT = 8000


@dataclass(slots=True)
class CheckCommand:
    """One command to run, and how seriously to take its failure."""

    name: str
    command: str
    #: A failing *required* check stops the attempt. A failing advisory check is
    #: recorded and reported but does not by itself block a pull request —
    #: a lint rule should not veto an outage fix.
    required: bool = True
    timeout: int = 1800
    #: Directories prepended to PATH before running *command*, earliest wins.
    #: Exists for one reason: a project can pin a runtime version
    #: (``package.json``'s ``engines.node``, ``.nvmrc``) that the machine
    #: running Wiz does not default to, and a check run under the wrong
    #: runtime is not evidence about the change — it is evidence about the
    #: machine. Empty by default, meaning "whatever this process already has".
    path_prepend: List[str] = field(default_factory=list)
    #: Extra environment variables, applied on top of this process's own
    #: environment. Found on FEAT-00031: a production ``next build`` can
    #: genuinely need more V8 heap than Node's default ceiling on a given
    #: machine, and a build killed by that is evidence about the machine's
    #: memory, not about the change — the same distinction ``path_prepend``
    #: already draws for the runtime version. Empty by default.
    env: Dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class CheckResult:
    """What one check did."""

    name: str
    ran: bool
    passed: bool
    summary: str = ""
    output: str = ""
    required: bool = True
    duration_seconds: float = 0.0

    @property
    def blocking_failure(self) -> bool:
        """Whether this result should stop the repair attempt."""
        return self.ran and not self.passed and self.required

    @property
    def icon(self) -> str:
        """Symbol for human-facing summaries."""
        if not self.ran:
            return "⚪"
        return "🟢" if self.passed else ("🔴" if self.required else "🟡")

    def to_dict(self) -> Dict[str, object]:
        """Serialize for the incident record. Output is deliberately excluded.

        The full output can be megabytes and can contain application data; it
        travels back to the agent in the retry brief (redacted) and is attached
        as evidence, but it does not belong in the attempt row.
        """
        return {
            "name": self.name,
            "ran": self.ran,
            "passed": self.passed,
            "summary": self.summary,
            "required": self.required,
            "duration_seconds": round(self.duration_seconds, 2),
        }


@dataclass(slots=True)
class CheckSuiteResult:
    """The outcome of every configured check."""

    results: List[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True when no required check failed.

        A suite where nothing ran passes vacuously — that is intentional and
        safe here, because a repair still cannot reach RESOLVED without
        independent verification. It is reported honestly by :attr:`ran_any`.
        """
        return not any(r.blocking_failure for r in self.results)

    @property
    def ran_any(self) -> bool:
        """Whether any check actually executed."""
        return any(r.ran for r in self.results)

    @property
    def failures(self) -> List[CheckResult]:
        """Required checks that failed."""
        return [r for r in self.results if r.blocking_failure]

    @property
    def summary(self) -> str:
        """One line per check, for notifications and the pull-request body."""
        if not self.results:
            return "no checks configured"
        return "\n".join(
            f"{r.icon} {r.name}: {r.summary or ('passed' if r.passed else 'failed')}"
            for r in self.results
        )

    def feedback(self, *, max_chars: int = 6000) -> str:
        """Render failures as evidence for the coding agent's next attempt."""
        blocks: List[str] = []
        for result in self.failures:
            body = (result.output or result.summary).strip()
            blocks.append(f"### {result.name} failed\n\n{body}")
        joined = "\n\n".join(blocks)
        return joined[:max_chars]

    def to_dict(self) -> Dict[str, object]:
        """Serialize for the incident record."""
        return {
            "passed": self.passed,
            "ran_any": self.ran_any,
            "results": [r.to_dict() for r in self.results],
        }


def run_check(check: CheckCommand, *, workspace: str) -> CheckResult:
    """Run one check in *workspace*.

    A command that is empty, or that cannot be started at all, is reported as
    "did not run" rather than as a failure: a missing type checker is not a type
    error, and conflating the two would either block every repair or hide real
    breakage.
    """
    import os
    import time

    if not check.command.strip():
        return CheckResult(
            name=check.name,
            ran=False,
            passed=False,
            summary="not configured",
            required=check.required,
        )

    env = None
    if check.path_prepend or check.env:
        env = dict(os.environ)
        if check.path_prepend:
            env["PATH"] = os.pathsep.join([*check.path_prepend, env.get("PATH", "")])
        env.update(check.env)

    started = time.monotonic()
    try:
        proc = subprocess.run(
            check.command,
            shell=True,  # noqa: S602 - operator-configured command, not model output
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=check.timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=check.name,
            ran=True,
            passed=False,
            summary=f"timed out after {check.timeout}s",
            required=check.required,
            duration_seconds=time.monotonic() - started,
        )
    except OSError as exc:
        return CheckResult(
            name=check.name,
            ran=False,
            passed=False,
            summary=f"could not run: {exc}",
            required=check.required,
            duration_seconds=time.monotonic() - started,
        )

    duration = time.monotonic() - started
    output = ((proc.stdout or "") + (proc.stderr or ""))[-_MAX_OUTPUT:]
    passed = proc.returncode == 0
    return CheckResult(
        name=check.name,
        ran=True,
        passed=passed,
        summary="passed" if passed else f"failed (exit {proc.returncode})",
        output="" if passed else output,
        required=check.required,
        duration_seconds=duration,
    )


@dataclass
class CheckSuite:
    """An ordered set of local gates.

    Ordered cheapest-first so a syntax error surfaces in seconds rather than
    after a full build — and so the feedback the agent gets names the most
    fundamental problem rather than its downstream consequences.
    """

    checks: List[CheckCommand] = field(default_factory=list)

    @classmethod
    def from_config(
        cls,
        *,
        test_command: str = "",
        lint_command: str = "",
        typecheck_command: str = "",
        build_command: str = "",
        timeout: int = 1800,
        path_prepend: Optional[List[str]] = None,
        build_env: Optional[Dict[str, str]] = None,
    ) -> "CheckSuite":
        """Build the standard suite from configured commands.

        Lint is advisory: a style violation is a poor reason to leave production
        broken. Tests, types and the build are required, because each of them
        failing means the change is not shippable at all.

        *path_prepend* applies to every check equally — a project either needs
        a pinned runtime for all four gates or none of them, and letting them
        disagree would make "which Node ran the tests" a question with more
        than one honest answer.

        *build_env* applies to the build check alone, unlike *path_prepend* —
        unlike a runtime version, an environment fix like extra V8 heap is
        something only the heaviest of the four gates has ever been observed
        to need; forcing it onto lint/typecheck/tests as well would only make
        their behaviour harder to reason about for no benefit.
        """
        prepend = list(path_prepend or [])
        return cls(
            checks=[
                CheckCommand(
                    "lint",
                    lint_command,
                    required=False,
                    timeout=timeout,
                    path_prepend=prepend,
                ),
                CheckCommand(
                    "typecheck", typecheck_command, timeout=timeout, path_prepend=prepend
                ),
                CheckCommand(
                    "tests", test_command, timeout=timeout, path_prepend=prepend
                ),
                CheckCommand(
                    "build",
                    build_command,
                    timeout=timeout,
                    path_prepend=prepend,
                    env=dict(build_env or {}),
                ),
            ]
        )

    def run(self, *, workspace: str, stop_early: bool = True) -> CheckSuiteResult:
        """Run every configured check in *workspace*.

        With *stop_early*, a required failure ends the run: there is no value in
        spending three minutes on a production build when the tests already
        said the change is wrong.
        """
        results: List[CheckResult] = []
        for check in self.checks:
            result = run_check(check, workspace=workspace)
            results.append(result)
            if stop_early and result.blocking_failure:
                logger.info(
                    "check '%s' failed; skipping the remaining checks", check.name
                )
                # Record the skipped checks honestly rather than omitting them.
                for skipped in self.checks[len(results) :]:
                    results.append(
                        CheckResult(
                            name=skipped.name,
                            ran=False,
                            passed=False,
                            summary="skipped after an earlier failure",
                            required=skipped.required,
                        )
                    )
                break
        return CheckSuiteResult(results=results)

    @property
    def configured_names(self) -> List[str]:
        """Names of checks that have a command to run."""
        return [c.name for c in self.checks if c.command.strip()]


def summarize_for_owner(result: Optional[CheckSuiteResult]) -> str:
    """Render a suite result for a Telegram message."""
    if result is None:
        return "no local checks were run"
    return result.summary
