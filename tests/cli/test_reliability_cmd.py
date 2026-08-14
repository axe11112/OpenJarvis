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

    def test_doctor_prints_literal_env_var_names(self, wired):
        result = CliRunner().invoke(cli, ["reliability", "doctor"])
        assert "GITHUB_READONLY_TOKEN" in result.output
        assert "VERCEL_READONLY_TOKEN" in result.output

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
    """The Phase 10 doctor: validates config and credentials, contacts nothing."""

    def test_reports_the_target(self, wired):
        config, _ = wired
        config.reliability.github.repo = "acme/site"
        result = CliRunner().invoke(cli, ["reliability", "doctor"])
        assert "acme/site" in result.output

    def test_missing_credentials_exit_nonzero(self, wired, monkeypatch):
        for name in (
            "GITHUB_READONLY_TOKEN",
            "VERCEL_READONLY_TOKEN",
            "SUPABASE_READONLY_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)
        result = CliRunner().invoke(cli, ["reliability", "doctor"])
        assert result.exit_code == 1
        assert "missing" in result.output

    def test_present_credential_is_reported_configured(self, wired, monkeypatch):
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", "ghp_" + "a" * 36)
        result = CliRunner().invoke(cli, ["reliability", "doctor"])
        assert "configured" in result.output

    def test_never_prints_a_credential_value(self, wired, monkeypatch):
        """The whole point of reporting by name."""
        secret = "ghp_" + "s" * 36
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", secret)
        result = CliRunner().invoke(cli, ["reliability", "doctor"])
        assert secret not in result.output

    def test_reports_the_safety_interlocks(self, wired):
        result = CliRunner().invoke(cli, ["reliability", "doctor"])
        assert "Automatic repair" in result.output
        assert "OFF" in result.output
        assert "pr_only" in result.output

    def test_json_output_is_parseable_and_secret_free(self, wired, monkeypatch):
        import json

        secret = "ghp_" + "j" * 36
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", secret)
        result = CliRunner().invoke(cli, ["reliability", "doctor", "--json"])
        assert secret not in result.output
        payload = json.loads(result.output)
        assert "target" in payload
        assert "credentials" in payload
        assert payload["safety"]["Automatic repair"] == "OFF"


class TestLiveDiagnosticCommand:
    def test_help(self):
        result = CliRunner().invoke(cli, ["reliability", "live-diagnostic", "--help"])
        assert result.exit_code == 0
        assert "read-only" in result.output

    def test_unconfigured_run_never_exits_zero(self, wired, monkeypatch):
        """A run that checked nothing must not look like success."""
        for name in (
            "GITHUB_READONLY_TOKEN",
            "VERCEL_READONLY_TOKEN",
            "SUPABASE_READONLY_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)
        result = CliRunner().invoke(
            cli, ["reliability", "live-diagnostic", "--no-probes", "--no-incidents"]
        )
        assert result.exit_code != 0

    def test_reports_blind_spots_rather_than_passes(self, wired, monkeypatch):
        for name in ("VERCEL_READONLY_TOKEN", "SUPABASE_READONLY_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        result = CliRunner().invoke(
            cli, ["reliability", "live-diagnostic", "--no-probes", "--no-incidents"]
        )
        assert "blind spots, not passes" in result.output

    def test_prints_the_safety_interlocks(self, wired):
        result = CliRunner().invoke(
            cli, ["reliability", "live-diagnostic", "--no-probes", "--no-incidents"]
        )
        assert "Automatic repair: OFF" in result.output


class TestAnalyzeCommand:
    def test_help(self):
        result = CliRunner().invoke(cli, ["reliability", "analyze", "--help"])
        assert result.exit_code == 0

    def test_missing_incident_is_an_error(self, wired):
        result = CliRunner().invoke(cli, ["reliability", "analyze", "INC-99999"])
        assert result.exit_code == 1
        assert "No such incident" in result.output

    def test_emits_a_read_only_prompt(self, wired):
        _, store = wired
        incident = store.create(_incident())
        result = CliRunner().invoke(cli, ["reliability", "analyze", incident.id])
        assert result.exit_code == 0
        assert "Do NOT modify any file." in result.output
        assert "Root cause" in result.output


class TestWatchStartupSafety:
    """§21: refuse to start in a configuration that could reach production."""

    def test_help(self):
        result = CliRunner().invoke(cli, ["reliability", "watch", "--help"])
        assert result.exit_code == 0
        assert "24/7" in result.output

    def test_refuses_repair_plus_default_branch_push(self, wired):
        config, _ = wired
        config.reliability.site.base_url = "https://example.test"
        config.reliability.repair.enabled = True
        config.reliability.repair.workspace = "/tmp/checkout"
        config.reliability.policy.allow_push_to_default_branch = True

        result = CliRunner().invoke(cli, ["reliability", "watch", "--once"])

        assert result.exit_code == 2
        assert "refuses to start" in result.output

    def test_refuses_repair_plus_auto_deploy(self, wired):
        config, _ = wired
        config.reliability.site.base_url = "https://example.test"
        config.reliability.repair.enabled = True
        config.reliability.repair.workspace = "/tmp/checkout"
        config.reliability.policy.deploy_mode = "auto_deploy_allowlisted"

        result = CliRunner().invoke(cli, ["reliability", "watch", "--once"])

        assert result.exit_code == 2
        assert "production" in result.output

    def test_safe_configuration_prints_the_interlocks(self, wired):
        config, _ = wired
        config.reliability.site.base_url = "https://example.test"
        result = CliRunner().invoke(cli, ["reliability", "watch", "--once"])
        # No probes are configured, so it exits 1 — but only after the banner.
        assert "Production deployment" in result.output
        assert "Automatic PR merge" in result.output


class TestStopCommand:
    """§35: a safe emergency stop."""

    def test_stop_reports_what_it_did(self, wired):
        result = CliRunner().invoke(cli, ["reliability", "stop"])
        assert result.exit_code == 0
        assert "JARVIS STOPPED" in result.output
        assert "Production:   UNCHANGED" in result.output

    def test_stop_does_not_delete_incidents(self, wired):
        _, store = wired
        incident = store.create(_incident())
        CliRunner().invoke(cli, ["reliability", "stop"])
        assert store.get(incident.id) is not None
        assert store.verify_chain() == (True, None)

    def test_watch_refuses_to_start_while_stopped(self, wired):
        config, _ = wired
        config.reliability.site.base_url = "https://example.test"
        CliRunner().invoke(cli, ["reliability", "stop"])

        result = CliRunner().invoke(cli, ["reliability", "watch", "--once"])

        assert result.exit_code == 3
        assert "JARVIS is stopped" in result.output


class TestIncidentsCommand:
    def test_reports_the_safety_posture(self, wired):
        result = CliRunner().invoke(cli, ["reliability", "incidents"])
        assert result.exit_code == 0
        assert "Production deployment" in result.output
        assert "Automatic merge" in result.output

    def test_lists_incidents(self, wired):
        _, store = wired
        store.create(_incident())
        result = CliRunner().invoke(cli, ["reliability", "incidents"])
        assert "INC-00001" in result.output

    def test_flags_incidents_needing_recovery(self, wired):
        _, store = wired
        incident = store.create(_incident())
        for step in (
            IncidentState.INVESTIGATING,
            IncidentState.REPRODUCING,
            IncidentState.FIXING,
        ):
            store.transition(incident, step)
        store.transition(incident, IncidentState.RECOVERY_REQUIRED)

        result = CliRunner().invoke(cli, ["reliability", "incidents"])

        assert "will NOT resume automatically" in result.output


class TestRepairCommand:
    def test_refused_when_repair_is_disabled(self, wired):
        result = CliRunner().invoke(cli, ["reliability", "repair", "INC-00001"])
        assert result.exit_code == 1
        assert "disabled" in result.output

    def test_missing_incident_is_an_error(self, wired):
        config, _ = wired
        config.reliability.repair.enabled = True
        result = CliRunner().invoke(cli, ["reliability", "repair", "INC-99999"])
        assert result.exit_code == 1
        assert "No such incident" in result.output


class TestReportCommand:
    def test_missing_incident_is_an_error(self, wired):
        result = CliRunner().invoke(cli, ["reliability", "report", "INC-99999"])
        assert result.exit_code == 1

    def test_renders_a_report(self, wired):
        _, store = wired
        incident = store.create(_incident())
        result = CliRunner().invoke(cli, ["reliability", "report", incident.id])
        assert result.exit_code == 0
        assert "INCIDENT REPORT" in result.output
        assert "Not performed" in result.output

    def test_json_output_is_parseable(self, wired):
        import json

        _, store = wired
        incident = store.create(_incident())
        result = CliRunner().invoke(
            cli, ["reliability", "report", incident.id, "--json"]
        )
        payload = json.loads(result.output)
        assert payload["incident_id"] == incident.id
        assert payload["production_deployed"] is False
