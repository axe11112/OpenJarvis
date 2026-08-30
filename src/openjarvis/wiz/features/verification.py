"""Checking the contract against the deployed preview, in a real browser.

This is the stage that decides whether a feature is finished. Everything before
it — the plan, the diff, the agent's account of its own work — is input. What
happens here is the output: the acceptance contract compiled into browser
workflows, run against the preview built from this exact commit, at a desktop
size and a phone size, with the console and the network watched.

Three rules shape the design.

**A criterion nobody checked did not pass.** The result distinguishes checked,
failed and *unchecked*, and a feature with unchecked criteria is not verified.
The tempting alternative — treat "no probe covered this" as "nothing went
wrong" — produces a verifier that reports success most reliably when it is
broken.

**The evidence is for the next attempt, not for a report.** When verification
fails, :meth:`FeatureVerification.evidence` renders exactly what went wrong into
prose a coding agent can act on: the criterion, the selector, the console error,
the failing request. §16 of the brief asks for the loop to be fed exact
evidence, and vague evidence is what makes a second attempt repeat the first.

**These checks are temporary.** They run against one deployment and are then
gone. They are never written into the probe directory and never registered with
the production scheduler; §15 says so, and the failure otherwise is a probe
suite that grows by one brittle entry per feature until nobody reads any of it.
:attr:`FeatureVerification.registered_probes` is always empty, and a test says
so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from openjarvis.reliability.types import EvidenceKind, ProbeResult
from openjarvis.wiz.features.acceptance import (
    AcceptanceContract,
    Criterion,
    Viewport,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BrowserUnavailable",
    "CriterionOutcome",
    "FeatureVerification",
    "FeatureVerifier",
    "gate_outcome",
]


class BrowserUnavailable(RuntimeError):
    """No browser is installed, so no user-interface claim can be checked."""


@dataclass(slots=True)
class CriterionOutcome:
    """What happened to one criterion."""

    criterion: Criterion

    #: ``True`` passed, ``False`` failed, ``None`` nobody checked it.
    passed: Optional[bool] = None

    viewport: str = ""
    detail: str = ""

    @property
    def checked(self) -> bool:
        return self.passed is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.criterion.kind,
            "description": self.criterion.description,
            "viewport": self.viewport,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(slots=True)
class FeatureVerification:
    """The verdict on one attempt, and everything it rests on."""

    feature_id: str
    preview_url: str = ""
    commit_sha: str = ""
    deployment_id: str = ""

    outcomes: List[CriterionOutcome] = field(default_factory=list)
    probe_results: List[ProbeResult] = field(default_factory=list)

    #: Paths to screenshots, per viewport. Shown in the Control Center and
    #: attached to the pull request.
    screenshots: Dict[str, List[str]] = field(default_factory=dict)
    traces: List[str] = field(default_factory=list)

    #: Browser acceptance results, bound to exact feature SHA and deployment.
    #: Format: {criterion_name: {sha, deployment_id, passed, evidence}}
    browser_acceptance: Dict[str, Any] = field(default_factory=dict)

    #: Criteria that need a person. Never counted as passed.
    awaiting_a_person: List[str] = field(default_factory=list)

    #: Always empty. Present so the promise is a value that can be asserted on
    #: rather than a sentence in a docstring.
    registered_probes: List[str] = field(default_factory=list)

    error: str = ""

    @property
    def failed(self) -> List[CriterionOutcome]:
        return [o for o in self.outcomes if o.passed is False]

    @property
    def unchecked(self) -> List[CriterionOutcome]:
        """Nothing in the compiled probe covered this, and it genuinely
        could have. Excludes a VIEWPORT criterion with no selector (see
        :attr:`~.acceptance.Criterion.is_an_unmeasured_layout_check`): that
        one is not a probe that failed to cover something it should have —
        it is exactly what :func:`~.acceptance.contract_for` generates on
        purpose, with no automatic overflow measurement yet to give it
        something to assert — and is reported through
        :attr:`awaiting_a_person` instead, exactly like a MANUAL criterion.
        Every other uncompilable shape (a CONTENT criterion with neither
        text nor selector, say) still counts here: that criterion has
        nothing wrong with the *system*, only with *it*, which is a real gap
        worth reporting as a failure, not forgiving.
        """
        return [
            o
            for o in self.outcomes
            if o.passed is None and not o.criterion.is_an_unmeasured_layout_check
        ]

    @property
    def passed(self) -> bool:
        """Whether every criterion this system can check, checked out.

        A genuinely unchecked criterion still counts against this: "no probe
        covered this" is not evidence that the criterion holds, and a
        verifier that treats it as such succeeds most reliably when it is
        broken. A structurally uncheckable one does not — see
        :attr:`unchecked` — because "cannot be checked" was never "does not
        hold" to begin with; :attr:`complete` is what still gates it behind a
        person via :attr:`awaiting_a_person`.
        """
        if self.error:
            return False
        if not self.outcomes:
            return False
        return not self.failed and not self.unchecked

    @property
    def complete(self) -> bool:
        """Whether the feature may reach READY without a person looking."""
        return self.passed and not self.awaiting_a_person

    def summary(self) -> str:
        """One line, in the operator's language."""
        if self.error:
            return f"I could not check the preview: {self.error}"
        checked = [o for o in self.outcomes if o.checked]
        failed = self.failed
        if failed:
            return f"{len(failed)} of {len(checked)} checks failed on the preview"
        if self.unchecked:
            return f"{len(self.unchecked)} thing(s) I promised to check, I could not"
        if self.awaiting_a_person:
            return (
                f"everything I can check passed; {len(self.awaiting_a_person)} "
                "thing(s) still need you to look"
            )
        return f"all {len(checked)} checks passed on the preview"

    def evidence(self, *, max_chars: int = 6000) -> str:
        """What the next Claude attempt is told.

        Deliberately concrete. "The mobile check failed" produces a second
        attempt that guesses; "at 390x844, /coach/summary does not contain
        'Download report', and the console logged: TypeError: t.map is not a
        function" produces one that fixes the bug.
        """
        if self.error:
            return f"Verification could not run: {self.error}"

        lines: List[str] = []
        for outcome in self.failed:
            where = f" at {outcome.viewport}" if outcome.viewport else ""
            lines.append(f"- FAILED{where}: {outcome.criterion.description}")
            if outcome.detail:
                lines.append(f"    {outcome.detail}")

        for outcome in self.unchecked:
            lines.append(
                f"- NOT CHECKED: {outcome.criterion.description} "
                "(nothing in the contract compiled to a check for this; the "
                "element may need a stable selector)"
            )

        observed = self._observed_evidence()
        if observed:
            lines.append("\nWhat the browser saw:")
            lines.extend(f"- {item}" for item in observed)

        if self.preview_url:
            lines.append(f"\nPreview checked: {self.preview_url}")

        rendered = "\n".join(lines)
        if len(rendered) > max_chars:
            return rendered[:max_chars] + "\n... (truncated)"
        return rendered

    def _observed_evidence(self) -> List[str]:
        """Console errors and failed requests, deduplicated across probes.

        Untrusted: every string here came from a web page. It is quoted into a
        prompt for a coding agent, so it is truncated and never interpreted.
        """
        seen: List[str] = []
        interesting = {
            EvidenceKind.CONSOLE_ERROR,
            EvidenceKind.NETWORK_FAILURE,
            EvidenceKind.HTTP_ERROR,
        }
        for result in self.probe_results:
            for item in result.evidence:
                if item.kind not in interesting:
                    continue
                line = f"{item.kind.value}: {item.summary}"[:300]
                if line not in seen:
                    seen.append(line)
        return seen[:20]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "preview_url": self.preview_url,
            "commit_sha": self.commit_sha,
            "deployment_id": self.deployment_id,
            "passed": self.passed,
            "complete": self.complete,
            "summary": self.summary(),
            "outcomes": [o.to_dict() for o in self.outcomes],
            "screenshots": {k: list(v) for k, v in self.screenshots.items()},
            "traces": list(self.traces),
            "browser_acceptance": dict(self.browser_acceptance),
            "awaiting_a_person": list(self.awaiting_a_person),
            "registered_probes": list(self.registered_probes),
            "error": self.error,
        }


