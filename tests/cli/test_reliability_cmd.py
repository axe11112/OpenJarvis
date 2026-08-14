"""Tests for the ``jarvis reliability`` CLI group."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from openjarvis.cli import cli
from openjarvis.core.config import JarvisConfig
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import (
    Evidence,
    EvidenceKind,
    Incident,
    IncidentState,
    RepairAttempt,
    Severity,
    VerificationResult,
)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point the CLI at an isolated config and incident database."""
    # Pin the console width so rich never truncates a value under assertion.
    monkeypatch.setenv("COLUMNS", "200")
    config = JarvisConfig()
    config.reliability.db_path = str(tmp_path / "incidents.db")

    monkeypatch.setattr("openjarvis.cli.reliability_cmd._load_config", lambda: config)
    store = IncidentStore(config.reliability.db_path)
    yield config, store
    store.close()


def _incident(**overrides) -> Incident:
    defaults = dict(
        fingerprint="fp_abc",
        severity=Severity.CRITICAL,
        component="authentication",
        title="Login does not reach the dashboard",
        summary="Auth succeeds but the dashboard never loads.",
    )
    defaults.update(overrides)
    return Incident(**defaults)


class TestHelp:
    def test_group_registered(self):
        result = CliRunner().invoke(cli, ["reliability", "--help"])
        assert result.exit_code == 0
        assert "JARVIS" in result.output

    @pytest.mark.parametrize(
        "args",
        [
            ["reliability", "status", "--help"],
            ["reliability", "doctor", "--help"],
            ["reliability", "verify-audit", "--help"],
            ["reliability", "incident", "--help"],
            ["reliability", "incident", "list", "--help"],
            ["reliability", "incident", "show", "--help"],
        ],
    )
    def test_subcommand_help(self, args):
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0


class TestStatus:
    def test_reports_disabled_and_no_incidents(self, wired):
        result = CliRunner().invoke(cli, ["reliability", "status"])
        assert result.exit_code == 0
        assert "disabled" in result.output
        assert "No open incidents" in result.output

    def test_counts_open_incidents(self, wired):
        _, store = wired
        store.create(_incident())
        result = CliRunner().invoke(cli, ["reliability", "status"])
        assert result.exit_code == 0
        assert "1 open incident" in result.output


class TestIncidentList:
    def test_empty(self, wired):
        result = CliRunner().invoke(cli, ["reliability", "incident", "list"])
        assert result.exit_code == 0
        assert "No incidents found" in result.output

    def test_lists_incidents(self, wired):
        _, store = wired
        store.create(_incident())
        result = CliRunner().invoke(cli, ["reliability", "incident", "list"])
        assert result.exit_code == 0
        assert "INC-00001" in result.output
        assert "CRITICAL" in result.output

    def test_filters_by_severity(self, wired):
        _, store = wired
        store.create(_incident(severity=Severity.CRITICAL, fingerprint="fp_1"))
        store.create(_incident(severity=Severity.LOW, fingerprint="fp_2"))
        result = CliRunner().invoke(
            cli, ["reliability", "incident", "list", "--severity", "low"]
        )
        assert result.exit_code == 0
        assert "INC-00002" in result.output
        assert "INC-00001" not in result.output

    def test_open_flag_excludes_resolved(self, wired):
        _, store = wired
        incident = store.create(_incident())
        store.transition(incident, IncidentState.RESOLVED, reason="transient")
        result = CliRunner().invoke(cli, ["reliability", "incident", "list", "--open"])
        assert result.exit_code == 0
        assert "No incidents found" in result.output

    def test_bad_severity_exits_nonzero(self, wired):
        result = CliRunner().invoke(
            cli, ["reliability", "incident", "list", "--severity", "spicy"]
        )
        assert result.exit_code == 1
        assert "Unknown severity" in result.output


class TestIncidentShow:
    def test_missing_incident(self, wired):
        result = CliRunner().invoke(cli, ["reliability", "incident", "show", "INC-9"])
        assert result.exit_code == 1
        assert "No such incident" in result.output

    def test_renders_full_incident(self, wired):
        _, store = wired
        incident = store.create(_incident(repro_steps=["Open /login", "Click Sign In"]))
        store.add_evidence(
            incident,
            Evidence(kind=EvidenceKind.CONSOLE_ERROR, summary="TypeError in auth.js"),
        )
        store.add_attempt(
            incident,
            RepairAttempt(
                number=1,
                branch="jarvis/incident-INC-00001",
                changed_files=["app/auth.ts"],
                tests_passed=True,
                verification=VerificationResult(passed=False, probe_id="auth-login"),
                outcome="verification_failed",
            ),
        )
        store.transition(incident, IncidentState.INVESTIGATING, reason="triage")

        result = CliRunner().invoke(
            cli, ["reliability", "incident", "show", incident.id]
        )
        assert result.exit_code == 0
        assert "Open /login" in result.output
        assert "TypeError in auth.js" in result.output
        assert "verification_failed" in result.output
        assert "INVESTIGATING" in result.output
        assert "triage" in result.output


