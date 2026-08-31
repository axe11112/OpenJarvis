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
    resolve_headers,
)
from openjarvis.reliability.probes.access import resolve_access_headers
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


def _normalize_rendered_text(text: str) -> str:
    """Collapse incidental whitespace formatting, the way a reader would.

    ``page.inner_text()`` is already the right primitive for "what text is
    actually visible" — confirmed against a real browser: it excludes
    ``display:none`` and ``visibility:hidden`` content, and correctly
    *includes* ``aria-hidden`` content that is still rendered (``aria-hidden``
    is an accessibility-tree signal, not a visual one, so a "visible text"
    check is right to still see it). What it does not do is agree with a
    hand-written expected string on how many spaces or line breaks sit
    between words: a heading split across sibling elements by an
    absolutely-positioned ``sr-only`` spacer — a legitimate, common pattern
    for exactly the missing-space accessibility bug FEAT-00030's own second
    attempt fixed — comes back from ``inner_text()`` with a line break where
    the DOM had a plain space, purely because the browser's own line-box
    computation treats the out-of-flow spacer as splitting the line, not
    because any text is missing or hidden. Collapsing runs of whitespace
    (including line breaks) to a single space, on both sides of the
    comparison, matches how :meth:`Page.get_by_text`'s own default matching
    already behaves, and is exactly and only a whitespace normalization —
    it changes nothing about which *words* are present, their order, or
    whether they are visible at all.
    """
    return re.sub(r"\s+", " ", text).strip()


