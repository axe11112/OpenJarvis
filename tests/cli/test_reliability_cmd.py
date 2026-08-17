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


class TestConfiguredPathsExpandHome:
    """``directory = "~/.openjarvis/reliability/probes"`` is what
    docs/JARVIS_LIVE_SETUP.md §2 tells an operator to write, and TOML has no
    notion of a home directory.  Left unexpanded it resolves relative to the
    working directory, and JARVIS reports "no probe specs" while pointing at a
    directory full of them — a monitoring system that says it is watching
    nothing, when it has simply looked in the wrong place."""

    def test_probe_directory_expands_tilde(self, wired):
        from pathlib import Path

        from openjarvis.cli.reliability_cmd import _probe_dir

        config, _ = wired
        config.reliability.probes.directory = "~/.openjarvis/reliability/probes"
        resolved = _probe_dir(config)
        assert "~" not in str(resolved)
        assert resolved == Path.home() / ".openjarvis" / "reliability" / "probes"

    def test_evidence_directory_expands_tilde(self, wired):
        from pathlib import Path

        from openjarvis.cli.reliability_cmd import _evidence_dir

        config, _ = wired
        config.reliability.probes.evidence_dir = "~/.openjarvis/reliability/evidence"
        resolved = _evidence_dir(config)
        assert "~" not in resolved
        assert resolved == str(Path.home() / ".openjarvis" / "reliability" / "evidence")

    def test_absolute_paths_are_left_alone(self, wired, tmp_path):
        from openjarvis.cli.reliability_cmd import _evidence_dir, _probe_dir

        config, _ = wired
        config.reliability.probes.directory = str(tmp_path / "p")
        config.reliability.probes.evidence_dir = str(tmp_path / "e")
        assert str(_probe_dir(config)) == str(tmp_path / "p")
        assert _evidence_dir(config) == str(tmp_path / "e")


class TestProbeBaseUrlHonoursEnvironment:
    """The diagnostic resolves the target through ``resolve_target``, which
    documents ``$TARGET_PRODUCTION_URL`` as the way to point a one-off run at
    staging.  The CLI executor used to read ``config`` directly, so the same
    override moved the diagnostic and left ``probe run`` on production."""

    def test_env_override_wins(self, wired, monkeypatch):
        from openjarvis.cli.reliability_cmd import _build_executor

        config, _ = wired
        config.reliability.site.base_url = "https://production.example"
        monkeypatch.setenv("TARGET_PRODUCTION_URL", "https://staging.example")
        assert _build_executor(config)._base_url == "https://staging.example"

    def test_config_is_used_when_no_override(self, wired, monkeypatch):
        from openjarvis.cli.reliability_cmd import _build_executor

        config, _ = wired
        config.reliability.site.base_url = "https://production.example"
        for name in ("TARGET_PRODUCTION_URL", "PRODUCTION_URL", "TARGET_URL"):
            monkeypatch.delenv(name, raising=False)
        assert _build_executor(config)._base_url == "https://production.example"