class TestExternalContentIsEscaped:
    """External text must be rendered as data, not interpreted as rich markup.

    Incident titles, summaries, evidence and reproduction steps are derived from
    page content, logs and API responses, so square brackets in them must not be
    parsed away (or crash the renderer).
    """

    def test_doctor_prints_literal_config_paths(self, wired):
        result = CliRunner().invoke(cli, ["reliability", "doctor"])
        assert "[reliability.site] base_url" in result.output

    def test_incident_show_preserves_bracketed_text(self, wired):
        _, store = wired
        incident = store.create(
            _incident(
                title="[ERROR] auth failed",
                summary="Server said [bold]nope[/bold] on /callback",
                repro_steps=["Open /login?next=[dashboard]"],
            )
        )
        store.add_evidence(
            incident,
            Evidence(kind=EvidenceKind.LOG, summary="[warn] cookie domain mismatch"),
        )
        result = CliRunner().invoke(
            cli, ["reliability", "incident", "show", incident.id]
        )
        assert result.exit_code == 0
        assert "[ERROR] auth failed" in result.output
        assert "[bold]nope[/bold]" in result.output
        assert "[dashboard]" in result.output
        assert "[warn] cookie domain mismatch" in result.output

    def test_incident_list_preserves_bracketed_title(self, wired):
        _, store = wired
        store.create(_incident(title="[ERROR] boom"))
        result = CliRunner().invoke(cli, ["reliability", "incident", "list"])
        assert result.exit_code == 0
        assert "[ERROR] boom" in result.output

    def test_unclosed_markup_does_not_crash_the_renderer(self, wired):
        _, store = wired
        incident = store.create(_incident(title="unterminated [red tag"))
        result = CliRunner().invoke(
            cli, ["reliability", "incident", "show", incident.id]
        )
        assert result.exit_code == 0
        assert result.exception is None


class TestVerifyAudit:
    def test_intact_chain(self, wired):
        _, store = wired
        store.create(_incident())
        result = CliRunner().invoke(cli, ["reliability", "verify-audit"])
        assert result.exit_code == 0
        assert "intact" in result.output

    def test_broken_chain_exits_nonzero(self, wired):
        _, store = wired
        incident = store.create(_incident())
        store.transition(incident, IncidentState.INVESTIGATING, reason="triage")
        store._conn.execute(
            "UPDATE incident_transitions SET reason = 'rewritten' WHERE id = 2"
        )
        store._conn.commit()
        result = CliRunner().invoke(cli, ["reliability", "verify-audit"])
        assert result.exit_code == 1
        assert "broken" in result.output


class TestDoctor:
    def test_clean_when_disabled_but_configured(self, wired):
        config, _ = wired
        config.reliability.site.base_url = "https://example.com"
        result = CliRunner().invoke(cli, ["reliability", "doctor"])
        assert result.exit_code == 0
        assert "Monitoring is disabled" in result.output

    def test_missing_site_is_a_problem(self, wired):
        result = CliRunner().invoke(cli, ["reliability", "doctor"])
        assert result.exit_code == 1
        assert "No site configured" in result.output

    def test_missing_token_env_is_a_problem(self, wired, monkeypatch):
        config, _ = wired
        config.reliability.site.base_url = "https://example.com"
        config.reliability.vercel.enabled = True
        monkeypatch.delenv("VERCEL_READONLY_TOKEN", raising=False)
        result = CliRunner().invoke(cli, ["reliability", "doctor"])
        assert result.exit_code == 1
        assert "VERCEL_READONLY_TOKEN is not set" in result.output

    def test_present_token_env_passes(self, wired, monkeypatch):
        config, _ = wired
        config.reliability.site.base_url = "https://example.com"
        config.reliability.enabled = True
        config.reliability.vercel.enabled = True
        monkeypatch.setenv("VERCEL_READONLY_TOKEN", "not-a-real-token")
        result = CliRunner().invoke(cli, ["reliability", "doctor"])
        assert result.exit_code == 0

    def test_warns_about_unsafe_settings(self, wired):
        config, _ = wired
        config.reliability.enabled = True
        config.reliability.site.base_url = "https://example.com"
        config.reliability.policy.allow_push_to_default_branch = True
        config.reliability.supabase.allow_production_writes = True
        config.reliability.policy.deploy_mode = "auto_deploy_allowlisted"

        result = CliRunner().invoke(cli, ["reliability", "doctor"])
        assert result.exit_code == 0  # warnings, not failures
        assert "default branch" in result.output
        assert "read-only" in result.output
        assert "deploy_mode" in result.output

    def test_repair_without_workspace_is_a_problem(self, wired):
        config, _ = wired
        config.reliability.enabled = True
        config.reliability.site.base_url = "https://example.com"
        config.reliability.repair.enabled = True
        result = CliRunner().invoke(cli, ["reliability", "doctor"])
        assert result.exit_code == 1
        assert "workspace is unset" in result.output
