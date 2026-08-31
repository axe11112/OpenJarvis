"""The boundary that stops a coding session from merging its own change.

Found on FEAT-00030: a building session ran ``gh pr create`` then
``gh pr merge`` through its own Bash tool, publishing to the real
``main`` outside every gate the pipeline otherwise enforces. These tests
pin the two-layer defense added afterward — see engineer_guard's module
docstring for what it is and, just as importantly, what it still is not.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from openjarvis.reliability.engineer_guard import (
    DANGEROUS_BASH_DENY_PATTERNS,
    inspect_command,
    guard_settings,
    guard_settings_json,
)


class TestTheExactIncidentIsBlocked:
    def test_gh_pr_merge_is_blocked(self):
        assert inspect_command("gh pr merge 251 --squash") is not None

    def test_gh_pr_create_is_blocked(self):
        assert inspect_command('gh pr create --title x --body y') is not None

    def test_git_push_to_main_is_blocked(self):
        assert inspect_command("git push origin main") is not None

    def test_git_push_to_any_branch_is_blocked(self):
        # Engineer never needs to push at all: the pipeline's own
        # FeatureWorkspace.push() already does it, with its own branch-prefix
        # guard. A building session pushing anything itself is out of scope.
        assert inspect_command("git push origin wiz/feature/x") is not None


class TestOtherProductionMutationsAreBlocked:
    @pytest.mark.parametrize(
        "command",
        [
            "gh api repos/x/y/pulls/1/merge -X PUT",
            "gh pr review 251 --approve",
            "gh repo edit --default-branch main",
            "git merge origin/main",
            "git commit -m 'sneaky'",
            "git remote set-url origin https://evil.example/x.git",
            "vercel --prod",
            "vercel deploy --prod",
            "vercel promote dpl_x",
            "vercel alias set dpl_x wizeperformance.com",
            "curl -X POST https://api.github.com/repos/x/y/pulls/1/merge",
            "wget --post-data=x https://api.github.com/repos/x/y/merges",
        ],
    )
    def test_blocked(self, command):
        assert inspect_command(command) is not None, command


class TestObfuscationIsStillCaught:
    """The deny-pattern layer alone matches only leading words; the hook
    layer scans the whole string, so these still get caught."""

    def test_chained_after_a_harmless_command(self):
        assert inspect_command("echo hi && gh pr merge 251") is not None

    def test_nested_inside_a_shell_dash_c(self):
        assert inspect_command('bash -c "gh pr merge 251"') is not None

    def test_nested_inside_sh_dash_c(self):
        assert inspect_command('sh -c "git push origin main"') is not None


class TestLegitimateWorkStillWorks:
    @pytest.mark.parametrize(
        "command",
        [
            "git diff",
            "git status",
            "git status --short",
            "git log --oneline -5",
            "git show HEAD",
            "npm test",
            "npm run build",
            "npm run lint",
            "npx tsc --noEmit",
            "npx playwright test",
            "echo hello",
            "ls -la",
            "cat package.json",
        ],
    )
    def test_not_blocked(self, command):
        assert inspect_command(command) is None, command


class TestGuardSettingsShape:
    def test_deny_patterns_cover_gh_entirely(self):
        assert "Bash(gh *)" in DANGEROUS_BASH_DENY_PATTERNS

    def test_deny_patterns_cover_git_push(self):
        assert "Bash(git push *)" in DANGEROUS_BASH_DENY_PATTERNS

    def test_settings_has_a_deny_list_and_a_hook(self):
        settings = guard_settings()
        assert settings["permissions"]["deny"] == list(DANGEROUS_BASH_DENY_PATTERNS)
        hooks = settings["hooks"]["PreToolUse"]
        assert hooks[0]["matcher"] == "Bash"
        command = hooks[0]["hooks"][0]["command"]
        assert "openjarvis.reliability.engineer_guard" in command

    def test_settings_json_round_trips(self):
        assert json.loads(guard_settings_json()) == guard_settings()

    def test_git_reads_are_not_in_the_deny_list(self):
        # Pinned explicitly: a future edit that widens "git push *" to a bare
        # "git *" would silently take investigation away from every building
        # session, and nothing else here would notice.
        for pattern in DANGEROUS_BASH_DENY_PATTERNS:
            assert pattern != "Bash(git *)"


class TestTheHookProtocol:
    """Exercises the real subprocess entry point (python -m ...engineer_guard),
    not just inspect_command(), since that is what Claude Code actually runs.
    """

    def _run_hook(self, payload):
        return subprocess.run(
            [sys.executable, "-m", "openjarvis.reliability.engineer_guard"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_a_dangerous_bash_call_exits_2_with_a_reason(self):
        result = self._run_hook(
            {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 251"}}
        )
        assert result.returncode == 2
        assert "gh pr merge" in result.stderr

    def test_a_safe_bash_call_exits_0(self):
        result = self._run_hook(
            {"tool_name": "Bash", "tool_input": {"command": "npm test"}}
        )
        assert result.returncode == 0

    def test_a_non_bash_tool_call_exits_0_without_inspecting_command(self):
        result = self._run_hook(
            {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}
        )
        assert result.returncode == 0

    def test_unreadable_stdin_fails_closed(self):
        result = subprocess.run(
            [sys.executable, "-m", "openjarvis.reliability.engineer_guard"],
            input="not json at all {{{",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2

    def test_the_reason_never_echoes_the_full_raw_command_verbatim_as_a_secret(self):
        # The reason is a fixed, human-authored string keyed off which
        # pattern matched — not an interpolation of the command itself —
        # so a command that happened to carry a secret in its arguments
        # cannot leak it through the block message.
        result = self._run_hook(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "gh pr merge 251 --token ghp_supersecrettoken1234"},
            }
        )
        assert result.returncode == 2
        assert "ghp_supersecrettoken1234" not in result.stderr
