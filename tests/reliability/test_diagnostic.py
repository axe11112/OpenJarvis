"""Tests for target configuration, credential reporting and the live diagnostic.

No live credentials are used: every integration is a double. The behaviours
under test are the Phase 10 safety properties — no false health, no false
incidents, and no secret values anywhere in a report.
"""

from __future__ import annotations

import pytest

from openjarvis.core.config import JarvisConfig
from openjarvis.reliability.diagnostic import LiveDiagnostic
from openjarvis.reliability.health import HealthState
from openjarvis.reliability.probes.placeholder import (
    is_placeholder,
    placeholder_reasons,
)
from openjarvis.reliability.probes.spec import parse_probe
from openjarvis.reliability.sources._stubs import MissingTokenError
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.target import credential_report, resolve_target
from openjarvis.reliability.types import ProbeResult

SECRET = "ghp_" + "z" * 36


def _config(tmp_path, **overrides):
    config = JarvisConfig()
    rc = config.reliability
    rc.enabled = True
    rc.site.base_url = "https://app.example.test"
    rc.github.repo = "acme/site"
    rc.vercel.project_id = "prj_1"
    rc.supabase.project_ref = "abcd"
    rc.probes.directory = str(tmp_path / "probes")
    rc.probes.evidence_dir = str(tmp_path / "evidence")
    for key, value in overrides.items():
        section, _, field = key.partition(".")
        target = getattr(rc, section)
        setattr(target, field, value) if field else None
    return config


@pytest.fixture
def store(tmp_path):
    s = IncidentStore(tmp_path / "incidents.db")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


