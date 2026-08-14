"""Tests for the HTTP and browser probe runners against a real fixture site."""

from __future__ import annotations

import pytest

from openjarvis.reliability.probes._stubs import (
    CredentialRedactor,
    MissingCredentialError,
    resolve_credentials,
)
from openjarvis.reliability.probes.browser import BrowserProbeRunner
from openjarvis.reliability.probes.http import HttpProbeRunner, resolve_url
from openjarvis.reliability.probes.spec import parse_probe
from openjarvis.reliability.types import EvidenceKind


def _http_spec(**overrides):
    probe = {
        "id": "test-http",
        "runner": "http",
        "url": "/",
        "timeout_ms": 10000,
    }
    probe.update(overrides)
    return parse_probe({"probe": probe})


def _browser_spec(**overrides):
    probe = {"id": "test-browser", "steps": [{"action": "goto", "url": "/"}]}
    probe.update(overrides)
    return parse_probe({"probe": probe})


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


class TestResolveUrl:
    def test_relative_joins_base(self):
        assert resolve_url("https://x.com", "/login") == "https://x.com/login"

    def test_trailing_and_leading_slashes(self):
        assert resolve_url("https://x.com/", "login") == "https://x.com/login"

    def test_absolute_passes_through(self):
        assert resolve_url("https://x.com", "https://y.com/z") == "https://y.com/z"

    def test_no_base(self):
        assert resolve_url("", "https://y.com") == "https://y.com"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class TestCredentials:
    def test_resolves_from_env(self, monkeypatch):
        monkeypatch.setenv("PROBE_TEST_EMAIL", "user@example.com")
        spec = _browser_spec(credentials={"email": "PROBE_TEST_EMAIL"})
        assert resolve_credentials(spec) == {"email": "user@example.com"}

    def test_missing_env_names_the_variable_not_the_value(self, monkeypatch):
        monkeypatch.delenv("PROBE_TEST_MISSING", raising=False)
        spec = _browser_spec(credentials={"password": "PROBE_TEST_MISSING"})
        with pytest.raises(MissingCredentialError) as excinfo:
            resolve_credentials(spec)
        assert "PROBE_TEST_MISSING" in str(excinfo.value)
        assert "password" in str(excinfo.value)

    def test_no_credentials_is_fine(self):
        assert resolve_credentials(_browser_spec()) == {}


class TestCredentialRedactor:
    def test_redacts_known_values(self):
        redactor = CredentialRedactor({"password": "hunter2000"})
        assert "hunter2000" not in redactor.redact("login failed for hunter2000")
        assert "[REDACTED]" in redactor.redact("login failed for hunter2000")

    def test_leaves_unrelated_text_alone(self):
        redactor = CredentialRedactor({"password": "hunter2000"})
        assert redactor.redact("all good") == "all good"

    def test_ignores_very_short_values(self):
        """Redacting a 2-character secret would mangle unrelated prose."""
        redactor = CredentialRedactor({"pin": "ab"})
        assert redactor.redact("about") == "about"

    def test_longest_first(self):
        redactor = CredentialRedactor({"a": "secret", "b": "secretvalue"})
        assert redactor.redact("secretvalue") == "[REDACTED]"

    def test_empty_redactor_is_a_noop(self):
        assert CredentialRedactor().redact("anything") == "anything"

    def test_redacts_evidence_fields(self):
        from openjarvis.reliability.types import Evidence

        redactor = CredentialRedactor({"password": "hunter2000"})
        evidence = Evidence(
            kind=EvidenceKind.CONSOLE_ERROR,
            summary="bad password hunter2000",
            content="POST body contained hunter2000",
        )
        redactor.redact_evidence(evidence)
        assert "hunter2000" not in evidence.summary
        assert "hunter2000" not in evidence.content


# ---------------------------------------------------------------------------
# HTTP runner
# ---------------------------------------------------------------------------