def _first_target_url(spec: ProbeSpec, base_url: str) -> str:
    """The URL this run will actually open.

    Access profiles key off the deployment under test, not off the spec, so
    this has to be the resolved target: ``--base-url`` and the verifier's
    candidate deployment are exactly the cases that matter.
    """
    for step in spec.steps:
        if step.action == "goto" and step.url:
            return resolve_url(base_url, step.url)
    return base_url


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
    locale:
        Browser locale, and the default ``Accept-Language`` sent with every
        request. Deliberately not the machine's own locale: a probe run
        against a multi-language site must check the same language every
        time regardless of what happens to be installed on whichever host
        runs it, and a target that reads ``Accept-Language`` (rather than a
        cookie or query param) to pick a language falls back to whatever it
        considers a "no preference" default otherwise — silently checking a
        different page than the one every other assertion was written
        against. A spec's own ``headers``/``headers_from_env`` still wins:
        this only fills in ``Accept-Language`` when the spec did not ask for
        a particular one itself.
    """

    runner_id = "browser"

    def __init__(
        self,
        *,
        headless: bool = True,
        executable_path: str = "",
        viewport: tuple[int, int] = (1280, 720),
        locale: str = "en-US",
    ) -> None:
        self._headless = headless
        self._executable_path = executable_path or os.environ.get(
            BROWSER_EXECUTABLE_ENV, ""
        )
        self._viewport = viewport
        self._locale = locale

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
        headers, header_secrets = resolve_headers(spec)
        # Access profiles resolve against the URL actually under test, so the
        # same spec needs no secret when pointed at production and picks one up
        # automatically when pointed at a protected preview.
        access_headers, access_secrets = resolve_access_headers(
            spec.access_profiles, _first_target_url(spec, base_url)
        )
        headers.update(access_headers)
        header_secrets.update(access_secrets)
        # Chromium's own locale-driven Accept-Language wins over
        # set_extra_http_headers for this specific header on navigation
        # requests, so the two must never both apply: pass `locale` to the
        # context only when the spec left Accept-Language unset, and let an
        # explicit spec header carry the whole thing otherwise.
        spec_set_language = any(k.lower() == "accept-language" for k in headers)
        if self._locale and not spec_set_language:
            base = self._locale.split("-")[0]
            headers["Accept-Language"] = (
                f"{self._locale},{base};q=0.9" if base != self._locale else self._locale
            )
        # Header secrets go into the redactor alongside credentials: a bypass
        # token travels on every request, so it is the value most likely to be
        # echoed back in an error page or a captured URL.
        redactor = CredentialRedactor({**credentials, **header_secrets})
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
                locale=None if spec_set_language else (self._locale or None),
            )
            context.set_default_timeout(spec.timeout_ms)
            if headers:
                # Context-level, so they ride on sub-resource and XHR requests
                # too, not just the top-level navigation. A deployment-
                # protection bypass has to cover every request the page makes
                # or the page loads and its assets 401.
                context.set_extra_http_headers(headers)
            if trace_path:
                with suppress(Exception):
                    context.tracing.start(screenshots=True, snapshots=True)
                    tracing_started = True

            page = context.new_page()
            page.on("console", capture.on_console)
            page.on("pageerror", capture.on_page_error)
            page.on("requestfailed", capture.on_request_failed)
            page.on("response", capture.on_response)

            captured_shots: List[tuple[str, str]] = []
            try:
                for index, step in enumerate(spec.steps):
                    shot = self._execute(
                        page, step, spec, base_url, credentials, artifact_dir, index
                    )
                    if shot:
                        captured_shots.append(shot)
                    steps_completed += 1
            except Exception as exc:
                failure_kind, error = self._classify(exc, redactor)
            finally:
                with suppress(Exception):
                    final_url = page.url
                with suppress(Exception):
                    page_title = page.title()

            failures: List[str] = []
            # Per-expectation/per-assertion outcomes, additive to `failures`
            # (which stays the single joined string every existing caller —
            # including production reliability probes with nothing to do with
            # feature acceptance — already reads). A spec bundles several
            # declared expectations and cross-cutting assertions into one
            # browser run; without this, a caller attributing results back to
            # the thing that asked for each check (see
            # ``openjarvis.wiz.features.verification``) has only the whole
            # run's pass/fail and one merged error string to work with, and
            # ends up reporting every check in the bundle as failed for
            # whichever one actually failed. Left empty when steps never
            # completed (`error` below) — nothing was evaluated, so there is
            # nothing per-check to report; the whole run failing for that
            # reason is not attribution, it is one real, shared cause.
            expectation_outcomes: List[Dict[str, Any]] = []
            assertion_outcomes: Dict[str, Dict[str, Any]] = {}
            if error:
                failures.append(error)
            else:
                for expectation in spec.expect:
                    problem = self._check(page, expectation, final_url, page_title)
                    detail = redactor.redact(problem) if problem else ""
                    if problem:
                        failures.append(detail)
                    expectation_outcomes.append(
                        {
                            "kind": expectation.kind,
                            "selector": expectation.selector,
                            "value": expectation.value,
                            "matches": expectation.matches,
                            "passed": not problem,
                            "detail": detail,
                        }
                    )
                if failures:
                    failure_kind = "assertion"

            duration = time.monotonic() - started

            # Cross-cutting assertions
            if spec.assertions.no_console_errors:
                count = len(capture.console_errors) + len(capture.page_errors)
                ok = count == 0
                detail = "" if ok else f"{count} JavaScript error(s) on the page"
                if not ok:
                    failures.append(detail)
                    failure_kind = failure_kind or "console_error"
                assertion_outcomes["console"] = {"passed": ok, "detail": detail}
            if spec.assertions.no_failed_requests:
                ok = not capture.failed_requests
                detail = (
                    ""
                    if ok
                    else f"{len(capture.failed_requests)} network request(s) failed"
                )
                if not ok:
                    failures.append(detail)
                    failure_kind = failure_kind or "network_failure"
                assertion_outcomes["network"] = {"passed": ok, "detail": detail}
            ceiling = spec.assertions.max_http_status
            if ceiling:
                over = [e for e in capture.http_errors if e["status"] > ceiling]
                ok = not over
                detail = (
                    ""
                    if ok
                    else f"{len(over)} response(s) exceeded HTTP {ceiling} "
                    f"(worst: {max(e['status'] for e in over)})"
                )
                if not ok:
                    failures.append(detail)
                    failure_kind = failure_kind or "http_error"
                assertion_outcomes["http_status"] = {"passed": ok, "detail": detail}
            if spec.assertions.max_duration_seconds:
                ok = duration <= spec.assertions.max_duration_seconds
                detail = (
                    ""
                    if ok
                    else f"took {duration:.2f}s, over the "
                    f"{spec.assertions.max_duration_seconds:.2f}s budget"
                )
                if not ok:
                    failures.append(detail)
                    failure_kind = failure_kind or "slow"
                assertion_outcomes["duration"] = {"passed": ok, "detail": detail}

            success = not failures
            evidence = self._build_evidence(
                capture, redactor, spec, final_url, page_title
            )

            for label, path in captured_shots:
                evidence.append(
                    Evidence(
                        kind=EvidenceKind.SCREENSHOT,
                        summary=redactor.redact(label)[:200],
                        artifact_path=path,
                        source="browser_probe",
                        trust=TrustLevel.TRUSTED,
                    )
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
                "navigation_error": error,
                "expectation_outcomes": expectation_outcomes,
                "assertion_outcomes": assertion_outcomes,
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
        artifact_dir: Optional[Path] = None,
        step_index: int = 0,
    ) -> Optional[tuple[str, str]]:
        """Perform one workflow step.

        Returns ``(label, path)`` when the step captured a screenshot, and
        ``None`` otherwise.
        """
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
            # An explicit screenshot step is taken whether or not the run goes
            # on to fail. Failure screenshots are automatic and are enough for
            # incident detection, but a probe that is *proving* something — a
            # feature's acceptance check, say — needs the picture of it working,
            # and there is nowhere later to get one from.
            if artifact_dir is None:
                return None
            name = re.sub(r"[^a-z0-9]+", "-", (step.label or "step").lower()).strip("-")
            path = artifact_dir / f"{step_index:02d}-{name or 'screenshot'}.png"
            try:
                page.screenshot(path=str(path), full_page=True)
            except Exception:
                logger.warning(
                    "Could not capture a requested screenshot", exc_info=True
                )
                return None
            return (step.label or "Screenshot", str(path))
        else:  # pragma: no cover - spec validation rejects these
            raise ValueError(f"Unsupported action '{action}'")
        return None

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
                # FEAT-00030: a heading split across sibling <span>s by an
                # absolutely-positioned sr-only spacer (a legitimate,
                # correct pattern — see _normalize_rendered_text's own
                # docstring) came back from inner_text() with a newline
                # where the DOM had a plain space, so a criterion written
                # as one line never matched — even though the text was
                # exactly right on screen, in both viewports, before and
                # after. inner_text() already excludes display:none and
                # visibility:hidden content and includes aria-hidden
                # content correctly (both are genuinely visible-text
                # questions, not whitespace ones) — this only collapses
                # incidental line-break/whitespace formatting the way a
                # person reading the rendered page would, not what counts
                # as visible at all.
                present = _normalize_rendered_text(
                    expectation.value
                ) in _normalize_rendered_text(content)
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