class TestOutputStreamSeparation:
    """The CLI writes three different things to two different places, and a
    reader — human, ``jq``, or an assertion — must be able to tell them apart.

    ``live-diagnostic`` always emits WARNING records for its blind spots, so
    every test here runs with warnings genuinely present rather than mocked in.
    """

    @pytest.fixture(autouse=True)
    def _isolated_config(self, tmp_path, monkeypatch):
        """Point the CLI at an empty config instead of the developer's own.

        Without this the assertions read ``~/.openjarvis/config.toml``, so the
        suite's result depends on how the machine it runs on happens to be
        configured — and ``Automatic repair: OFF`` started failing on the day
        somebody enabled automatic repair locally. A test that passes or fails
        based on a file outside the repository is not testing the CLI.
        """
        path = tmp_path / "config.toml"
        path.write_text("[reliability]\nenabled = true\n", encoding="utf-8")
        monkeypatch.setenv("OPENJARVIS_CONFIG", str(path))

    @staticmethod
    def _diagnostic(*extra):
        return CliRunner().invoke(
            cli,
            ["reliability", "live-diagnostic", "--no-probes", "--no-incidents", *extra],
        )

    def test_warnings_are_emitted_and_reach_stderr(self):
        """Guards the fix against the lazy version of itself. Making these
        tests pass by silencing the logger would be a regression: the blind
        spots are the honest part of the diagnostic."""
        result = self._diagnostic()
        assert "diagnostic blind spot" in result.stderr

    def test_human_output_is_on_stdout_and_free_of_log_records(self):
        result = self._diagnostic()
        assert "Automatic repair: OFF" in result.stdout
        assert "WARNING" not in result.stdout

    def test_json_output_is_pure_json_on_stdout(self):
        """``--json`` means a machine is reading. Warnings must not land in the
        document, and the document must parse."""
        import json

        result = self._diagnostic("--json")
        payload = json.loads(result.stdout)
        assert "checks" in payload
        assert "diagnostic blind spot" in result.stderr  # still reported, elsewhere

    def test_json_survives_a_terminal_that_forces_colour(self, monkeypatch):
        """The real defect. ``console.print_json`` styles its output whenever
        colour is enabled, so ``$FORCE_COLOR`` — exported by several terminals
        and agent harnesses — put ANSI escapes at byte 0 of a ``--json``
        document and broke ``| jq``. Set here deliberately, against the
        suite-wide normalisation, so this asserts the CLI's behaviour rather
        than the test environment's."""
        import json

        monkeypatch.setenv("FORCE_COLOR", "3")
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")

        result = self._diagnostic("--json")
        assert "\x1b[" not in result.stdout, "ANSI escapes in machine-readable output"
        assert json.loads(result.stdout)["checks"]

    def test_doctor_json_survives_a_terminal_that_forces_colour(
        self, wired, monkeypatch
    ):
        import json

        monkeypatch.setenv("FORCE_COLOR", "3")
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")

        result = CliRunner().invoke(cli, ["reliability", "doctor", "--json"])
        assert "\x1b[" not in result.stdout
        assert "target" in json.loads(result.stdout)


class TestExplicitBaseUrlWins:
    """`--base-url` must beat both config and the environment.

    Resolving the target through resolve_target fixed one bug and introduced
    another: $TARGET_PRODUCTION_URL is set on any machine configured for real
    monitoring, so it silently outranked the URL the operator typed. `probe run
    --base-url <preview>` then probed production instead — the single worst
    behaviour a target override can have, and it fails silently because
    production is usually healthy.
    """

    def test_flag_beats_the_environment(self, wired, monkeypatch):
        from openjarvis.cli.reliability_cmd import _build_executor

        config, _ = wired
        config.reliability.site.base_url = "https://config.example"
        monkeypatch.setenv("TARGET_PRODUCTION_URL", "https://production.example")
        executor = _build_executor(config, base_url_override="https://preview.example")
        assert executor._base_url == "https://preview.example"

    def test_environment_still_wins_when_no_flag(self, wired, monkeypatch):
        from openjarvis.cli.reliability_cmd import _build_executor

        config, _ = wired
        config.reliability.site.base_url = "https://config.example"
        monkeypatch.setenv("TARGET_PRODUCTION_URL", "https://production.example")
        assert _build_executor(config)._base_url == "https://production.example"

    def test_config_used_when_neither(self, wired, monkeypatch):
        from openjarvis.cli.reliability_cmd import _build_executor

        config, _ = wired
        config.reliability.site.base_url = "https://config.example"
        for name in ("TARGET_PRODUCTION_URL", "PRODUCTION_URL", "TARGET_URL"):
            monkeypatch.delenv(name, raising=False)
        assert _build_executor(config)._base_url == "https://config.example"