class TestHttpRunner:
    @pytest.fixture
    def runner(self):
        # The fixture site is on 127.0.0.1, which the SSRF guard blocks by
        # design; probing a loopback target is an explicit opt-out.
        return HttpProbeRunner(verify_ssrf=False)

    def test_healthy_page(self, runner, site):
        spec = _http_spec(expect=[{"kind": "status", "matches": "200"}])
        result = runner.run(spec, base_url=site.base_url)
        assert result.success
        assert result.http_status == 200
        assert result.failure_kind == ""

    def test_detects_500(self, runner, site):
        spec = _http_spec(url="/boom", assertions={"max_http_status": 399})
        result = runner.run(spec, base_url=site.base_url)
        assert not result.success
        assert result.http_status == 500
        assert "exceeds the allowed maximum" in result.error
        assert any(e.kind is EvidenceKind.HTTP_ERROR for e in result.evidence)

    def test_status_expectation_failure(self, runner, site):
        spec = _http_spec(url="/boom", expect=[{"kind": "status", "matches": "200"}])
        result = runner.run(spec, base_url=site.base_url)
        assert not result.success
        assert "expected HTTP 200, got 500" in result.error

    def test_text_expectation(self, runner, site):
        spec = _http_spec(expect=[{"kind": "text", "value": "Sign in"}])
        assert runner.run(spec, base_url=site.base_url).success

    def test_missing_text_fails(self, runner, site):
        spec = _http_spec(expect=[{"kind": "text", "value": "Nonexistent"}])
        result = runner.run(spec, base_url=site.base_url)
        assert not result.success
        assert "expected the body to contain" in result.error

    def test_not_text_expectation(self, runner, site):
        spec = _http_spec(expect=[{"kind": "not_text", "value": "Sign in"}])
        assert not runner.run(spec, base_url=site.base_url).success

    def test_title_expectation(self, runner, site):
        spec = _http_spec(expect=[{"kind": "title", "matches": "Sign in"}])
        assert runner.run(spec, base_url=site.base_url).success

    def test_url_expectation_on_redirect(self, runner, site):
        spec = _http_spec(
            url="/redirect-loop-home", expect=[{"kind": "url", "matches": "/login"}]
        )
        result = runner.run(spec, base_url=site.base_url)
        assert result.success
        assert result.final_url.endswith("/login")

    def test_unexpected_redirect_is_evidence(self, runner, site):
        spec = _http_spec(url="/redirect-loop-home")
        result = runner.run(spec, base_url=site.base_url)
        assert any(e.kind is EvidenceKind.UNEXPECTED_REDIRECT for e in result.evidence)

    def test_duration_budget(self, runner, site):
        spec = _http_spec(url="/slow", assertions={"max_duration_seconds": 0.2})
        result = runner.run(spec, base_url=site.base_url)
        assert not result.success
        assert "over the" in result.error

    def test_connection_failure(self, runner):
        spec = _http_spec(url="http://127.0.0.1:1/nothing", timeout_ms=2000)
        result = runner.run(spec)
        assert not result.success
        assert result.failure_kind in ("network_failure", "timeout")

    def test_ssrf_guard_blocks_loopback_by_default(self, site):
        result = HttpProbeRunner().run(_http_spec(), base_url=site.base_url)
        assert not result.success
        assert result.failure_kind == "blocked"

    def test_visible_expectation_unsupported(self, runner, site):
        spec = _http_spec(expect=[{"kind": "visible", "selector": "#x"}])
        result = runner.run(spec, base_url=site.base_url)
        assert "not supported by the http runner" in result.error


# ---------------------------------------------------------------------------
# Browser runner
# ---------------------------------------------------------------------------