@dataclass
class FeatureVerifier:
    """Runs a contract against a preview.

    Parameters
    ----------
    runner_factory:
        Builds a browser probe runner for a given viewport. Injected so tests
        never launch Chromium and so the viewport — which is a constructor
        argument on the real runner — can differ per pass.
    evidence_root:
        Where screenshots and traces are written. One directory per feature per
        attempt, so the operator can find the pictures of attempt 2 without
        them having overwritten attempt 1.
    """

    runner_factory: Callable[[Viewport], Any]
    evidence_root: Optional[Path] = None

    def verify(
        self,
        contract: AcceptanceContract,
        *,
        preview_url: str,
        commit_sha: str = "",
        deployment_id: str = "",
        attempt: int = 1,
        gate_outcomes: Sequence[CriterionOutcome] = (),
    ) -> FeatureVerification:
        """Check *contract* against the deployment at *preview_url*.

        *gate_outcomes* carries the verdicts already reached locally — the
        target's own tests, types and build. They are part of the contract and
        so part of the verdict; they are passed in rather than re-run here
        because they were run in the worktree, before anything was deployed.
        """
        verification = FeatureVerification(
            feature_id=contract.feature_id,
            preview_url=preview_url,
            commit_sha=commit_sha,
            deployment_id=deployment_id,
        )

        if not preview_url:
            verification.error = "there is no preview to check"
            return verification

        verification.outcomes.extend(gate_outcomes)
        verification.awaiting_a_person.extend(contract.unmet_without_a_person())

        browser_criteria = contract.browser_criteria
        if not browser_criteria:
            # Nothing in a browser to check. Legitimate for a backend change,
            # and the gate outcomes still decide the verdict.
            return verification

        specs = contract.probe_specs()
        if not specs:
            # Criteria exist but none compiled. Every one of them is unchecked,
            # which is a failure rather than a quiet pass.
            for criterion in browser_criteria:
                verification.outcomes.append(CriterionOutcome(criterion=criterion))
            return verification

        covered: List[Tuple[Criterion, str, bool, str]] = []
        for viewport, spec in specs:
            result = self._run(spec, viewport, preview_url, contract, attempt)
            if result is None:
                continue
            verification.probe_results.append(result)
            self._collect_artifacts(verification, viewport, result)

            applicable = [
                c
                for c in browser_criteria
                if c.applies_to(viewport) and (c.route or "/") == (spec.steps[0].url)
            ]
            covered.extend(self._attribute(spec, result, applicable, viewport.name))

        if not verification.probe_results and not verification.error:
            verification.error = "no browser check could be run against the preview"

        checked_criteria = {id(c) for c, _, _, _ in covered}
        for criterion, viewport_name, ok, detail in covered:
            verification.outcomes.append(
                CriterionOutcome(
                    criterion=criterion,
                    passed=ok,
                    viewport=viewport_name,
                    detail=detail,
                )
            )
        for criterion in browser_criteria:
            if id(criterion) not in checked_criteria:
                verification.outcomes.append(CriterionOutcome(criterion=criterion))
                if criterion.is_an_unmeasured_layout_check:
                    # Not a probe that missed its coverage — this specific,
                    # deliberately-generated shape has no automatic overflow
                    # measurement yet. Reported the same way a MANUAL
                    # criterion is, so it gates completion without reading
                    # as a failure.
                    verification.awaiting_a_person.append(criterion.description)

        return verification

    # -- plumbing ----------------------------------------------------------

    def _run(
        self,
        spec: Any,
        viewport: Viewport,
        preview_url: str,
        contract: AcceptanceContract,
        attempt: int,
    ) -> Optional[ProbeResult]:
        try:
            runner = self.runner_factory(viewport)
        except Exception as exc:
            raise BrowserUnavailable(
                f"a browser is needed to check this feature: {exc}"
            ) from exc

        evidence_dir = self._evidence_dir(contract.feature_id, attempt, viewport)
        try:
            return runner.run(
                spec,
                base_url=preview_url,
                evidence_dir=str(evidence_dir) if evidence_dir else None,
            )
        except Exception as exc:
            # One probe failing to *run* is not the same as the feature failing.
            # Logged and skipped; the criteria it covered stay unchecked, which
            # keeps the feature out of READY.
            logger.warning("feature probe %s could not run: %s", spec.id, exc)
            return None

    @staticmethod
    def _attribute(
        spec: Any,
        result: ProbeResult,
        applicable: Sequence[Criterion],
        viewport_name: str,
    ) -> List[Tuple[Criterion, str, bool, str]]:
        """Which of *applicable* actually passed, failed, or was never checked.

        A spec bundles every criterion sharing a route and viewport into one
        browser run (:meth:`AcceptanceContract._spec_for_route`), so the run's
        own single pass/fail is not one criterion's verdict — it is whether
        *everything* in the bundle held. Reading it as though it were let one
        real content mismatch report a healthy console and an untouched
        viewport as failed for the exact same reason.

        Per-criterion when the runner reported per-expectation/per-assertion
        detail (see ``BrowserProbeRunner.run``'s ``expectation_outcomes`` /
        ``assertion_outcomes`` metadata) and the compiled spec recorded which
        criterion asked for each one (``expectation_owners`` /
        ``assertion_owners``, set by :meth:`AcceptanceContract._spec_for_route`).
        A criterion neither list mentions compiled to no actual check — a
        selector-less layout criterion, say — and is left for the caller's own
        "not in checked_criteria" pass to report as unchecked, not as passed
        or failed by association.

        Falls back to the run's single verdict, exactly as before, in the two
        cases where per-criterion detail either does not exist or would be
        wrong to invent: a runner with no per-check metadata at all (a test
        double, or any runner predating this), and a hard navigation failure
        (steps never completed, so nothing was individually evaluated — the
        whole page failing to load is one real, shared cause, not several
        checks contaminating each other).
        """
        metadata = result.metadata or {}
        if metadata.get("navigation_error"):
            return [
                (criterion, viewport_name, False, metadata["navigation_error"])
                for criterion in applicable
            ]

        expectation_outcomes = metadata.get("expectation_outcomes")
        assertion_outcomes = metadata.get("assertion_outcomes")
        if expectation_outcomes is None and assertion_outcomes is None:
            return [
                (
                    criterion,
                    viewport_name,
                    result.success,
                    "" if result.success else result.error,
                )
                for criterion in applicable
            ]

        expectation_owners = spec.metadata.get("expectation_owners", [])
        assertion_owners = spec.metadata.get("assertion_owners", {})

        by_owner: Dict[int, List[Tuple[bool, str]]] = {}
        for owner_id, outcome in zip(expectation_owners, expectation_outcomes or []):
            by_owner.setdefault(owner_id, []).append(
                (bool(outcome["passed"]), str(outcome["detail"]))
            )
        for name, owner_id in assertion_owners.items():
            outcome = (assertion_outcomes or {}).get(name)
            if outcome is None:
                continue
            by_owner.setdefault(owner_id, []).append(
                (bool(outcome["passed"]), str(outcome["detail"]))
            )

        attributed: List[Tuple[Criterion, str, bool, str]] = []
        for criterion in applicable:
            owned = by_owner.get(id(criterion))
            if owned is None:
                continue
            ok = all(passed for passed, _ in owned)
            detail = "; ".join(detail for passed, detail in owned if not passed and detail)
            attributed.append((criterion, viewport_name, ok, detail))
        return attributed

    def _evidence_dir(
        self, feature_id: str, attempt: int, viewport: Viewport
    ) -> Optional[Path]:
        if self.evidence_root is None:
            return None
        path = (
            Path(self.evidence_root) / feature_id / f"attempt-{attempt}" / viewport.name
        )
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("could not create %s: %s", path, exc)
            return None
        return path

    @staticmethod
    def _collect_artifacts(
        verification: FeatureVerification, viewport: Viewport, result: ProbeResult
    ) -> None:
        for item in result.evidence:
            if item.kind is EvidenceKind.SCREENSHOT and item.artifact_path:
                verification.screenshots.setdefault(viewport.name, []).append(
                    item.artifact_path
                )
            elif item.kind is EvidenceKind.TRACE and item.artifact_path:
                verification.traces.append(item.artifact_path)


def gate_outcome(name: str, passed: bool, detail: str = "") -> CriterionOutcome:
    """Turn a local check's verdict into a criterion outcome.

    Lives here rather than in the check runner so that the reliability package
    keeps knowing nothing about acceptance contracts.
    """
    from openjarvis.wiz.features.acceptance import GATE

    return CriterionOutcome(
        criterion=Criterion(
            kind=GATE, name=name, description=f"the project's {name} check passes"
        ),
        passed=passed,
        detail=detail,
    )
