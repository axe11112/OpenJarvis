"""Independent verification.

This is the architectural centre of JARVIS. Everything else is plumbing.

The rule: **the coding agent's claim that it fixed something is never evidence.**
Verification re-runs *the exact probe spec that opened the incident* against a
freshly built preview deployment, and compares observed behaviour with the
spec's declared expectations. No model is consulted, and no model output can
influence the verdict — which is also why injected content cannot talk its way
to RESOLVED.

If the probe that detected the failure now passes, the failure is fixed. If it
does not, it is not, whatever anyone claims.
"""

from __future__ import annotations

import logging
from typing import Optional

from openjarvis.reliability.probes.executor import ProbeExecutor
from openjarvis.reliability.probes.spec import ProbeSpec
from openjarvis.reliability.types import (
    Evidence,
    EvidenceKind,
    TrustLevel,
    VerificationResult,
    now_iso,
)

logger = logging.getLogger(__name__)

__all__ = ["Verifier"]


class Verifier:
    """Re-runs a probe against a candidate deployment and judges the result.

    Parameters
    ----------
    evidence_dir:
        Where failure artifacts from verification runs are written.
    executor_factory:
        Builds a :class:`ProbeExecutor` bound to a given base URL.  Injected so
        tests can verify without a browser.
    """

    def __init__(
        self,
        *,
        evidence_dir: str = "",
        executor_factory=None,
    ) -> None:
        self._evidence_dir = evidence_dir
        self._executor_factory = executor_factory or self._default_factory

    def _default_factory(self, base_url: str) -> ProbeExecutor:
        return ProbeExecutor(base_url=base_url, evidence_dir=self._evidence_dir)

    def verify(
        self,
        spec: ProbeSpec,
        *,
        target_url: str,
        incident_id: str = "",
    ) -> VerificationResult:
        """Run *spec* against *target_url* and return a verdict.

        A failure to run the probe at all counts as FAIL, not as an error: an
        unverifiable repair must never be treated as verified.
        """
        if not target_url:
            return VerificationResult(
                passed=False,
                probe_id=spec.id,
                expected=spec.expectation_summary(),
                actual="no deployment was available to verify against",
                notes=(
                    "Verification could not run. A repair that cannot be "
                    "verified is not a verified repair."
                ),
                checked_at=now_iso(),
            )

        executor = self._executor_factory(target_url)
        try:
            result = executor.run(spec)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("verification of %s raised", spec.id)
            return VerificationResult(
                passed=False,
                probe_id=spec.id,
                target_url=target_url,
                expected=spec.expectation_summary(),
                actual=f"verification could not run: {type(exc).__name__}: {exc}",
                checked_at=now_iso(),
            )

        if result.success:
            return VerificationResult(
                passed=True,
                probe_id=spec.id,
                target_url=target_url,
                expected=spec.expectation_summary(),
                actual="the workflow completed and every expectation held",
                notes=(
                    f"Re-ran probe '{spec.id}' against the candidate deployment; "
                    f"{result.steps_completed} step(s) completed in "
                    f"{result.duration_seconds:.2f}s."
                ),
                checked_at=now_iso(),
            )

        evidence = list(result.evidence)
        evidence.append(
            Evidence(
                kind=EvidenceKind.NOTE,
                summary="Verification failed",
                content=(
                    f"Expected: {spec.expectation_summary()}\n"
                    f"Observed: {result.error}\n"
                    f"Failure kind: {result.failure_kind}\n"
                    f"Final URL: {result.final_url}\n"
                    f"Steps completed: {result.steps_completed}"
                ),
                source="verification",
                trust=TrustLevel.TRUSTED,
            )
        )

        return VerificationResult(
            passed=False,
            probe_id=spec.id,
            target_url=target_url,
            expected=spec.expectation_summary(),
            actual=result.error or f"probe failed ({result.failure_kind})",
            failure_kind=result.failure_kind,
            notes=(
                f"Re-ran probe '{spec.id}' against the candidate deployment and "
                "the original failure still reproduces."
            ),
            evidence=evidence,
            checked_at=now_iso(),
        )

    @staticmethod
    def summarize_for_retry(verification: Optional[VerificationResult]) -> str:
        """Render a failed verification as feedback for the next attempt.

        This is what turns attempt n+1 into a *better* attempt rather than the
        same one again.
        """
        if verification is None:
            return ""
        lines = [
            f"Probe: {verification.probe_id}",
            f"Verified against: {verification.target_url or 'n/a'}",
            f"Expected: {verification.expected}",
            f"Observed: {verification.actual}",
        ]
        if verification.notes:
            lines.append(f"Notes: {verification.notes}")
        for item in verification.evidence[:8]:
            body = item.content or item.summary
            if body:
                lines.append(f"[{item.kind.value}] {body[:600]}")
        return "\n".join(lines)