@pytest.mark.browser
class TestBrowserRunner:
    @pytest.fixture
    def runner(self, require_browser, chromium_path):
        return BrowserProbeRunner(headless=True, executable_path=chromium_path)

    def test_healthy_workflow(self, runner, site):
        spec = _browser_spec(
            steps=[{"action": "goto", "url": "/login"}],
            expect=[
                {"kind": "title", "matches": "Sign in"},
                {"kind": "visible", "selector": "[data-testid=submit]"},
            ],
            assertions={"no_console_errors": True, "no_failed_requests": True},
        )
        result = runner.run(spec, base_url=site.base_url)
        assert result.success, result.error
        assert result.steps_completed == 1

    def test_login_reaches_dashboard(self, runner, site, monkeypatch):
        monkeypatch.setenv("PROBE_EMAIL", "probe@example.com")
        monkeypatch.setenv("PROBE_PASSWORD", "correct-horse-battery")
        spec = _browser_spec(
            credentials={"email": "PROBE_EMAIL", "password": "PROBE_PASSWORD"},
            steps=[
                {"action": "goto", "url": "/login"},
                {
                    "action": "fill",
                    "selector": "[data-testid=email]",
                    "value_from": "email",
                },
                {
                    "action": "fill",
                    "selector": "[data-testid=password]",
                    "value_from": "password",
                },
                {"action": "click", "selector": "[data-testid=submit]"},
                {"action": "wait_for_url", "url": "/dashboard"},
            ],
            expect=[
                {"kind": "url", "matches": "/dashboard"},
                {"kind": "visible", "selector": "[data-testid=dashboard-root]"},
            ],
        )
        result = runner.run(spec, base_url=site.base_url)
        assert result.success, result.error

    def test_broken_login_is_detected(self, runner, broken_site, monkeypatch):
        """The headline case: the form submits, auth 'succeeds', and the user
        is bounced back to /login instead of the dashboard."""
        monkeypatch.setenv("PROBE_EMAIL", "probe@example.com")
        monkeypatch.setenv("PROBE_PASSWORD", "correct-horse-battery")
        spec = _browser_spec(
            credentials={"email": "PROBE_EMAIL", "password": "PROBE_PASSWORD"},
            timeout_ms=5000,
            steps=[
                {"action": "goto", "url": "/login"},
                {
                    "action": "fill",
                    "selector": "[data-testid=email]",
                    "value_from": "email",
                },
                {
                    "action": "fill",
                    "selector": "[data-testid=password]",
                    "value_from": "password",
                },
                {"action": "click", "selector": "[data-testid=submit]"},
            ],
            expect=[{"kind": "url", "matches": "/dashboard"}],
        )
        result = runner.run(spec, base_url=broken_site.base_url)
        assert not result.success
        assert "expected the URL to match /dashboard" in result.error
        assert "/login" in result.final_url

    def test_subresource_404_is_not_a_javascript_error(self, runner, site):
        """A missing favicon makes Chromium log a console *error*. Treating that
        as a JS error would false-positive on essentially every real site, and
        would double-report what the network listener already captures."""
        spec = _browser_spec(
            steps=[{"action": "goto", "url": "/login"}],
            assertions={"no_console_errors": True},
        )
        result = runner.run(spec, base_url=site.base_url)
        assert result.success, result.error
        assert result.metadata["suppressed_console_count"] >= 1

    def test_author_supplied_console_noise_is_ignored(self, runner, site):
        spec = _browser_spec(
            steps=[{"action": "goto", "url": "/js-error"}],
            assertions={
                "no_console_errors": True,
                "ignore_console_patterns": ["notAFunction"],
            },
        )
        assert runner.run(spec, base_url=site.base_url).success

    def test_javascript_error_is_captured(self, runner, site):
        spec = _browser_spec(
            steps=[{"action": "goto", "url": "/js-error"}],
            assertions={"no_console_errors": True},
        )
        result = runner.run(spec, base_url=site.base_url)
        assert not result.success
        assert "JavaScript error" in result.error
        console = [e for e in result.evidence if e.kind is EvidenceKind.CONSOLE_ERROR]
        assert console
        assert "notAFunction" in console[0].content

    def test_failed_xhr_is_captured_as_http_error(self, runner, site):
        spec = _browser_spec(
            steps=[
                {"action": "goto", "url": "/xhr-fail"},
                {"action": "wait_for_timeout", "timeout_ms": 500},
            ],
            assertions={"max_http_status": 399},
        )
        result = runner.run(spec, base_url=site.base_url)
        assert not result.success
        errors = [e for e in result.evidence if e.kind is EvidenceKind.HTTP_ERROR]
        assert any("/api/broken" in e.summary for e in errors)

    def test_missing_selector_times_out(self, runner, site):
        spec = _browser_spec(
            timeout_ms=1500,
            steps=[
                {"action": "goto", "url": "/login"},
                {"action": "click", "selector": "#does-not-exist"},
            ],
        )
        result = runner.run(spec, base_url=site.base_url)
        assert not result.success
        assert result.failure_kind == "timeout"
        assert result.steps_completed == 1

    def test_screenshot_and_trace_written_on_failure(self, runner, site, tmp_path):
        spec = _browser_spec(
            timeout_ms=1500,
            trace_on_failure=True,
            steps=[
                {"action": "goto", "url": "/login"},
                {"action": "click", "selector": "#nope"},
            ],
        )
        result = runner.run(spec, base_url=site.base_url, evidence_dir=str(tmp_path))
        assert not result.success
        kinds = {e.kind for e in result.evidence}
        assert EvidenceKind.SCREENSHOT in kinds
        assert EvidenceKind.TRACE in kinds
        for item in result.evidence:
            if item.artifact_path:
                from pathlib import Path

                assert Path(item.artifact_path).is_file()

    def test_no_artifacts_on_success(self, runner, site, tmp_path):
        """A healthy site should cost nothing in disk."""
        spec = _browser_spec(steps=[{"action": "goto", "url": "/login"}])
        result = runner.run(spec, base_url=site.base_url, evidence_dir=str(tmp_path))
        assert result.success
        assert not any(e.artifact_path for e in result.evidence)

    def test_credentials_never_appear_in_the_result(
        self, runner, broken_site, monkeypatch
    ):
        """The whole point of the redactor: a failing login must not leak the
        password into evidence, error text or metadata."""
        secret = "SuperSecretProbePassword123"
        monkeypatch.setenv("PROBE_EMAIL", "probe@example.com")
        monkeypatch.setenv("PROBE_PASSWORD", secret)
        spec = _browser_spec(
            credentials={"email": "PROBE_EMAIL", "password": "PROBE_PASSWORD"},
            timeout_ms=2000,
            steps=[
                {"action": "goto", "url": "/login"},
                {
                    "action": "fill",
                    "selector": "[data-testid=password]",
                    "value_from": "password",
                },
                {"action": "click", "selector": "#missing-button"},
            ],
        )
        result = runner.run(spec, base_url=broken_site.base_url)
        serialized = str(result.to_dict())
        assert secret not in serialized

    def test_contexts_are_isolated_between_runs(self, runner, site):
        """Each run gets a fresh context, so auth state cannot bleed across
        probes and make a broken login look healthy."""
        spec = _browser_spec(
            steps=[{"action": "goto", "url": "/login"}],
            expect=[{"kind": "title", "matches": "Sign in"}],
        )
        first = runner.run(spec, base_url=site.base_url)
        second = runner.run(spec, base_url=site.base_url)
        assert first.success and second.success
        assert first.metadata["run_id"] != second.metadata["run_id"]
