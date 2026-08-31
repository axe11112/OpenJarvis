"""Security boundaries of the repair loop (§23 of the Phase 12 brief).

Each test here names a way the loop could be turned against the system it is
meant to protect, and pins the behaviour that prevents it.
"""

from __future__ import annotations

import shutil

import pytest

from openjarvis.reliability.briefing import redact_secrets
from openjarvis.reliability.code_agent import (
    ClaudeCliAgent,
    CodeAgentResult,
    FakeCodeAgent,
)
from openjarvis.reliability.policy import SafetyPolicy
from openjarvis.reliability.probes.spec import parse_probe
from openjarvis.reliability.repair import RepairLoop
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import Incident, ProbeResult, Severity
from openjarvis.reliability.verify import Verifier
from openjarvis.reliability.workspace import RepairWorkspace, WorkspaceError, Worktree
from tests.reliability import fixture_repo


def _spec():
    return parse_probe(
        {
            "probe": {
                "id": "pricing-discount",
                "component": "pricing",
                "severity": "high",
                "steps": [{"action": "goto", "url": "/checkout"}],
                "expect": [{"kind": "text", "value": "180"}],
            }
        }
    )


@pytest.fixture
def store(tmp_path):
    s = IncidentStore(tmp_path / "incidents.db")
    yield s
    s.close()


@pytest.fixture
def incident(store):
    return store.create(
        Incident(
            fingerprint="fp_x",
            severity=Severity.HIGH,
            component="pricing",
            title="Discounts are wrong",
        )
    )


class _PassingExecutor:
    def run(self, spec):
        return ProbeResult(probe_id=spec.id, success=True, steps_completed=1)


# ---------------------------------------------------------------------------
# Secrets in agent output
# ---------------------------------------------------------------------------


SECRET = "ghp_" + "z" * 36


class TestSecretsInAgentOutput:
    """The agent has just read the application's source; it can echo a secret."""

    @pytest.mark.parametrize(
        "text",
        [
            f"I found the token {SECRET} hardcoded and moved it.",
            "The config had DB_PASSWORD=hunter2 in it.",
            'Set {"api_key": "sk-abcdefghijklmnop"} in the client.',
        ],
    )
    def test_redaction_catches_the_shape_and_the_assignment(self, text):
        cleaned = redact_secrets(text)
        assert SECRET not in cleaned
        assert "hunter2" not in cleaned
        assert "sk-abcdefghijklmnop" not in cleaned

    def test_the_claim_is_redacted_before_it_is_persisted(self, store, incident):
        agent = FakeCodeAgent(
            [
                CodeAgentResult(
                    claim=f"Removed the hardcoded token {SECRET} from config.ts.",
                    changed_files=["config.ts"],
                )
            ]
        )
        loop = RepairLoop(
            agent=agent,
            policy=SafetyPolicy(repair_enabled=True),
            verifier=Verifier(executor_factory=lambda _u: _PassingExecutor()),
            store=store,
            workspace=".",
            preview_lookup=lambda _b: "https://preview.example",
            sleep=lambda _s: None,
        )
        loop.run(incident, _spec())

        assert SECRET not in incident.attempts[0].claim
        assert "[REDACTED" in incident.attempts[0].claim or "***" in (
            incident.attempts[0].claim
        )

    def test_a_redacted_claim_does_not_reach_the_pull_request(self, store, incident):
        created = []

        class _GitHub:
            base_branch = "main"

            def branch_name_for(self, incident_id):
                return f"jarvis/incident-{incident_id}"

            def create_branch(self, branch, **kwargs):
                return "sha"

            def create_pull_request(self, **kwargs):
                created.append(kwargs)
                return {"number": 1, "url": "u"}

        agent = FakeCodeAgent(
            [CodeAgentResult(claim=f"token {SECRET}", changed_files=["a.ts"])]
        )
        loop = RepairLoop(
            agent=agent,
            policy=SafetyPolicy(repair_enabled=True),
            verifier=Verifier(executor_factory=lambda _u: _PassingExecutor()),
            store=store,
            workspace=".",
            github=_GitHub(),
            preview_lookup=lambda _b: "https://preview.example",
            sleep=lambda _s: None,
        )
        loop.run(incident, _spec())

        assert created
        assert SECRET not in created[0]["body"]

    def test_agent_errors_are_redacted_too(self, store, incident):
        agent = FakeCodeAgent(
            [CodeAgentResult(succeeded=False, error=f"auth failed with {SECRET}")]
        )
        loop = RepairLoop(
            agent=agent,
            policy=SafetyPolicy(repair_enabled=True, max_attempts=1),
            verifier=Verifier(executor_factory=lambda _u: _PassingExecutor()),
            store=store,
            workspace=".",
            sleep=lambda _s: None,
        )
        loop.run(incident, _spec())

        assert SECRET not in incident.attempts[0].test_summary