class TestIncidentClose:
    """Manual closure exists for incidents that are no longer *true* — a false
    positive, a failure that stopped reproducing, a retired class of check. It
    must be indistinguishable from any other transition as far as the audit
    chain is concerned, and distinguishable from a verified repair as far as a
    reader is concerned.
    """

    def _close(self, incident_id, *extra):
        return CliRunner().invoke(
            cli, ["reliability", "incident", "close", incident_id, *extra]
        )

    def test_normal_closure(self, wired):
        _, store = wired
        incident = store.create(_incident())
        result = self._close(incident.id, "--reason", "false positive")
        assert result.exit_code == 0, result.output
        assert "closed" in result.stdout
        assert store.get(incident.id).state is IncidentState.RESOLVED

    def test_reason_is_required(self, wired):
        _, store = wired
        incident = store.create(_incident())
        result = self._close(incident.id)
        assert result.exit_code != 0
        assert "reason" in (result.stdout + result.stderr).lower()
        assert store.get(incident.id).state is IncidentState.DETECTED

    def test_blank_reason_is_refused(self, wired):
        """`--reason ""` satisfies click but says nothing; the point of the
        flag is the audit record, not the ceremony."""
        _, store = wired
        incident = store.create(_incident())
        result = self._close(incident.id, "--reason", "   ")
        assert result.exit_code != 0
        assert store.get(incident.id).state is IncidentState.DETECTED

    def test_already_resolved_is_refused(self, wired):
        _, store = wired
        incident = store.create(_incident())
        store.transition(incident, IncidentState.RESOLVED, reason="fixed")
        before = len(store.transitions_for(incident.id))
        result = self._close(incident.id, "--reason", "again")
        assert result.exit_code != 0
        assert "already RESOLVED" in result.stdout
        assert len(store.transitions_for(incident.id)) == before, "no audit noise"

    @pytest.mark.parametrize(
        "state",
        [
            IncidentState.FIXING,
            IncidentState.TESTING,
            IncidentState.VERIFYING,
            IncidentState.RECOVERY_REQUIRED,
        ],
    )
    def test_active_repair_is_refused_without_force(self, wired, state):
        """Closing mid-repair races a live attempt that may hold an open
        worktree or a pushed branch."""
        _, store = wired
        incident = store.create(_incident())
        for step in (
            IncidentState.INVESTIGATING,
            IncidentState.REPRODUCING,
            IncidentState.FIXING,
            IncidentState.TESTING,
            IncidentState.VERIFYING,
        ):
            if store.get(incident.id).state is state:
                break
            store.transition(incident, step, reason="advance")
        if state is IncidentState.RECOVERY_REQUIRED:
            store.transition(incident, state, reason="interrupted")

        result = self._close(incident.id, "--reason", "stale")
        assert result.exit_code != 0
        assert "repair is in progress" in result.stdout
        assert store.get(incident.id).state is not IncidentState.RESOLVED

    def test_force_closes_an_active_repair(self, wired):
        _, store = wired
        incident = store.create(_incident())
        for step in (
            IncidentState.INVESTIGATING,
            IncidentState.REPRODUCING,
            IncidentState.FIXING,
        ):
            store.transition(incident, step, reason="advance")
        result = self._close(incident.id, "--reason", "abandoned", "--force")
        assert result.exit_code == 0, result.output
        assert store.get(incident.id).state is IncidentState.RESOLVED

    def test_force_from_fixing_routes_through_human_required(self, wired):
        """FIXING cannot reach RESOLVED directly — the state machine reserves
        that path for verification. The command must not invent a shortcut; it
        records that a human took the incident over, then closed it."""
        _, store = wired
        incident = store.create(_incident())
        for step in (
            IncidentState.INVESTIGATING,
            IncidentState.REPRODUCING,
            IncidentState.FIXING,
        ):
            store.transition(incident, step, reason="advance")
        self._close(incident.id, "--reason", "abandoned", "--force")
        states = [t.to_state for t in store.transitions_for(incident.id)]
        assert IncidentState.HUMAN_REQUIRED in states
        assert states[-1] is IncidentState.RESOLVED

    def test_audit_entry_records_who_why_and_both_states(self, wired):
        _, store = wired
        incident = store.create(_incident())
        self._close(
            incident.id, "--reason", "RSC noise profile", "--actor", "operator:axel"
        )
        last = store.transitions_for(incident.id)[-1]
        assert last.from_state is IncidentState.DETECTED
        assert last.to_state is IncidentState.RESOLVED
        assert last.actor == "operator:axel"
        assert "RSC noise profile" in last.reason
        assert last.at, "a timestamp is recorded"

    def test_actor_defaults_to_a_named_operator_not_jarvis(self, wired):
        """The audit trail is how a reader tells an administrative closure from
        a verified repair, so the actor must not default to 'jarvis'."""
        _, store = wired
        incident = store.create(_incident())
        self._close(incident.id, "--reason", "stale")
        assert store.transitions_for(incident.id)[-1].actor.startswith("operator:")

    def test_audit_chain_remains_valid(self, wired):
        _, store = wired
        for n in range(3):
            inc = store.create(_incident(fingerprint=f"fp_{n}"))
            self._close(inc.id, "--reason", f"stale {n}")
        intact, broken_at = store.verify_chain()
        assert intact, f"audit chain broken at row {broken_at}"

    def test_evidence_and_attempts_are_preserved(self, wired):
        _, store = wired
        incident = store.create(_incident())
        store.add_evidence(
            incident,
            Evidence(kind=EvidenceKind.CONSOLE_ERROR, summary="TypeError in auth.js"),
        )
        store.add_attempt(incident, RepairAttempt(number=1, branch="jarvis/fix/x"))
        self._close(incident.id, "--reason", "stale")
        reloaded = store.get(incident.id)
        assert len(reloaded.evidence) == 1
        assert reloaded.evidence[0].summary == "TypeError in auth.js"
        assert len(reloaded.attempts) == 1

    def test_closed_incident_is_no_longer_open(self, wired):
        """Requirement: status and open-incident counts must stop showing it."""
        _, store = wired
        incident = store.create(_incident())
        assert len(store.list(open_only=True)) == 1
        self._close(incident.id, "--reason", "stale")
        assert store.list(open_only=True) == []
        status = CliRunner().invoke(cli, ["reliability", "status"])
        assert "No open incidents" in status.stdout

    def test_unknown_incident_is_an_error(self, wired):
        result = self._close("INC-99999", "--reason", "stale")
        assert result.exit_code != 0
        assert "No such incident" in result.stdout

    def test_nothing_is_deleted(self, wired):
        _, store = wired
        incident = store.create(_incident())
        self._close(incident.id, "--reason", "stale")
        assert store.get(incident.id) is not None
        assert len(store.transitions_for(incident.id)) >= 2


