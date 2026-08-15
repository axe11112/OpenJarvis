"""Browser probe — runs a real workflow and captures why it failed.

Deliberately separate from ``openjarvis.tools.browser``.  Those tools are built
for interactive agent use: one shared module-level session, one page, no event
listeners.  A reliability probe needs the opposite — an isolated context per
run, console/network/response listeners attached before the first navigation,
and artifacts written on failure.

Playwright is an optional dependency (``uv sync --extra browser``); importing
this module without it is fine, and only :meth:`BrowserProbeRunner.run` raises.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.reliability.probes._stubs import (
    BaseProbeRunner,
    CredentialRedactor,
    ProbeRunnerRegistry,
    resolve_credentials,
)
from openjarvis.reliability.probes.http import resolve_url
from openjarvis.reliability.probes.spec import ProbeSpec, ProbeStep
from openjarvis.reliability.types import (
    Evidence,
    EvidenceKind,
    ProbeResult,
    TrustLevel,
    now_iso,
)

logger = logging.getLogger(__name__)

__all__ = ["BrowserProbeRunner", "BrowserUnavailableError"]

#: Environment override for the Chromium binary.  Needed where the bundled
#: browser revision does not match the installed Playwright version (pinned
#: images, air-gapped hosts).
BROWSER_EXECUTABLE_ENV = "JARVIS_BROWSER_EXECUTABLE"

#: Console/network noise caps, so one broken page cannot produce a 50 MB incident.
_MAX_CONSOLE_ENTRIES = 50
_MAX_NETWORK_ENTRIES = 50
_MAX_TEXT_CHARS = 4000

#: Console messages the browser itself emits for failed subresource loads.
#: These are *network* problems, and the response/requestfailed listeners
#: already capture them with far more detail (URL, status, method).  Counting
#: them as JavaScript errors too would double-report every missing favicon and
#: make ``no_console_errors`` unusable on a real site.
_BROWSER_RESOURCE_NOISE = re.compile(
    r"^Failed to load resource\b|^net::ERR_|favicon\.ico",
    re.IGNORECASE,
)


class BrowserUnavailableError(RuntimeError):
    """Raised when Playwright or a browser binary is not available."""


class _Capture:
    """Accumulates observations from page event listeners.

    Parameters
    ----------
    ignore_console:
        Regexes for known, benign console noise — the spec's own patterns plus
        those of any noise profile it named.
    ignore_requests:
        Regexes for request failures the application causes on purpose —
        matched against ``"METHOD URL reason"``, same two sources.
    """

    def __init__(
        self,
        ignore_console: Optional[List[str]] = None,
        ignore_requests: Optional[List[str]] = None,
    ) -> None:
        self.console_errors: List[Dict[str, Any]] = []
        self.failed_requests: List[Dict[str, Any]] = []
        self.http_errors: List[Dict[str, Any]] = []
        self.page_errors: List[str] = []
        self.suppressed_console = 0
        self.suppressed_requests = 0
        self._ignore = [re.compile(p) for p in (ignore_console or [])]
        self._ignore_requests = [re.compile(p) for p in (ignore_requests or [])]

    def _is_author_ignored(self, text: str) -> bool:
        """Author-declared noise, applied to every JavaScript error source."""
        return any(pattern.search(text) for pattern in self._ignore)

    def _is_console_noise(self, text: str) -> bool:
        """Author noise, plus browser-generated subresource-load messages.

        The browser-generated filter applies only to console messages: a
        ``pageerror`` is always a genuine uncaught exception.
        """
        return bool(_BROWSER_RESOURCE_NOISE.search(text)) or self._is_author_ignored(
            text
        )

    def on_console(self, message: Any) -> None:
        if message.type != "error":
            return
        text = str(message.text)[:_MAX_TEXT_CHARS]
        if self._is_console_noise(text):
            self.suppressed_console += 1
            return
        if len(self.console_errors) < _MAX_CONSOLE_ENTRIES:
            location = getattr(message, "location", None) or {}
            self.console_errors.append(
                {
                    "text": text,
                    "url": location.get("url", "")
                    if isinstance(location, dict)
                    else "",
                }
            )

    def on_page_error(self, error: Any) -> None:
        text = str(error)[:_MAX_TEXT_CHARS]
        if self._is_author_ignored(text):
            self.suppressed_console += 1
            return
        if len(self.page_errors) < _MAX_CONSOLE_ENTRIES:
            self.page_errors.append(text)

    def on_request_failed(self, request: Any) -> None:
        failure = getattr(request, "failure", None)
        entry = {
            "url": request.url,
            "method": request.method,
            "reason": str(failure) if failure else "unknown",
        }
        subject = f"{entry['method']} {entry['url']} {entry['reason']}"
        if any(pattern.search(subject) for pattern in self._ignore_requests):
            self.suppressed_requests += 1
            return
        if len(self.failed_requests) >= _MAX_NETWORK_ENTRIES:
            return
        self.failed_requests.append(entry)

    def on_response(self, response: Any) -> None:
        if response.status < 400 or len(self.http_errors) >= _MAX_NETWORK_ENTRIES:
            return
        self.http_errors.append(
            {
                "url": response.url,
                "status": response.status,
                "method": getattr(response.request, "method", ""),
            }
        )


@ProbeRunnerRegistry.register("browser")
class BrowserProbeRunner(BaseProbeRunner):
    """Executes a workflow in a real browser and reports what broke.

    Parameters
    ----------
    headless:
        Run without a visible window (always true in production).
    executable_path:
        Explicit Chromium binary.  Falls back to ``$JARVIS_BROWSER_EXECUTABLE``
        and then to Playwright's bundled browser.
    viewport:
        ``(width, height)``.
    """

    runner_id = "browser"

    def __init__(
        self,
        *,
        headless: bool = True,
        executable_path: str = "",
        viewport: tuple[int, int] = (1280, 720),
    ) -> None:
        self._headless = headless
        self._executable_path = executable_path or os.environ.get(
            BROWSER_EXECUTABLE_ENV, ""
        )
        self._viewport = viewport

    # -- entry point ------------------------------------------------------

    def run(
        self,
        spec: ProbeSpec,
        *,
        base_url: str = "",
        evidence_dir: Optional[str] = None,
        **options: Any,
    ) -> ProbeResult:
        """Run the workflow once, capturing evidence on failure."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise BrowserUnavailableError(
                "Playwright is not installed. Install with: uv sync --extra browser"
            ) from exc

        credentials = resolve_credentials(spec)
        redactor = CredentialRedactor(credentials)
        # Resolved, not raw: a named noise profile contributes to *both*
        # channels.  One framework event can arrive as a failed request and as
        # a console error, and filtering only the channel it was first noticed
        # on is what let the RSC prefetch abort keep failing this probe.
        capture = _Capture(
            spec.assertions.resolved_console_patterns(),
            spec.assertions.resolved_request_patterns(),
        )

        run_id = uuid.uuid4().hex[:12]
        artifact_dir = self._artifact_dir(evidence_dir, spec.id, run_id)
        trace_path = (
            str(artifact_dir / "trace.zip")
            if artifact_dir and spec.trace_on_failure
            else ""
        )

        started_at = now_iso()
        started = time.monotonic()
        launch_kwargs: Dict[str, Any] = {"headless": self._headless}
        if self._executable_path:
            launch_kwargs["executable_path"] = self._executable_path

        steps_completed = 0
        failure_kind = ""
        error = ""
        final_url = ""
        page_title = ""
        tracing_started = False

        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(**launch_kwargs)
            except Exception as exc:  # pragma: no cover - environment dependent
                raise BrowserUnavailableError(
                    f"Could not launch Chromium: {exc}"
                ) from exc

            # A fresh context per run: no cookie or auth-state bleed between
            # probes, and per-run tracing.
            context = browser.new_context(
                viewport={"width": self._viewport[0], "height": self._viewport[1]},
                ignore_https_errors=False,
            )
            context.set_default_timeout(spec.timeout_ms)
            if trace_path:
                with suppress(Exception):
                    context.tracing.start(screenshots=True, snapshots=True)
                    tracing_started = True

            page = context.new_page()
            page.on("console", capture.on_console)
            page.on("pageerror", capture.on_page_error)
            page.on("requestfailed", capture.on_request_failed)
            page.on("response", capture.on_response)

            try:
                for step in spec.steps:
                    self._execute(page, step, spec, base_url, credentials)
                    steps_completed += 1
            except Exception as exc:
                failure_kind, error = self._classify(exc, redactor)
            finally:
                with suppress(Exception):
                    final_url = page.url
                with suppress(Exception):
                    page_title = page.title()

            failures: List[str] = []
            if error:
                failures.append(error)
            else:
                for expectation in spec.expect:
                    problem = self._check(page, expectation, final_url, page_title)
                    if problem:
                        failures.append(redactor.redact(problem))
                if failures:
                    failure_kind = "assertion"

            duration = time.monotonic() - started

            # Cross-cutting assertions
            if spec.assertions.no_console_errors and (
                capture.console_errors or capture.page_errors
            ):
                count = len(capture.console_errors) + len(capture.page_errors)
                failures.append(f"{count} JavaScript error(s) on the page")
                failure_kind = failure_kind or "console_error"
            if spec.assertions.no_failed_requests and capture.failed_requests:
                failures.append(
                    f"{len(capture.failed_requests)} network request(s) failed"
                )
                failure_kind = failure_kind or "network_failure"
            ceiling = spec.assertions.max_http_status
            if ceiling:
                over = [e for e in capture.http_errors if e["status"] > ceiling]
                if over:
                    failures.append(
                        f"{len(over)} response(s) exceeded HTTP {ceiling} "
                        f"(worst: {max(e['status'] for e in over)})"
                    )
                    failure_kind = failure_kind or "http_error"
            if (
                spec.assertions.max_duration_seconds
                and duration > spec.assertions.max_duration_seconds
            ):
                failures.append(
                    f"took {duration:.2f}s, over the "
                    f"{spec.assertions.max_duration_seconds:.2f}s budget"
                )
                failure_kind = failure_kind or "slow"

            success = not failures
            evidence = self._build_evidence(
                capture, redactor, spec, final_url, page_title
            )

            # Artifacts only on failure — a healthy site should cost nothing.
            if not success and artifact_dir is not None:
                shot = self._screenshot(page, artifact_dir)
                if shot:
                    evidence.append(
                        Evidence(
                            kind=EvidenceKind.SCREENSHOT,
                            summary=f"Screenshot at failure ({final_url})",
                            artifact_path=shot,
                            source="browser_probe",
                            trust=TrustLevel.TRUSTED,
                        )
                    )

            if tracing_started:
                with suppress(Exception):
                    if success:
                        context.tracing.stop()
                    else:
                        context.tracing.stop(path=trace_path)
                        evidence.append(
                            Evidence(
                                kind=EvidenceKind.TRACE,
                                summary="Playwright trace",
                                artifact_path=trace_path,
                                source="browser_probe",
                                trust=TrustLevel.TRUSTED,
                            )
                        )

            with suppress(Exception):
                context.close()
            with suppress(Exception):
                browser.close()

        return ProbeResult(
            probe_id=spec.id,
            success=success,
            failure_kind="" if success else (failure_kind or "assertion"),
            error=redactor.redact("; ".join(failures)),
            duration_seconds=duration,
            final_url=final_url,
            http_status=max((e["status"] for e in capture.http_errors), default=0),
            steps_completed=steps_completed,
            evidence=[redactor.redact_evidence(item) for item in evidence],
            started_at=started_at,
            metadata={
                "run_id": run_id,
                "title": redactor.redact(page_title),
                "console_error_count": len(capture.console_errors)
                + len(capture.page_errors),
                "suppressed_console_count": capture.suppressed_console,
                "failed_request_count": len(capture.failed_requests),
                "suppressed_request_count": capture.suppressed_requests,
                "http_error_count": len(capture.http_errors),
            },
        )

    # -- step execution ---------------------------------------------------

    @staticmethod
    def _execute(
        page: Any,
        step: ProbeStep,
        spec: ProbeSpec,
        base_url: str,
        credentials: Dict[str, str],
    ) -> None:
        """Perform one workflow step."""
        timeout = step.timeout_ms or spec.timeout_ms
        action = step.action

        if action == "goto":
            page.goto(resolve_url(base_url, step.url), timeout=timeout)
        elif action == "click":
            page.click(step.selector, timeout=timeout)
        elif action == "fill":
            value = credentials[step.value_from] if step.value_from else step.value
            page.fill(step.selector, value, timeout=timeout)
        elif action == "press":
            page.press(step.selector, step.value or "Enter", timeout=timeout)
        elif action == "select":
            page.select_option(step.selector, step.value, timeout=timeout)
        elif action == "check":
            page.check(step.selector, timeout=timeout)
        elif action == "uncheck":
            page.uncheck(step.selector, timeout=timeout)
        elif action == "wait_for":
            page.wait_for_selector(step.selector, state=step.state, timeout=timeout)
        elif action == "wait_for_url":
            page.wait_for_url(
                re.compile(step.url)
                if not step.url.startswith(("http", "/"))
                else resolve_url(base_url, step.url),
                timeout=timeout,
            )
        elif action == "wait_for_timeout":
            page.wait_for_timeout(step.timeout_ms or 1000)
        elif action == "screenshot":
            pass  # captured on failure; an explicit step is a no-op marker
        else:  # pragma: no cover - spec validation rejects these
            raise ValueError(f"Unsupported action '{action}'")

    @staticmethod
    def _classify(exc: Exception, redactor: CredentialRedactor) -> tuple[str, str]:
        """Map a Playwright exception onto (failure_kind, redacted message)."""
        name = type(exc).__name__
        message = redactor.redact(str(exc).split("\nCall log")[0].strip())
        if "Timeout" in name or "Timeout" in message:
            return "timeout", message or "Timed out"
        if "net::" in message or "NS_ERROR" in message:
            return "navigation", message
        return "step_error", f"{name}: {message}" if message else name

    # -- expectations -----------------------------------------------------

    @staticmethod
    def _check(page: Any, expectation: Any, final_url: str, title: str) -> str:
        """Return a failure description, or '' when the expectation holds."""
        kind = expectation.kind
        try:
            if kind == "url":
                if not re.search(expectation.matches, final_url):
                    return (
                        f"expected the URL to match {expectation.matches}, "
                        f"got {final_url}"
                    )
                return ""
            if kind == "title":
                if expectation.matches not in title:
                    return (
                        f"expected the title to contain {expectation.matches!r}, "
                        f"got {title!r}"
                    )
                return ""
            if kind == "visible":
                if not page.is_visible(expectation.selector):
                    return f"expected {expectation.selector} to be visible"
                return ""
            if kind == "hidden":
                if page.is_visible(expectation.selector):
                    return f"expected {expectation.selector} to be hidden"
                return ""
            if kind in ("text", "not_text"):
                content = (
                    page.inner_text(expectation.selector)
                    if expectation.selector
                    else page.inner_text("body")
                )
                present = expectation.value in content
                if kind == "text" and not present:
                    return (
                        f"expected {expectation.selector or 'the page'} to contain "
                        f"{expectation.value!r}"
                    )
                if kind == "not_text" and present:
                    return (
                        f"expected {expectation.selector or 'the page'} not to "
                        f"contain {expectation.value!r}"
                    )
                return ""
            if kind == "status":
                return ""  # asserted via assertions.max_http_status
        except Exception as exc:
            return f"could not evaluate '{kind}': {exc}"
        return f"unsupported expectation '{kind}'"

    # -- evidence ---------------------------------------------------------

    @staticmethod
    def _build_evidence(
        capture: _Capture,
        redactor: CredentialRedactor,
        spec: ProbeSpec,
        final_url: str,
        title: str,
    ) -> List[Evidence]:
        """Turn captured observations into evidence records."""
        evidence: List[Evidence] = []
        for entry in capture.console_errors:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.CONSOLE_ERROR,
                    summary=redactor.redact(entry["text"])[:200],
                    content=redactor.redact(entry["text"]),
                    source="browser_console",
                    trust=TrustLevel.EXTERNAL,
                    metadata={"url": entry["url"]},
                )
            )
        for message in capture.page_errors:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.CONSOLE_ERROR,
                    summary=redactor.redact(message)[:200],
                    content=redactor.redact(message),
                    source="browser_pageerror",
                    trust=TrustLevel.EXTERNAL,
                )
            )
        for entry in capture.failed_requests:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.NETWORK_FAILURE,
                    summary=(
                        f"{entry['method']} {entry['url']} failed: {entry['reason']}"
                    ),
                    source="browser_network",
                    trust=TrustLevel.EXTERNAL,
                    metadata=entry,
                )
            )
        for entry in capture.http_errors:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.HTTP_ERROR,
                    summary=f"HTTP {entry['status']} for {entry['url']}",
                    source="browser_network",
                    trust=TrustLevel.EXTERNAL,
                    metadata=entry,
                )
            )
        return evidence

    @staticmethod
    def _artifact_dir(
        evidence_dir: Optional[str], probe_id: str, run_id: str
    ) -> Optional[Path]:
        if not evidence_dir:
            return None
        path = Path(evidence_dir) / probe_id / run_id
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Could not create evidence directory %s", path)
            return None
        return path

    @staticmethod
    def _screenshot(page: Any, artifact_dir: Path) -> str:
        path = artifact_dir / "failure.png"
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            logger.warning("Could not capture a screenshot", exc_info=True)
            return ""
        return str(path)