class TestAgentEnvironment:
    """The agent should not be handed JARVIS's own credentials."""

    def test_jarvis_tokens_are_stripped(self):
        env = ClaudeCliAgent.scrubbed_environment(
            {
                "PATH": "/usr/bin",
                "GITHUB_READONLY_TOKEN": "ghp_x",
                "SUPABASE_READONLY_TOKEN": "sb_x",
                "VERCEL_READONLY_TOKEN": "v_x",
                "TELEGRAM_BOT_TOKEN": "t_x",
                "JARVIS_TEST_PASSWORD": "hunter2",
            }
        )
        assert env["PATH"] == "/usr/bin"
        assert "GITHUB_READONLY_TOKEN" not in env
        assert "SUPABASE_READONLY_TOKEN" not in env
        assert "VERCEL_READONLY_TOKEN" not in env
        assert "TELEGRAM_BOT_TOKEN" not in env
        assert "JARVIS_TEST_PASSWORD" not in env

    def test_the_agents_own_credential_survives(self):
        """Stripping it would leave the agent unable to authenticate at all."""
        env = ClaudeCliAgent.scrubbed_environment(
            {"ANTHROPIC_API_KEY": "sk-x", "OTHER_TOKEN": "y"}
        )
        assert env["ANTHROPIC_API_KEY"] == "sk-x"
        assert "OTHER_TOKEN" not in env

    def test_build_variables_are_left_alone(self):
        env = ClaudeCliAgent.scrubbed_environment(
            {"NODE_ENV": "test", "HOME": "/home/x", "CI": "1"}
        )
        assert env["NODE_ENV"] == "test"
        assert env["HOME"] == "/home/x"
        assert env["CI"] == "1"

    def test_gh_cannot_find_the_operators_own_ambient_login(self):
        """FEAT-00030: gh authenticates from the operator's own `gh auth
        login`, stored in the keychain and resolved via $GH_CONFIG_DIR — not
        through any of the env vars STRIPPED_ENV_PARTS ever touched. Pointed
        at a directory that cannot exist, so `gh` behaves as logged out.
        """
        env = ClaudeCliAgent.scrubbed_environment({"HOME": "/home/x"})
        assert env["GH_CONFIG_DIR"] != ""
        import os

        assert not os.path.exists(env["GH_CONFIG_DIR"])

    def test_network_tools_are_refused_by_default(self):
        assert "WebFetch" in ClaudeCliAgent.DEFAULT_DISALLOWED_TOOLS
        assert "WebSearch" in ClaudeCliAgent.DEFAULT_DISALLOWED_TOOLS

    def test_the_task_is_passed_as_argv_not_through_a_shell(self):
        """Evidence text must not be able to become a command."""
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs

            class _P:
                returncode = 0
                stdout = "done"
                stderr = ""

            return _P()

        agent = ClaudeCliAgent(runner=runner, executable="echo")
        agent.run("; rm -rf /", workspace=".")

        assert isinstance(captured["command"], list)
        assert "; rm -rf /" in captured["command"]
        assert "shell" not in captured["kwargs"]

    def test_every_invocation_carries_the_engineer_guard_settings(self):
        """FEAT-00030: a building session merged its own PR through Bash.
        Every ClaudeCliAgent.run() call must carry the guard settings — not
        opt-in, not something a caller can forget to pass.
        """
        import json

        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command

            class _P:
                returncode = 0
                stdout = "done"
                stderr = ""

            return _P()

        agent = ClaudeCliAgent(runner=runner, executable="echo")
        agent.run("do something", workspace=".")

        command = captured["command"]
        assert "--settings" in command
        settings = json.loads(command[command.index("--settings") + 1])
        assert "Bash(gh *)" in settings["permissions"]["deny"]
        assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"


# ---------------------------------------------------------------------------
# Branch protection
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
class TestDefaultBranchProtection:
    def test_pushing_a_non_incident_branch_is_refused(self, tmp_path):
        repo = fixture_repo.build_broken_repo(tmp_path / "repo")
        manager = RepairWorkspace(repo_path=str(repo), root=str(tmp_path / "wt"))
        rogue = Worktree(
            incident_id="INC-1", path=str(repo), branch="main", base_commit="0" * 40
        )
        with pytest.raises(WorkspaceError, match="not an incident branch"):
            manager.push(rogue)

    def test_policy_refuses_the_default_branch(self):
        policy = SafetyPolicy()
        assert not policy.may_push_to("main", "main")
        assert policy.may_push_to("jarvis/incident-INC-1", "main")

    def test_the_loop_refuses_to_prepare_a_workspace_on_the_default_branch(
        self, tmp_path, store, incident
    ):
        """Belt and braces: if the branch name ever resolved to the base branch."""
        repo = fixture_repo.build_broken_repo(tmp_path / "repo")
        manager = RepairWorkspace(
            repo_path=str(repo),
            root=str(tmp_path / "wt"),
            branch_prefix="",  # makes branch_name_for return the bare incident id
        )
        loop = RepairLoop(
            agent=FakeCodeAgent(),
            policy=SafetyPolicy(repair_enabled=True, max_attempts=1),
            verifier=Verifier(executor_factory=lambda _u: _PassingExecutor()),
            store=store,
            workspace_manager=manager,
            base_branch=incident.id,  # force the collision
            sleep=lambda _s: None,
        )
        outcome = loop.run(incident, _spec())

        assert outcome.resolved is False
        assert "default branch" in outcome.reason
