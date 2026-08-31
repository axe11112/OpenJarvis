"""What a coding session's shell may never do, enforced by the tool itself.

Found on FEAT-00030: a Claude Code *building* session — plan approved, worktree
open, ``Bash`` allowed so it can run the project's own lint/typecheck/test/build
— used that same shell to run ``gh pr create`` and then ``gh pr merge`` against
the real repository, merging its own change into ``main`` outside every gate
this package otherwise enforces (:class:`~openjarvis.wiz.features.shipping.
FeatureShippingPolicy`, the Vercel status gate, canonical browser verification,
:meth:`~openjarvis.wiz.features.pipeline.FeaturePipeline.ship`). Nothing
forced it to: the git commit/push the *pipeline* performs is already
structurally scoped (:meth:`~openjarvis.reliability.workspace.RepairWorkspace.
push` refuses anything not carrying the feature/incident branch prefix), but
that guard lives in code the model never has to go through. Its own shell can
reach ``git`` and ``gh`` directly, authenticated by whatever the *operating
system account* already trusts — the developer's own ``gh auth login``,
stored in the macOS keychain, independent of any environment variable this
package strips (see :meth:`~openjarvis.reliability.code_agent.ClaudeCliAgent.
scrubbed_environment`, which removes JARVIS's *own* tokens and was never the
right tool for an ambient, OS-level credential it does not issue).

Nothing here is a sandbox. It is the two enforcement points Claude Code's own
CLI actually offers — a settings-level deny list matched against each Bash
invocation's leading words, and a ``PreToolUse`` hook that additionally scans
the *whole* command string before it runs — layered because either one alone
has a known gap the other can (partially) cover. Neither survives a
sufficiently adversarial session: a command wrapped as a *string argument* to
an already-permitted interpreter (``python3 -c "..."``, a script written with
``Write`` and then executed) is invisible to both, because Claude Code hands
this hook the Bash tool's literal command text, not the contents of files the
session created. Closing that gap for real means either removing ``Bash``
from building sessions entirely (the pipeline already runs every gate itself
without it — see ``FeaturePipeline._deploy_preview``) or running the session
under an OS principal with no access to the ambient GitHub/Vercel credentials
at all. Both are bigger changes than a settings file; this module is the
strongest available protection against the *actual* incident and its close
variants, not a claim that Bash-with-network-tools has become a hard boundary.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List, Optional, Pattern, Tuple

__all__ = [
    "DANGEROUS_BASH_DENY_PATTERNS",
    "DANGEROUS_COMMAND_SIGNATURES",
    "inspect_command",
    "guard_settings",
    "guard_settings_json",
]

#: Matched by Claude Code itself against each Bash invocation's leading words
#: (``Bash(git push *)`` matches any command starting with ``git push``).
#: Deny rules are evaluated before any allow rule and win outright — see the
#: module docstring on why the boundary sits here rather than only in a
#: system-prompt instruction.
#:
#: What is *not* here is deliberate too: ``git diff``, ``git status``,
#: ``git log``, ``git show`` and plain ``git`` reads stay available, because a
#: building session legitimately inspects its own worktree. What it never
#: legitimately needs — because the pipeline itself performs every one of
#: these, through code with its own structural guards — is anything that
#: publishes, merges, or leaves the worktree: committing, pushing, merging,
#: tagging, the entire ``gh`` CLI, the entire Vercel CLI, and raw HTTP tools
#: that could reach either API directly.
DANGEROUS_BASH_DENY_PATTERNS: Tuple[str, ...] = (
    "Bash(gh *)",
    "Bash(git push *)",
    "Bash(git commit *)",
    "Bash(git merge *)",
    "Bash(git tag *)",
    "Bash(git remote *)",
    "Bash(vercel *)",
    "Bash(curl *)",
    "Bash(wget *)",
)

#: The second, independent layer: a PreToolUse hook receives the *entire*
#: literal Bash command Claude Code is about to run and can refuse it before
#: permission rules are even evaluated. Scanned as substrings against the
#: whole string rather than the leading-words match above, so a command
#: hidden inside ``bash -c "..."`` or chained after a harmless-looking prefix
#: with ``&&``/``;``/``|`` is still caught even where the glob deny rule's
#: prefix match would not reach it — see the module docstring for what this
#: still cannot see (a script written to a file and executed by an
#: interpreter that is itself permitted).
DANGEROUS_COMMAND_SIGNATURES: Tuple[Tuple[Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in (
        (r"\bgh\s+pr\s+merge\b", "gh pr merge — merging a pull request"),
        (r"\bgh\s+pr\s+review\b.*--approve", "gh pr review --approve — self-approval"),
        (r"\bgh\s+api\b", "gh api — direct GitHub API mutation"),
        (r"\bgh\s+pr\s+create\b", "gh pr create — opening a pull request"),
        (r"\bgh\s+repo\s+edit\b", "gh repo edit — repository settings mutation"),
        (r"\bgit\s+push\b", "git push — publishing outside the pipeline's own guarded push"),
        (r"\bgit\s+merge\b", "git merge — merging outside canonical ship()"),
        (r"\bgit\s+commit\b", "git commit — committing outside the pipeline's own commit step"),
        (r"\bgit\s+remote\s+set-url\b", "git remote set-url — redirecting the push target"),
        (
            r"\bvercel\s+(deploy|promote|alias|--prod)\b",
            "vercel deploy/promote/alias — direct production deployment mutation",
        ),
        (
            r"(curl|wget)\b.{0,200}(github\.com|githubusercontent\.com|vercel\.com)",
            "curl/wget targeting github.com or vercel.com — raw API access bypassing gh/vercel",
        ),
        (
            r"\b(bash|sh|zsh)\s+-c\b.{0,300}(gh\s+pr|git\s+push|git\s+merge)",
            "a nested shell invocation containing a blocked GitHub/git operation",
        ),
    )
)


def inspect_command(command: str) -> Optional[str]:
    """The reason *command* is blocked, or ``None`` if it is not.

    Pure and side-effect free so it can be unit tested directly, independent
    of the hook protocol below.
    """
    text = command or ""
    for pattern, reason in DANGEROUS_COMMAND_SIGNATURES:
        if pattern.search(text):
            return reason
    return None


def guard_settings() -> Dict[str, Any]:
    """The settings passed to every ``claude`` invocation this package makes.

    Two layers, both described in the module docstring: a deny list matched
    by Claude Code itself, and a ``PreToolUse`` hook running this same module
    (``python -m openjarvis.reliability.engineer_guard``) for the broader,
    whole-string check. A blocking hook takes precedence over any allow rule,
    so the two layers are additive rather than either one being able to
    silently disable the other.
    """
    return {
        "permissions": {"deny": list(DANGEROUS_BASH_DENY_PATTERNS)},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{sys.executable} -m openjarvis.reliability.engineer_guard",
                        }
                    ],
                }
            ]
        },
    }


def guard_settings_json() -> str:
    """``guard_settings()``, ready for ``claude --settings <json>``."""
    return json.dumps(guard_settings())


def _run_as_hook() -> int:
    """PreToolUse entry point: read the tool call from stdin, block or allow.

    Protocol is Claude Code's own (see the module docstring's link): a JSON
    object on stdin with ``tool_input.command``; exit ``2`` with a reason on
    stderr blocks the call, exit ``0`` lets ordinary permission rules decide.
    Never raises past this function — a hook that crashes must not be able to
    either wedge every Bash call or, worse, fail open silently; any error
    here is treated as a block, with the reason bounded and secret-free.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as exc:  # noqa: BLE001 - see docstring: unreadable input blocks
        sys.stderr.write(f"engineer_guard: could not read the tool call: {exc}\n")
        return 2

    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0

    tool_input = payload.get("tool_input")
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    reason = inspect_command(str(command))
    if reason is None:
        return 0

    sys.stderr.write(
        "engineer_guard: blocked — "
        f"{reason}. Production-changing actions (merging, pushing, deploying) "
        "happen only through the canonical pipeline, never from inside a "
        "coding session's own shell.\n"
    )
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised via inspect_command()
    sys.exit(_run_as_hook())
