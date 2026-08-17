"""Tests for the HTTP and browser probe runners against a real fixture site."""

from __future__ import annotations

import pytest

from openjarvis.reliability.probes._stubs import (
    CredentialRedactor,
    MissingCredentialError,
    resolve_credentials,
)
from openjarvis.reliability.probes.browser import BrowserProbeRunner, _Capture
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

    # -- slow versus broken ------------------------------------------------
    #
    # These four are INC-00020. A homepage that returned 200 with the correct
    # title in 9.65s against a 5s budget was filed CRITICAL, escalated 27
    # seconds later and resolved itself. The severity rule that was supposed to
    # catch that keys on failure_kind == "slow", which the browser runner has
    # always set and this runner did not — it reported every failure, including
    # a pure clock overrun, as "assertion".

    def test_a_duration_only_overrun_is_slow_not_an_assertion(self, runner, site):
        spec = _http_spec(
            expect=[{"kind": "status", "matches": "200"}],
            assertions={"max_duration_seconds": 0.000001},
        )
        result = runner.run(spec, base_url=site.base_url)
        assert not result.success
        assert result.failure_kind == "slow"
        assert result.http_status == 200
        assert "over the" in result.error

    def test_a_broken_page_is_an_assertion_however_fast(self, runner, site):
        spec = _http_spec(expect=[{"kind": "text", "value": "Nonexistent"}])
        result = runner.run(spec, base_url=site.base_url)
        assert result.failure_kind == "assertion"

    def test_slow_and_broken_is_an_assertion_not_merely_slow(self, runner, site):
        """The control. Softening a real fault to "slow" would hide an outage."""
        spec = _http_spec(
            expect=[{"kind": "text", "value": "Nonexistent"}],
            assertions={"max_duration_seconds": 0.000001},
        )
        result = runner.run(spec, base_url=site.base_url)
        assert not result.success
        assert result.failure_kind == "assertion"

    def test_a_bad_status_is_an_assertion_even_when_also_slow(self, runner, site):
        spec = _http_spec(
            url="/boom",
            assertions={"max_http_status": 399, "max_duration_seconds": 0.000001},
        )
        result = runner.run(spec, base_url=site.base_url)
        assert result.failure_kind == "assertion"

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
        """A missing image makes Chromium log a console *error*. Treating that
        as a JS error would false-positive on essentially every real site, and
        would double-report what the network listener already captures."""
        spec = _browser_spec(
            steps=[{"action": "goto", "url": "/subresource-404"}],
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


# ---------------------------------------------------------------------------
# Failed-request noise filtering
# ---------------------------------------------------------------------------


class _FakeRequest:
    """The three attributes ``_Capture.on_request_failed`` reads."""

    def __init__(self, url: str, method: str = "GET", failure: str = "net::ERR_FAILED"):
        self.url = url
        self.method = method
        self.failure = failure


class TestIgnoredRequestFailures:
    """A router that cancels its own speculative prefetches reports a *failed*
    request on every healthy page load.  Without a way to name that, the
    ``no_failed_requests`` assertion cannot be used at all on such a site."""

    def test_unfiltered_failures_are_recorded(self):
        capture = _Capture()
        capture.on_request_failed(_FakeRequest("https://x.com/api/thing"))
        assert len(capture.failed_requests) == 1
        assert capture.suppressed_requests == 0

    def test_author_pattern_suppresses_the_match(self):
        capture = _Capture(ignore_requests=[r"\?_rsc=[^ ]* net::ERR_ABORTED"])
        capture.on_request_failed(
            _FakeRequest("https://x.com/privacy?_rsc=19zvn", failure="net::ERR_ABORTED")
        )
        assert capture.failed_requests == []
        assert capture.suppressed_requests == 1

    def test_a_real_failure_still_gets_through(self):
        """The pattern is scoped, not a blanket mute: a genuinely broken
        request on the same host is still reported."""
        capture = _Capture(ignore_requests=[r"\?_rsc=[^ ]* net::ERR_ABORTED"])
        capture.on_request_failed(
            _FakeRequest("https://x.com/api/thing", failure="net::ERR_CONNECTION_RESET")
        )
        assert len(capture.failed_requests) == 1
        assert capture.suppressed_requests == 0

    def test_patterns_can_scope_by_method(self):
        capture = _Capture(ignore_requests=[r"^GET .*/beacon"])
        capture.on_request_failed(_FakeRequest("https://x.com/beacon", method="POST"))
        assert len(capture.failed_requests) == 1

    def test_spec_parses_the_patterns(self):
        spec = _browser_spec(
            assertions={
                "no_failed_requests": True,
                "ignore_request_patterns": ["_rsc="],
            }
        )
        assert spec.assertions.ignore_request_patterns == ["_rsc="]

    def test_patterns_default_to_empty(self):
        assert _browser_spec().assertions.ignore_request_patterns == []


# ---------------------------------------------------------------------------
# Named noise profiles (INC-00001)
# ---------------------------------------------------------------------------


#: The console error verbatim from https://www.wizeperformance.com/dashboard on
#: 2026-08-15, stack trace and all. Hard-coded rather than paraphrased: the
#: whole value of the pattern is that it matches what a real Next.js build
#: actually emits, and a paraphrase would let a drifted pattern keep passing.
_PRODUCTION_RSC_CONSOLE_ERROR = (
    "Failed to fetch RSC payload for https://www.wizeperformance.com/privacy. "
    "Falling back to browser navigation. TypeError: Failed to fetch\n"
    "    at f (https://www.wizeperformance.com/_next/static/chunks/"
    "7537-af1bd9b3d36afbdc.js:1:46127)\n"
    "    at https://www.wizeperformance.com/_next/static/chunks/"
    "7537-af1bd9b3d36afbdc.js:1:59679"
)

#: The corresponding failed request, in the "METHOD URL reason" form
#: ``_Capture`` matches against.
_PRODUCTION_RSC_REQUEST = (
    "https://www.wizeperformance.com/privacy?_rsc=hi3jv",
    "net::ERR_ABORTED",
)


def _rsc_capture():
    """A capture configured exactly as the nextjs_rsc_prefetch profile does."""
    spec = _browser_spec(assertions={"ignore_known_noise": ["nextjs_rsc_prefetch"]})
    return _Capture(
        spec.assertions.resolved_console_patterns(),
        spec.assertions.resolved_request_patterns(),
    )


class _FakeConsoleMessage:
    """The three attributes ``_Capture.on_console`` reads."""

    def __init__(self, text: str, type: str = "error"):  # noqa: A002
        self.type = type
        self.text = text
        self.location = {"url": "https://x.com/app.js"}


class TestNextjsRscProfileIgnoresBenignNoise:
    """INC-00001. The cancelled prefetch reaches JARVIS on two independent
    channels — ``requestfailed`` and ``console`` — and only the first was
    filtered, so a probe asserting ``no_console_errors`` (and not
    ``no_failed_requests``) was never protected by that fix at all."""

    def test_production_console_error_is_suppressed(self):
        capture = _rsc_capture()
        capture.on_console(_FakeConsoleMessage(_PRODUCTION_RSC_CONSOLE_ERROR))
        assert capture.console_errors == []
        assert capture.suppressed_console == 1

    def test_production_failed_request_is_suppressed(self):
        url, reason = _PRODUCTION_RSC_REQUEST
        capture = _rsc_capture()
        capture.on_request_failed(_FakeRequest(url, failure=reason))
        assert capture.failed_requests == []
        assert capture.suppressed_requests == 1

    def test_suppression_is_counted_not_hidden(self):
        """Evidence capture stays intact: what was filtered is still reported
        as a number, so 'quiet' and 'not looking' remain distinguishable."""
        url, reason = _PRODUCTION_RSC_REQUEST
        capture = _rsc_capture()
        capture.on_console(_FakeConsoleMessage(_PRODUCTION_RSC_CONSOLE_ERROR))
        capture.on_request_failed(_FakeRequest(url, failure=reason))
        assert (capture.suppressed_console, capture.suppressed_requests) == (1, 1)

    def test_nothing_is_filtered_without_the_opt_in(self):
        """The profile is not global: a spec that does not name it sees the
        noise, which is what makes this a probe-author decision."""
        url, reason = _PRODUCTION_RSC_REQUEST
        capture = _Capture()
        capture.on_console(_FakeConsoleMessage(_PRODUCTION_RSC_CONSOLE_ERROR))
        capture.on_request_failed(_FakeRequest(url, failure=reason))
        assert len(capture.console_errors) == 1
        assert len(capture.failed_requests) == 1


class TestNextjsRscProfileStillReportsRealFailures:
    """Each of these is a failure the profile must never swallow. They are the
    reason the patterns are over-specified rather than matching 'Failed to
    fetch', which is the shorthand a hurried author would reach for."""

    @pytest.mark.parametrize(
        "text",
        [
            "TypeError: Failed to fetch",
            "Uncaught TypeError: Failed to fetch at loadProfile (app.js:12)",
            "Failed to fetch user profile: 500",
            "Error: Failed to fetch RSC payload",  # truncated, no recovery clause
        ],
        ids=["bare", "uncaught", "app-message", "no-recovery-clause"],
    )
    def test_arbitrary_failed_to_fetch_is_not_ignored(self, text):
        capture = _rsc_capture()
        capture.on_console(_FakeConsoleMessage(text))
        assert len(capture.console_errors) == 1, f"{text!r} was wrongly suppressed"

    def test_genuine_uncaught_exception_is_not_ignored(self):
        capture = _rsc_capture()
        capture.on_page_error("TypeError: window.notAFunction is not a function")
        assert len(capture.page_errors) == 1

    @pytest.mark.parametrize(
        "url,reason",
        [
            ("https://x.com/api/athletes", "net::ERR_CONNECTION_REFUSED"),
            ("https://x.com/api/athletes?_rsc=abc", "net::ERR_CONNECTION_REFUSED"),
            ("https://x.com/logo.png", "net::ERR_ABORTED"),
            ("https://x.com/_next/static/chunks/main.js", "net::ERR_ABORTED"),
            ("https://x.com/styles.css", "net::ERR_ABORTED"),
            ("https://x.com/privacy?_rsc=hi3jv", "net::ERR_NAME_NOT_RESOLVED"),
            ("https://x.com/privacy?_rsc=hi3jv", "net::ERR_CERT_DATE_INVALID"),
        ],
        ids=["api", "api-with-rsc", "image", "script", "css", "dns", "tls"],
    )
    def test_real_request_failures_are_not_ignored(self, url, reason):
        capture = _rsc_capture()
        capture.on_request_failed(_FakeRequest(url, failure=reason))
        assert len(capture.failed_requests) == 1, f"{url} {reason} wrongly suppressed"


class TestNoiseProfileSpecParsing:
    def test_profile_expands_into_both_channels(self):
        spec = _browser_spec(assertions={"ignore_known_noise": ["nextjs_rsc_prefetch"]})
        assert spec.assertions.resolved_console_patterns()
        assert spec.assertions.resolved_request_patterns()

    def test_author_patterns_are_preserved_verbatim(self):
        """Resolution is additive and non-destructive, so ``probe show`` can
        still print what the author wrote rather than an expanded blob."""
        spec = _browser_spec(
            assertions={
                "ignore_known_noise": ["nextjs_rsc_prefetch"],
                "ignore_console_patterns": ["ResizeObserver loop"],
                "ignore_request_patterns": ["/beacon"],
            }
        )
        assert spec.assertions.ignore_console_patterns == ["ResizeObserver loop"]
        assert spec.assertions.ignore_request_patterns == ["/beacon"]
        assert "ResizeObserver loop" in spec.assertions.resolved_console_patterns()
        assert "/beacon" in spec.assertions.resolved_request_patterns()

    def test_unknown_profile_is_a_spec_error(self):
        """A typo must fail loudly. The quiet version of this bug is a probe
        whose author believes noise is filtered when it is not — or believes a
        check is running when the name guarding it never matched."""
        from openjarvis.reliability.probes.spec import ProbeSpecError

        with pytest.raises(ProbeSpecError) as excinfo:
            _browser_spec(assertions={"ignore_known_noise": ["nextjs_rsc_prefetc"]})
        assert "nextjs_rsc_prefetc" in str(excinfo.value)
        assert "nextjs_rsc_prefetch" in str(excinfo.value)  # names the valid one

    def test_defaults_to_no_profiles(self):
        spec = _browser_spec()
        assert spec.assertions.ignore_known_noise == []
        assert spec.assertions.resolved_console_patterns() == []
        assert spec.assertions.resolved_request_patterns() == []


class TestNoiseProfileEndToEnd:
    """Through a real Chromium against the fixture site, so the patterns are
    tested against what a browser actually emits rather than against strings
    this test suite made up."""

    @pytest.fixture
    def runner(self, require_browser):
        return BrowserProbeRunner(headless=True)

    def test_benign_prefetch_abort_passes(self, runner, site):
        spec = _browser_spec(
            steps=[
                {"action": "goto", "url": "/rsc-prefetch-abort"},
                {"action": "wait_for_timeout", "timeout_ms": 800},
            ],
            assertions={
                "no_console_errors": True,
                "no_failed_requests": True,
                "ignore_known_noise": ["nextjs_rsc_prefetch"],
            },
        )
        result = runner.run(spec, base_url=site.base_url)
        assert result.success, result.error
        # Proves the run really did produce the noise, rather than the page
        # quietly failing to reproduce it and the test passing for free.
        assert result.metadata["suppressed_console_count"] >= 1
        assert result.metadata["suppressed_request_count"] >= 1

    def test_same_page_fails_without_the_profile(self, runner, site):
        """The control: identical probe, profile removed."""
        spec = _browser_spec(
            steps=[
                {"action": "goto", "url": "/rsc-prefetch-abort"},
                {"action": "wait_for_timeout", "timeout_ms": 800},
            ],
            assertions={"no_console_errors": True, "no_failed_requests": True},
        )
        result = runner.run(spec, base_url=site.base_url)
        assert not result.success
        assert result.failure_kind == "console_error"

    def test_genuine_javascript_error_still_fails(self, runner, site):
        spec = _browser_spec(
            steps=[{"action": "goto", "url": "/js-error"}],
            assertions={
                "no_console_errors": True,
                "ignore_known_noise": ["nextjs_rsc_prefetch"],
            },
        )
        result = runner.run(spec, base_url=site.base_url)
        assert not result.success
        assert "JavaScript error" in result.error
        console = [e for e in result.evidence if e.kind is EvidenceKind.CONSOLE_ERROR]
        assert any("notAFunction" in (e.content or "") for e in console)

    def test_broken_api_call_alongside_the_noise_still_fails(self, runner, site):
        """Both happen on the same page load. The profile must let exactly one
        of them through."""
        spec = _browser_spec(
            steps=[
                {"action": "goto", "url": "/rsc-prefetch-abort-and-broken-api"},
                {"action": "wait_for_timeout", "timeout_ms": 800},
            ],
            assertions={
                "no_failed_requests": True,
                "ignore_known_noise": ["nextjs_rsc_prefetch"],
            },
        )
        result = runner.run(spec, base_url=site.base_url)
        assert not result.success
        assert result.failure_kind == "network_failure"
        failures = [
            e for e in result.evidence if e.kind is EvidenceKind.NETWORK_FAILURE
        ]
        assert any("/api/profile" in e.summary for e in failures)
        assert not any("_rsc=" in e.summary for e in failures)

    def test_http_500_still_fails_with_the_profile_on(self, runner, site):
        spec = _browser_spec(
            steps=[
                {"action": "goto", "url": "/xhr-fail"},
                {"action": "wait_for_timeout", "timeout_ms": 500},
            ],
            assertions={
                "max_http_status": 399,
                "ignore_known_noise": ["nextjs_rsc_prefetch"],
            },
        )
        result = runner.run(spec, base_url=site.base_url)
        assert not result.success
        errors = [e for e in result.evidence if e.kind is EvidenceKind.HTTP_ERROR]
        assert any("/api/broken" in e.summary for e in errors)


# ---------------------------------------------------------------------------
# Request headers
# ---------------------------------------------------------------------------


class TestProbeHeaders:
    """Some targets cannot be reached without a header — a deployment-
    protection bypass, a feature flag, a language preference. The value of one
    of those is often a shared secret, which is exactly the kind of thing that
    must not be written into a spec file that gets committed and printed by
    ``probe show``.
    """

    def test_literal_headers_are_returned(self):
        from openjarvis.reliability.probes._stubs import resolve_headers

        spec = _browser_spec(headers={"accept-language": "sv-SE"})
        headers, secrets = resolve_headers(spec)
        assert headers == {"accept-language": "sv-SE"}
        assert secrets == {}, "a literal is not a secret"

    def test_env_sourced_header_is_resolved_and_marked_secret(self, monkeypatch):
        from openjarvis.reliability.probes._stubs import resolve_headers

        monkeypatch.setenv("PROBE_TEST_BYPASS", "s3cret-bypass-value")
        spec = _browser_spec(
            headers_from_env={"x-vercel-protection-bypass": "PROBE_TEST_BYPASS"}
        )
        headers, secrets = resolve_headers(spec)
        assert headers["x-vercel-protection-bypass"] == "s3cret-bypass-value"
        assert secrets == {"x-vercel-protection-bypass": "s3cret-bypass-value"}

    def test_missing_env_names_the_variable_not_the_value(self, monkeypatch):
        from openjarvis.reliability.probes._stubs import resolve_headers

        monkeypatch.delenv("PROBE_TEST_ABSENT", raising=False)
        spec = _browser_spec(headers_from_env={"x-bypass": "PROBE_TEST_ABSENT"})
        with pytest.raises(MissingCredentialError) as excinfo:
            resolve_headers(spec)
        assert "PROBE_TEST_ABSENT" in str(excinfo.value)
        assert "x-bypass" in str(excinfo.value)

    def test_secret_header_values_are_redacted_from_evidence(self, monkeypatch):
        """The point of the split return. A bypass token rides on every request,
        so it is the value most likely to come back in an error page."""
        from openjarvis.reliability.probes._stubs import (
            CredentialRedactor,
            resolve_headers,
        )

        monkeypatch.setenv("PROBE_TEST_BYPASS", "s3cret-bypass-value")
        spec = _browser_spec(headers_from_env={"x-bypass": "PROBE_TEST_BYPASS"})
        _, secrets = resolve_headers(spec)
        redactor = CredentialRedactor(secrets)
        leaked = "denied for token s3cret-bypass-value on /dashboard"
        assert "s3cret-bypass-value" not in redactor.redact(leaked)
        assert "[REDACTED]" in redactor.redact(leaked)

    def test_declaring_a_header_twice_is_a_spec_error(self, monkeypatch):
        """Ambiguous: is the value a literal or the name of a variable?"""
        from openjarvis.reliability.probes.spec import ProbeSpecError

        with pytest.raises(ProbeSpecError) as excinfo:
            _browser_spec(
                headers={"x-bypass": "literal"},
                headers_from_env={"x-bypass": "SOME_VAR"},
            )
        assert "x-bypass" in str(excinfo.value)

    def test_no_headers_by_default(self):
        from openjarvis.reliability.probes._stubs import resolve_headers

        assert resolve_headers(_browser_spec()) == ({}, {})

    def test_http_runner_sends_the_headers(self, site, monkeypatch):
        """End to end through the real fixture server, which echoes what it
        received — proves the header reaches the wire, not just the dict."""
        monkeypatch.setenv("PROBE_TEST_BYPASS", "bypass-abc123")
        runner = HttpProbeRunner(verify_ssrf=False)
        spec = _http_spec(
            url="/echo-headers",
            headers={"x-plain": "plain-value"},
            headers_from_env={"x-secret": "PROBE_TEST_BYPASS"},
            expect=[{"kind": "text", "value": "x-plain: plain-value"}],
        )
        result = runner.run(spec, base_url=site.base_url)
        assert result.success, result.error

    def test_http_runner_secret_header_does_not_leak_into_evidence(
        self, site, monkeypatch
    ):
        """The echo endpoint deliberately reflects the secret back. A failing
        assertion captures the body as evidence, so this is the realistic path
        by which a bypass token would escape into an incident."""
        monkeypatch.setenv("PROBE_TEST_BYPASS", "bypass-abc123")
        runner = HttpProbeRunner(verify_ssrf=False)
        spec = _http_spec(
            url="/echo-headers",
            headers_from_env={"x-secret": "PROBE_TEST_BYPASS"},
            # Force a failure so the body is captured as evidence.
            assertions={"max_http_status": 1},
        )
        result = runner.run(spec, base_url=site.base_url)
        assert not result.success
        blob = result.error + "".join(
            (e.summary or "") + (e.content or "") for e in result.evidence
        )
        assert "bypass-abc123" not in blob, "secret header leaked into evidence"

    def test_browser_runner_sends_headers_on_every_request(self, site, monkeypatch):
        """Context-level, not just the navigation: a deployment-protection
        bypass has to cover sub-resource and XHR requests too, or the document
        loads and its assets 401."""
        pytest.importorskip("playwright.sync_api")
        monkeypatch.setenv("PROBE_TEST_BYPASS", "bypass-abc123")
        runner = BrowserProbeRunner(headless=True)
        spec = _browser_spec(
            steps=[{"action": "goto", "url": "/echo-headers"}],
            headers={"x-plain": "plain-value"},
            headers_from_env={"x-secret": "PROBE_TEST_BYPASS"},
            expect=[
                {"kind": "text", "value": "x-plain: plain-value"},
                {"kind": "text", "value": "x-secret: bypass-abc123"},
            ],
        )
        result = runner.run(spec, base_url=site.base_url)
        assert result.success, result.error


# ---------------------------------------------------------------------------
# Access profiles (Vercel preview protection)
# ---------------------------------------------------------------------------


class TestAccessProfiles:
    """A probe that can verify a protected preview must stay usable against
    production, where the secret is absent and irrelevant. The profile is
    therefore resolved per run against the URL actually under test, not per
    spec."""

    PREVIEW = "https://wizeperformance-abc123-team.vercel.app"
    PRODUCTION = "https://www.wizeperformance.com"

    def test_preview_target_gets_the_header(self, monkeypatch):
        from openjarvis.reliability.probes.access import resolve_access_headers

        monkeypatch.setenv("VERCEL_AUTOMATION_BYPASS_SECRET", "bypass-secret-xyz")
        headers, secrets = resolve_access_headers(["vercel_preview"], self.PREVIEW)
        assert headers["x-vercel-protection-bypass"] == "bypass-secret-xyz"
        assert secrets["x-vercel-protection-bypass"] == "bypass-secret-xyz"

    def test_production_target_gets_no_header(self, monkeypatch):
        monkeypatch.setenv("VERCEL_AUTOMATION_BYPASS_SECRET", "bypass-secret-xyz")
        from openjarvis.reliability.probes.access import resolve_access_headers

        headers, secrets = resolve_access_headers(["vercel_preview"], self.PRODUCTION)
        assert headers == {} and secrets == {}

    def test_production_target_does_not_require_the_secret(self, monkeypatch):
        """The property that lets a production probe carry the profile safely:
        with the secret absent, a production run must not raise."""
        from openjarvis.reliability.probes.access import resolve_access_headers

        monkeypatch.delenv("VERCEL_AUTOMATION_BYPASS_SECRET", raising=False)
        assert resolve_access_headers(["vercel_preview"], self.PRODUCTION) == ({}, {})

    def test_missing_secret_on_a_preview_fails_clearly(self, monkeypatch):
        """Failing loudly beats running anyway: an unauthenticated request gets
        Vercel's login page, and the probe would report the application broken."""
        from openjarvis.reliability.probes.access import (
            MissingAccessSecretError,
            resolve_access_headers,
        )

        monkeypatch.delenv("VERCEL_AUTOMATION_BYPASS_SECRET", raising=False)
        with pytest.raises(MissingAccessSecretError) as excinfo:
            resolve_access_headers(["vercel_preview"], self.PREVIEW)
        message = str(excinfo.value)
        assert "VERCEL_AUTOMATION_BYPASS_SECRET" in message
        assert "x-vercel-protection-bypass" in message

    def test_no_profile_means_no_headers_ever(self, monkeypatch):
        from openjarvis.reliability.probes.access import resolve_access_headers

        monkeypatch.setenv("VERCEL_AUTOMATION_BYPASS_SECRET", "bypass-secret-xyz")
        assert resolve_access_headers([], self.PREVIEW) == ({}, {})

    def test_unknown_profile_is_a_spec_error(self):
        from openjarvis.reliability.probes.spec import ProbeSpecError

        with pytest.raises(ProbeSpecError) as excinfo:
            _browser_spec(access_profiles=["vercel_previews"])
        assert "vercel_previews" in str(excinfo.value)
        assert "vercel_preview" in str(excinfo.value)

    def test_reflected_secret_is_redacted_from_evidence(self, site, monkeypatch):
        """End to end against an endpoint that echoes the header back. The
        profile does not apply to a 127.0.0.1 fixture host, so this drives the
        redaction path directly with the same secrets mapping the runner uses."""
        from openjarvis.reliability.probes._stubs import CredentialRedactor

        monkeypatch.setenv("VERCEL_AUTOMATION_BYPASS_SECRET", "bypass-secret-xyz")
        from openjarvis.reliability.probes.access import resolve_access_headers

        _, secrets = resolve_access_headers(["vercel_preview"], self.PREVIEW)
        redactor = CredentialRedactor(secrets)
        leaked = "upstream said: x-vercel-protection-bypass: bypass-secret-xyz"
        assert "bypass-secret-xyz" not in redactor.redact(leaked)
        assert "[REDACTED]" in redactor.redact(leaked)

    def test_secret_never_appears_in_the_spec_or_on_disk(self, tmp_path, monkeypatch):
        """The whole reason for a profile rather than a URL parameter: the spec
        is a file that gets committed, printed and diffed."""
        monkeypatch.setenv("VERCEL_AUTOMATION_BYPASS_SECRET", "bypass-secret-xyz")
        spec_file = tmp_path / "p.toml"
        spec_file.write_text(
            '[probe]\nid = "p"\naccess_profiles = ["vercel_preview"]\n'
            '[[probe.steps]]\naction = "goto"\nurl = "/"\n'
        )
        from openjarvis.reliability.probes.spec import load_probe

        spec = load_probe(spec_file)
        assert spec.access_profiles == ["vercel_preview"]
        assert "bypass-secret-xyz" not in spec_file.read_text()
        assert "bypass-secret-xyz" not in repr(spec)

    def test_http_runner_applies_the_profile_by_target(self, site, monkeypatch):
        """A fixture site on 127.0.0.1 is not a *.vercel.app host, so the
        profile must stay inert and the probe must still pass."""
        monkeypatch.delenv("VERCEL_AUTOMATION_BYPASS_SECRET", raising=False)
        runner = HttpProbeRunner(verify_ssrf=False)
        spec = _http_spec(url="/", access_profiles=["vercel_preview"])
        assert runner.run(spec, base_url=site.base_url).success