# ---------------------------------------------------------------------------
# The status table must not hard-code a production authority
#
# Two rows of it were literal "DISABLED" strings, written before automatic merge
# existed. They kept printing DISABLED after merging was switched on. A control
# panel that says an authority is off while it is on is worse than no panel: it
# is read precisely when somebody is deciding whether it is safe to walk away.
# ---------------------------------------------------------------------------


class TestProductionAuthorityIsReportedHonestly:
    def _render(self, wired, **merge):
        from click.testing import CliRunner

        config, _store = wired
        config.reliability.merge.enabled = merge.get("enabled", False)
        config.reliability.merge.method = merge.get("method", "squash")
        config.reliability.merge.required_status_contexts = merge.get(
            "contexts", ["Vercel"]
        )
        result = CliRunner().invoke(cli, ["reliability", "status"])
        assert result.exit_code == 0, result.output
        return result.output

    @staticmethod
    def _row(output: str, label: str) -> str:
        return next(line for line in output.splitlines() if label in line)

    def test_merge_off_reads_disabled(self, wired):
        output = self._render(wired, enabled=False)
        assert "DISABLED" in self._row(output, "Automatic PR merge")

    def test_merge_on_never_reads_disabled(self, wired):
        """The whole point. This row was a hard-coded string."""
        output = self._render(wired, enabled=True)
        row = self._row(output, "Automatic PR merge")
        assert "DISABLED" not in row
        assert "ENABLED" in row

    def test_merge_on_names_the_required_context(self, wired):
        output = self._render(wired, enabled=True, contexts=["Vercel"])
        assert "Vercel" in self._row(output, "Automatic PR merge")

    def test_merge_on_says_it_is_production_authority(self, wired):
        """Because a merge to the default branch deploys production via Git."""
        assert "production authority" in self._render(wired, enabled=True)

    def test_the_deploy_mode_row_is_read_from_config(self, wired):
        config, _store = wired
        config.reliability.policy.deploy_mode = "never"
        assert "DISABLED" in self._row(
            self._render(wired), "Production deployment API"
        )