class TestTargetConfig:
    def test_reads_from_config(self, tmp_path):
        target = resolve_target(_config(tmp_path))
        assert target.repository == "acme/site"
        assert target.production_url == "https://app.example.test"

    def test_environment_overrides_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TARGET_REPOSITORY", "other/repo")
        monkeypatch.setenv("TARGET_PRODUCTION_URL", "https://other.test")
        target = resolve_target(_config(tmp_path))
        assert target.repository == "other/repo"
        assert target.production_url == "https://other.test"

    def test_trailing_slash_is_stripped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TARGET_PRODUCTION_URL", "https://x.test/")
        assert resolve_target(_config(tmp_path)).production_url == "https://x.test"

    def test_missing_lists_env_names(self, tmp_path):
        config = _config(tmp_path)
        config.reliability.vercel.project_id = ""
        assert "VERCEL_PROJECT" in resolve_target(config).missing()

    def test_rejects_a_non_https_url(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TARGET_PRODUCTION_URL", "ftp://x.test")
        assert "https://" in resolve_target(_config(tmp_path)).url_problem()

    def test_warns_about_plain_http(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TARGET_PRODUCTION_URL", "http://x.test")
        assert "plain http" in resolve_target(_config(tmp_path)).url_problem()

    def test_rejects_a_repo_url(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TARGET_REPOSITORY", "https://github.com/a/b")
        assert "owner/name" in resolve_target(_config(tmp_path)).repository_problem()

    def test_serialization_carries_no_secret_fields(self, tmp_path):
        payload = resolve_target(_config(tmp_path)).to_dict()
        forbidden = {"token", "password", "secret", "key"}
        assert set(payload) & forbidden == set()


class TestCredentialReport:
    def test_reports_presence_not_values(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", SECRET)
        report = credential_report(_config(tmp_path))
        github = next(c for c in report if c.label == "GitHub")
        assert github.present is True
        assert SECRET not in str([c.to_dict() for c in report])

    def test_missing_is_reported_by_name(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VERCEL_READONLY_TOKEN", raising=False)
        report = credential_report(_config(tmp_path))
        vercel = next(c for c in report if c.label == "Vercel")
        assert vercel.present is False
        assert vercel.env_name == "VERCEL_READONLY_TOKEN"

    def test_status_has_no_value_bearing_field(self, tmp_path):
        from openjarvis.reliability.target import CredentialStatus

        forbidden = {"value", "token", "secret", "password"}
        assert set(CredentialStatus.__dataclass_fields__) & forbidden == set()

    def test_test_account_is_optional(self, tmp_path):
        report = credential_report(_config(tmp_path))
        account = next(c for c in report if c.label == "Test account password")
        assert account.optional is True


# ---------------------------------------------------------------------------
# Placeholder probes
# ---------------------------------------------------------------------------


class TestPlaceholderDetection:
    def _spec(self, **probe):
        base = {"id": "p", "steps": [{"action": "goto", "url": "/"}]}
        base.update(probe)
        return parse_probe({"probe": base})

    def test_example_selectors_are_placeholders(self):
        spec = self._spec(
            steps=[
                {"action": "goto", "url": "/login"},
                {
                    "action": "fill",
                    "selector": "input[name=email]",
                    "value": "x",
                },
            ]
        )
        assert is_placeholder(spec)
        assert "example selectors" in " ".join(placeholder_reasons(spec))

    def test_explicit_marker(self):
        assert is_placeholder(self._spec(metadata={"placeholder": True}))

    def test_example_host(self):
        spec = self._spec(steps=[{"action": "goto", "url": "https://example.com/"}])
        assert is_placeholder(spec)

    def test_a_real_probe_is_not_a_placeholder(self):
        spec = self._spec(
            steps=[
                {"action": "goto", "url": "/login"},
                {"action": "click", "selector": "[data-cy=real-submit]"},
            ]
        )
        assert not is_placeholder(spec)

    def test_shipped_browser_examples_are_detected(self):
        """The specs in configs/ must be recognised as unaimed."""
        from pathlib import Path

        from openjarvis.reliability.probes.spec import load_probes

        directory = (
            Path(__file__).resolve().parents[2] / "configs" / "reliability" / "probes"
        )
        specs = {s.id: s for s in load_probes(directory)}
        assert is_placeholder(specs["auth-login"])
        assert is_placeholder(specs["dashboard"])


# ---------------------------------------------------------------------------
# The diagnostic
# ---------------------------------------------------------------------------


class _FakeGitHub:
    """A GitHub source whose Actions scope is missing, as in the real world."""

    def __init__(self, *, actions_403: bool = True):
        self._actions_403 = actions_403

    def health(self):
        from openjarvis.reliability.sources._stubs import SourceHealth

        return SourceHealth(source="github", reachable=True)

    def list_commits(self, **kwargs):
        return [{"sha": "abc12345", "message": "fix things", "date": "2026-08-14"}]

    def list_branches(self, **kwargs):
        return ["main", "dev"]

    def list_pull_requests(self, **kwargs):
        return [{"number": 1}]

    def list_workflow_runs(self, **kwargs):
        if self._actions_403:
            import httpx

            raise httpx.HTTPStatusError(
                "Client error '403 Forbidden'",
                request=httpx.Request("GET", "https://api.github.com/x"),
                response=httpx.Response(403),
            )
        return [{"name": "CI", "status": "completed", "conclusion": "success"}]


class _MissingToken:
    """Every call raises MissingTokenError, as an unset token does."""

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise MissingTokenError("vercel: $VERCEL_READONLY_TOKEN is not set")

        return _raise


class TestGitHubCheck:
    def test_partial_scope_is_degraded_with_a_named_blind_spot(self, tmp_path):
        """A real token that reads commits but not Actions."""
        diagnostic = LiveDiagnostic(
            _config(tmp_path), factories={"github": _FakeGitHub}
        )
        result = diagnostic.check_github()
        assert result.state is HealthState.DEGRADED
        assert result.capabilities["commits"].state is HealthState.HEALTHY
        assert result.capabilities["actions"].state is HealthState.UNKNOWN
        assert "actions" in result.unchecked_capabilities

    def test_full_scope_is_healthy(self, tmp_path):
        diagnostic = LiveDiagnostic(
            _config(tmp_path),
            factories={"github": lambda: _FakeGitHub(actions_403=False)},
        )
        assert diagnostic.check_github().state is HealthState.HEALTHY

    def test_no_repository_is_not_configured(self, tmp_path):
        config = _config(tmp_path)
        config.reliability.github.repo = ""
        diagnostic = LiveDiagnostic(config)
        assert diagnostic.check_github().state is HealthState.NOT_CONFIGURED

    def test_a_403_never_becomes_failed(self, tmp_path):
        """403 means "we cannot see", not "production is broken"."""
        diagnostic = LiveDiagnostic(
            _config(tmp_path), factories={"github": _FakeGitHub}
        )
        actions = diagnostic.check_github().capabilities["actions"]
        assert actions.state is not HealthState.FAILED


class TestVercelCheck:
    def test_missing_token_is_unknown_not_healthy(self, tmp_path):
        diagnostic = LiveDiagnostic(
            _config(tmp_path), factories={"vercel": _MissingToken}
        )
        result = diagnostic.check_vercel()
        assert result.state is HealthState.UNKNOWN
        assert result.state is not HealthState.HEALTHY

    def test_no_production_deployment_is_not_healthy(self, tmp_path):
        """An empty deployment list must not read as a healthy production."""

        class _Empty:
            def list_deployments(self, **kwargs):
                return []

            def list_environment_variable_names(self):
                return []

        diagnostic = LiveDiagnostic(_config(tmp_path), factories={"vercel": _Empty})
        result = diagnostic.check_vercel()
        assert (
            result.capabilities["production_deployment"].state
            is not HealthState.HEALTHY
        )

    def test_runtime_errors_are_declared_unmonitored(self, tmp_path):
        class _Empty:
            def list_deployments(self, **kwargs):
                return []

            def list_environment_variable_names(self):
                return []

        diagnostic = LiveDiagnostic(_config(tmp_path), factories={"vercel": _Empty})
        runtime = diagnostic.check_vercel().capabilities["runtime_errors"]
        assert runtime.state is HealthState.NOT_CHECKED
        assert "not implemented" in runtime.summary


class TestSupabaseCheck:
    def test_empty_logs_do_not_read_as_zero_problems(self, tmp_path):
        """ "0 RLS denials" from an unreadable log is not good news."""

        class _NoLogs:
            def get_project(self):
                return {"status": "ACTIVE_HEALTHY"}

            def list_edge_functions(self):
                return []

            def list_migrations(self):
                return []

            def query_logs(self, **kwargs):
                return []

            def rls_diagnostics(self, **kwargs):
                return []

            def auth_diagnostics(self, **kwargs):
                return {"sampled": 0, "failure_count": 0, "by_kind": {}}

        diagnostic = LiveDiagnostic(_config(tmp_path), factories={"supabase": _NoLogs})
        result = diagnostic.check_supabase()
        assert result.capabilities["rls_diagnostics"].state is HealthState.UNKNOWN
        assert result.capabilities["auth_diagnostics"].state is HealthState.UNKNOWN

    def test_paused_project_is_a_real_failure(self, tmp_path):
        class _Paused:
            def get_project(self):
                return {"status": "PAUSED"}

            def list_edge_functions(self):
                return []

            def list_migrations(self):
                return []

            def query_logs(self, **kwargs):
                return [{"event_message": "x"}]

            def rls_diagnostics(self, **kwargs):
                return []

            def auth_diagnostics(self, **kwargs):
                return {"sampled": 1, "failure_count": 0, "by_kind": {}}

        diagnostic = LiveDiagnostic(_config(tmp_path), factories={"supabase": _Paused})
        assert diagnostic.check_supabase().state is HealthState.FAILED

    def test_writes_are_reported_as_disabled(self, tmp_path):
        diagnostic = LiveDiagnostic(
            _config(tmp_path), factories={"supabase": _MissingToken}
        )
        assert diagnostic.check_supabase().facts.get("write_guard") == "active"


class TestProbeCheck:
    def _write(self, tmp_path, name, body):
        directory = tmp_path / "probes"
        directory.mkdir(exist_ok=True)
        (directory / name).write_text(body, encoding="utf-8")

    def test_no_specs_is_not_configured(self, tmp_path):
        (tmp_path / "probes").mkdir()
        diagnostic = LiveDiagnostic(_config(tmp_path))
        assert diagnostic.check_probes().state is HealthState.NOT_CONFIGURED

    def test_placeholder_probe_cannot_pass(self, tmp_path):
        """The requirement: a placeholder must never count as a pass."""
        self._write(
            tmp_path,
            "login.toml",
            '[probe]\nid = "login"\n'
            "[[probe.steps]]\n"
            'action = "goto"\nurl = "/login"\n'
            "[[probe.steps]]\n"
            'action = "fill"\nselector = "input[name=email]"\nvalue = "x"\n',
        )

        class _AlwaysPasses:
            def run(self, spec):
                return ProbeResult(probe_id=spec.id, success=True)

        diagnostic = LiveDiagnostic(_config(tmp_path), executor=_AlwaysPasses())
        result = diagnostic.check_probes()
        login = result.capabilities["login"]
        assert login.state is HealthState.NOT_CONFIGURED
        assert login.state is not HealthState.HEALTHY
        assert "placeholder" in login.summary

    def test_blocked_network_is_a_blind_spot_not_a_failure(self, tmp_path):
        """A refused connection says nothing about the target."""
        self._write(
            tmp_path,
            "home.toml",
            '[probe]\nid = "home"\nrunner = "http"\nurl = "/"\n',
        )

        class _Blocked:
            def run(self, spec):
                return ProbeResult(
                    probe_id=spec.id,
                    success=False,
                    failure_kind="blocked",
                    error="the network refused the connection",
                )

        diagnostic = LiveDiagnostic(_config(tmp_path), executor=_Blocked())
        result = diagnostic.check_probes()
        assert result.capabilities["home"].state is HealthState.UNKNOWN

    def test_a_real_failure_is_failed(self, tmp_path):
        self._write(
            tmp_path,
            "home.toml",
            '[probe]\nid = "home"\nrunner = "http"\nurl = "/"\n',
        )

        class _Fails:
            def run(self, spec):
                return ProbeResult(
                    probe_id=spec.id,
                    success=False,
                    failure_kind="assertion",
                    error="expected 200, got 500",
                )

        diagnostic = LiveDiagnostic(_config(tmp_path), executor=_Fails())
        assert (
            diagnostic.check_probes().capabilities["home"].state is HealthState.FAILED
        )


class TestFullRun:
    def _diagnostic(self, tmp_path, store):
        (tmp_path / "probes").mkdir(exist_ok=True)
        return LiveDiagnostic(
            _config(tmp_path),
            store=store,
            factories={
                "github": _FakeGitHub,
                "vercel": _MissingToken,
                "supabase": _MissingToken,
            },
        )

    def test_unconfigured_run_is_never_healthy(self, tmp_path, store):
        report = self._diagnostic(tmp_path, store).run(include_probes=False)
        assert report.overall.state is not HealthState.HEALTHY

    def test_blind_spots_are_enumerated(self, tmp_path, store):
        report = self._diagnostic(tmp_path, store).run(include_probes=False)
        spots = " ".join(report.blind_spots())
        assert "vercel" in spots
        assert "supabase" in spots

    def test_missing_credentials_open_no_incidents(self, tmp_path, store):
        """An incident about production because JARVIS lacks a token is false."""
        report = self._diagnostic(tmp_path, store).run(include_probes=False)
        assert report.incidents_opened == []
        assert store.count() == 0

    def test_exit_code_is_not_zero_when_incomplete(self, tmp_path, store):
        report = self._diagnostic(tmp_path, store).run(include_probes=False)
        assert report.exit_code != 0

    def test_audit_chain_is_verified(self, tmp_path, store):
        report = self._diagnostic(tmp_path, store).run(include_probes=False)
        assert report.audit_chain_intact is True

    def test_report_contains_no_secret(self, tmp_path, store, monkeypatch):
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", SECRET)
        report = self._diagnostic(tmp_path, store).run(include_probes=False)
        assert SECRET not in str(report.to_dict())

    def test_skipped_probes_are_not_checked_not_healthy(self, tmp_path, store):
        report = self._diagnostic(tmp_path, store).run(include_probes=False)
        probes = next(c for c in report.checks if c.name == "probes")
        assert probes.state is HealthState.NOT_CHECKED
