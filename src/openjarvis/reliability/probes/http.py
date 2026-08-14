"""HTTP probe — a cheap status/latency/content check.

Answers "is it responding, and with what?" without paying for a browser.  Used
for uptime checks and API endpoints; use the browser runner for anything that
needs JavaScript.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, List, Optional

import httpx

from openjarvis.reliability.probes._stubs import (
    BaseProbeRunner,
    CredentialRedactor,
    ProbeRunnerRegistry,
)
from openjarvis.reliability.probes.spec import ProbeSpec
from openjarvis.reliability.types import (
    Evidence,
    EvidenceKind,
    ProbeResult,
    TrustLevel,
    now_iso,
)

logger = logging.getLogger(__name__)

__all__ = ["HttpProbeRunner"]

#: Cap on captured body text, so a huge response cannot bloat an incident.
_MAX_BODY_CHARS = 8000

_MAX_REDIRECTS = 5


def resolve_url(base_url: str, target: str) -> str:
    """Join a probe's target onto the configured base URL.

    Absolute targets are used as-is so a probe can check a third-party
    dependency; relative targets are appended to the site's base URL.
    """
    if target.startswith(("http://", "https://")):
        return target
    if not base_url:
        return target
    return f"{base_url.rstrip('/')}/{target.lstrip('/')}"


@ProbeRunnerRegistry.register("http")
class HttpProbeRunner(BaseProbeRunner):
    """Runs an HTTP probe with SSRF protection and bounded response reading."""

    runner_id = "http"

    def __init__(self, *, verify_ssrf: bool = True) -> None:
        self._verify_ssrf = verify_ssrf

    def run(
        self,
        spec: ProbeSpec,
        *,
        base_url: str = "",
        evidence_dir: Optional[str] = None,
        **options: Any,
    ) -> ProbeResult:
        """Issue the request and evaluate the spec's expectations."""
        started = time.monotonic()
        started_at = now_iso()
        url = resolve_url(base_url, spec.url)
        redactor = CredentialRedactor()
        evidence: List[Evidence] = []

        if self._verify_ssrf:
            blocked = self._ssrf_reason(url)
            if blocked:
                return ProbeResult(
                    probe_id=spec.id,
                    success=False,
                    failure_kind="blocked",
                    error=blocked,
                    started_at=started_at,
                    final_url=url,
                )

        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=spec.timeout_ms / 1000.0,
                max_redirects=_MAX_REDIRECTS,
            ) as client:
                response = client.request(spec.method, url)
        except httpx.TimeoutException as exc:
            return ProbeResult(
                probe_id=spec.id,
                success=False,
                failure_kind="timeout",
                error=redactor.redact(
                    f"Request timed out after {spec.timeout_ms}ms: {exc}"
                ),
                duration_seconds=time.monotonic() - started,
                final_url=url,
                started_at=started_at,
            )
        except httpx.HTTPError as exc:
            return ProbeResult(
                probe_id=spec.id,
                success=False,
                failure_kind="network_failure",
                error=redactor.redact(f"{type(exc).__name__}: {exc}"),
                duration_seconds=time.monotonic() - started,
                final_url=url,
                started_at=started_at,
            )

        duration = time.monotonic() - started
        body = response.text[:_MAX_BODY_CHARS]
        final_url = str(response.url)

        failures: List[str] = []

        # Cross-cutting assertions
        ceiling = spec.assertions.max_http_status
        if ceiling and response.status_code > ceiling:
            failures.append(
                f"HTTP {response.status_code} exceeds the allowed maximum {ceiling}"
            )
            evidence.append(
                Evidence(
                    kind=EvidenceKind.HTTP_ERROR,
                    summary=f"HTTP {response.status_code} for {final_url}",
                    content=body,
                    source="http_probe",
                    trust=TrustLevel.EXTERNAL,
                    metadata={"status": response.status_code, "url": final_url},
                )
            )
        if (
            spec.assertions.max_duration_seconds
            and duration > spec.assertions.max_duration_seconds
        ):
            failures.append(
                f"took {duration:.2f}s, over the "
                f"{spec.assertions.max_duration_seconds:.2f}s budget"
            )

        # Declared expectations
        for expectation in spec.expect:
            problem = self._check(expectation, response, final_url, body)
            if problem:
                failures.append(problem)

        if final_url != url and not any(e.kind == "url" for e in spec.expect):
            evidence.append(
                Evidence(
                    kind=EvidenceKind.UNEXPECTED_REDIRECT,
                    summary=f"{url} redirected to {final_url}",
                    source="http_probe",
                    trust=TrustLevel.TRUSTED,
                )
            )

        return ProbeResult(
            probe_id=spec.id,
            success=not failures,
            failure_kind="assertion" if failures else "",
            error=redactor.redact("; ".join(failures)),
            duration_seconds=duration,
            final_url=final_url,
            http_status=response.status_code,
            steps_completed=1,
            evidence=[redactor.redact_evidence(item) for item in evidence],
            started_at=started_at,
            metadata={"method": spec.method, "requested_url": url},
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _ssrf_reason(url: str) -> str:
        """Return a refusal reason when *url* fails the SSRF check, else ''.

        ``check_ssrf`` returns ``None`` when the URL is allowed and a reason
        string when it is not.
        """
        from openjarvis.security.ssrf import check_ssrf

        return check_ssrf(url) or ""

    @staticmethod
    def _check(
        expectation: Any,
        response: httpx.Response,
        final_url: str,
        body: str,
    ) -> str:
        """Return a failure description, or '' when the expectation holds."""
        kind = expectation.kind
        if kind == "status":
            if str(response.status_code) != expectation.matches:
                return (
                    f"expected HTTP {expectation.matches}, got {response.status_code}"
                )
            return ""
        if kind == "url":
            if not re.search(expectation.matches, final_url):
                return (
                    f"expected the URL to match {expectation.matches}, got {final_url}"
                )
            return ""
        if kind == "text":
            if expectation.value not in body:
                return f"expected the body to contain {expectation.value!r}"
            return ""
        if kind == "not_text":
            if expectation.value in body:
                return f"expected the body not to contain {expectation.value!r}"
            return ""
        if kind == "title":
            match = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.S)
            title = match.group(1).strip() if match else ""
            if expectation.matches not in title:
                return (
                    f"expected the title to contain {expectation.matches!r}, "
                    f"got {title!r}"
                )
            return ""
        # visible/hidden need a real browser
        return f"expectation '{kind}' is not supported by the http runner"
