"""Feature verification: what the preview actually showed."""

from __future__ import annotations

import pytest

from openjarvis.reliability.types import (
    Evidence,
    EvidenceKind,
    ProbeResult,
    TrustLevel,
)
from openjarvis.wiz.features.acceptance import (
    CONSOLE,
    CONTENT,
    DESKTOP,
    MANUAL,
    NETWORK,
    VIEWPORT,
    AcceptanceContract,
    Criterion,
    contract_for,
)
from openjarvis.wiz.features.verification import (
    BrowserUnavailable,
    FeatureVerifier,
    gate_outcome,
)


class FakeRunner:
    """A browser that returns scripted results and records what it was asked."""

    def __init__(self, *, success=True, error="", evidence=None, explode=False):
        self.success = success
        self.error = error
        self.evidence = evidence or []
        self.explode = explode
        self.runs = []

    def run(self, spec, *, base_url="", evidence_dir=None, **kwargs):
        self.runs.append((spec, base_url, evidence_dir))
        if self.explode:
            raise RuntimeError("the browser died")
        return ProbeResult(
            probe_id=spec.id,
            success=self.success,
            error=self.error,
            evidence=list(self.evidence),
            metadata={"viewport": spec.metadata.get("viewport", "")},
        )


class PageStateRunner:
    """A browser stand-in that evaluates a spec against a simulated page.

    Mirrors ``BrowserProbeRunner.run``'s per-expectation/per-assertion
    metadata (``expectation_outcomes``, ``assertion_outcomes``,
    ``navigation_error``) closely enough to exercise
    ``FeatureVerifier._attribute`` realistically, without a real browser.
    ``FakeRunner`` above deliberately does *not* do this — it exists to test
    the parts of verification that do not care about per-criterion
    attribution, and its absence of this metadata is itself what proves the
    fallback path still works.
    """

    def __init__(self, *, page_text="", console_ok=True, network_ok=True):
        self.page_text = page_text
        self.console_ok = console_ok
        self.network_ok = network_ok
        self.runs = []

    def run(self, spec, *, base_url="", evidence_dir=None, **kwargs):
        self.runs.append(spec)
        expectation_outcomes = []
        for expectation in spec.expect:
            present = expectation.value in self.page_text
            if expectation.kind == "text":
                passed = present
                detail = "" if passed else f"expected the page to contain {expectation.value!r}"
            elif expectation.kind == "not_text":
                passed = not present
                detail = (
                    ""
                    if passed
                    else f"expected the page not to contain {expectation.value!r}"
                )
            else:  # pragma: no cover - not exercised by these tests
                passed, detail = True, ""
            expectation_outcomes.append(
                {
                    "kind": expectation.kind,
                    "selector": expectation.selector,
                    "value": expectation.value,
                    "passed": passed,
                    "detail": detail,
                }
            )

        assertion_outcomes = {}
        if spec.assertions.no_console_errors:
            assertion_outcomes["console"] = {
                "passed": self.console_ok,
                "detail": "" if self.console_ok else "1 JavaScript error(s) on the page",
            }
        if spec.assertions.no_failed_requests:
            assertion_outcomes["network"] = {
                "passed": self.network_ok,
                "detail": "" if self.network_ok else "1 network request(s) failed",
            }

        failures = [o["detail"] for o in expectation_outcomes if not o["passed"]]
        failures += [o["detail"] for o in assertion_outcomes.values() if not o["passed"]]

        return ProbeResult(
            probe_id=spec.id,
            success=not failures,
            error="; ".join(failures),
            metadata={
                "viewport": spec.metadata.get("viewport", ""),
                "navigation_error": "",
                "expectation_outcomes": expectation_outcomes,
                "assertion_outcomes": assertion_outcomes,
            },
        )


def verifier(runner, **kwargs):
    return FeatureVerifier(runner_factory=lambda viewport: runner, **kwargs)


UI_CONTRACT_REQUEST = 'Add a "Download report" button to /coach/summary'


class TestVerdict:
    def test_a_passing_contract_verifies(self):
        contract = contract_for(feature_id="FEAT-1", request=UI_CONTRACT_REQUEST)
        result = verifier(FakeRunner()).verify(
            contract, preview_url="https://preview.app"
        )
        assert result.passed
        assert result.complete
        assert "checks passed" in result.summary()

    def test_a_failing_browser_check_fails_the_feature(self):
        contract = contract_for(feature_id="FEAT-1", request=UI_CONTRACT_REQUEST)
        result = verifier(
            FakeRunner(
                success=False,
                error="the page does not contain 'Download report'",
            )
        ).verify(contract, preview_url="https://preview.app")
        assert not result.passed
        assert result.failed
        assert "Download report" in result.evidence()

    def test_a_failing_gate_fails_the_feature_even_when_the_preview_is_fine(self):
        # The gates are part of the contract. A green preview does not excuse
        # a red test suite.
        contract = contract_for(
            feature_id="FEAT-1", request=UI_CONTRACT_REQUEST, gates=["tests"]
        )
        result = verifier(FakeRunner()).verify(
            contract,
            preview_url="https://preview.app",
            gate_outcomes=[gate_outcome("tests", False, "3 failing")],
        )
        assert not result.passed
        assert "3 failing" in result.evidence()

    def test_no_preview_is_not_a_pass(self):
        contract = contract_for(feature_id="FEAT-1", request=UI_CONTRACT_REQUEST)
        result = verifier(FakeRunner()).verify(contract, preview_url="")
        assert not result.passed
        assert "no preview" in result.summary()

    def test_an_empty_contract_never_verifies(self):
        # A contract that asks nothing cannot prove anything, and the honest
        # answer is "not verified" rather than "everything passed".
        contract = AcceptanceContract(feature_id="FEAT-1")
        result = verifier(FakeRunner()).verify(
            contract, preview_url="https://preview.app"
        )
        assert not result.passed


class TestUncheckedIsNotPassed:
    def test_a_criterion_no_probe_covered_keeps_the_feature_unverified(self):
        # The failure this exists to prevent: a verifier that reports success
        # most reliably when it is broken.
        contract = AcceptanceContract(
            feature_id="FEAT-2",
            criteria=(
                Criterion(
                    kind=CONTENT,
                    route="/a",
                    text="hello",
                    description="/a says hello",
                ),
                # Nothing compiles this: a content criterion with neither text
                # nor selector has nothing to assert.
                Criterion(kind=CONTENT, route="/b", description="/b looks right"),
            ),
        )
        result = verifier(FakeRunner()).verify(
            contract, preview_url="https://preview.app"
        )
        assert not result.passed
        assert result.unchecked
        assert "NOT CHECKED" in result.evidence()

    def test_a_manual_criterion_blocks_completion_but_not_the_checks(self):
        contract = AcceptanceContract(
            feature_id="FEAT-3",
            criteria=(
                Criterion(
                    kind=CONTENT, route="/a", text="hi", description="/a says hi"
                ),
                Criterion(kind=MANUAL, description="a person reads the wording"),
            ),
        )
        result = verifier(FakeRunner()).verify(
            contract, preview_url="https://preview.app"
        )
        assert result.passed
        assert not result.complete
        assert result.awaiting_a_person == ["a person reads the wording"]

    def test_a_probe_that_cannot_run_leaves_its_criteria_unchecked(self):
        contract = contract_for(feature_id="FEAT-4", request=UI_CONTRACT_REQUEST)
        result = verifier(FakeRunner(explode=True)).verify(
            contract, preview_url="https://preview.app"
        )
        assert not result.passed
        assert result.error or result.unchecked


class TestUnmeasuredLayoutChecksDoNotFailAlone:
    """A VIEWPORT criterion with no selector — exactly what contract_for()
    generates whenever a request names no specific element — has no
    automatic overflow measurement yet. It must gate completion like a
    MANUAL criterion, not read as a failed check: found on FEAT-00017, where
    every real, checkable criterion passed but the feature still reported
    as failed because of these two.
    """

    #: Request text with no quoted label, so contract_for() falls back to
    #: the selector-less VIEWPORT criteria rather than CONTENT ones.
    NO_LABEL_REQUEST = "Change one small piece of text on the landing page"

    def test_passing_checks_alongside_unmeasured_layout_criteria_still_pass(self):
        contract = contract_for(feature_id="FEAT-5", request=self.NO_LABEL_REQUEST)
        assert any(c.kind == VIEWPORT and not c.selector for c in contract.criteria)

        # PageStateRunner, not FakeRunner: it reports the same per-check
        # detail the real browser runner does, which is what lets an
        # unattributed VIEWPORT criterion fall through as genuinely
        # unchecked rather than inheriting a blanket pass/fail.
        result = verifier(PageStateRunner()).verify(
            contract, preview_url="https://preview.app"
        )

        assert result.passed, result.summary()

    def test_it_still_gates_completion_like_a_manual_criterion(self):
        contract = contract_for(feature_id="FEAT-5", request=self.NO_LABEL_REQUEST)
        result = verifier(PageStateRunner()).verify(
            contract, preview_url="https://preview.app"
        )
        assert result.passed
        assert not result.complete
        assert result.awaiting_a_person

    def test_a_genuine_content_failure_still_fails_alongside_it(self):
        # The forgiveness is narrow: a real, checkable failure right next to
        # an unmeasured layout criterion still fails the feature.
        contract = contract_for(feature_id="FEAT-5", request=self.NO_LABEL_REQUEST)
        result = verifier(PageStateRunner(console_ok=False)).verify(
            contract, preview_url="https://preview.app"
        )
        assert not result.passed

    def test_an_empty_content_criterion_is_not_forgiven_the_same_way(self):
        # Narrower than "anything uncompilable": a CONTENT criterion with
        # neither text nor selector has nothing wrong with the system, only
        # with itself, and must still count as a real, reportable gap.
        contract = AcceptanceContract(
            feature_id="FEAT-6",
            criteria=(
                Criterion(kind=CONTENT, route="/b", description="/b looks right"),
            ),
        )
        result = verifier(FakeRunner()).verify(
            contract, preview_url="https://preview.app"
        )
        assert not result.passed
        assert result.unchecked
        assert not result.awaiting_a_person


class TestPerCriterionAttribution:
    """One shared browser run must not let one failing criterion contaminate
    the others sharing its route and viewport — the FEAT-00017 bug: a content
    mismatch reported console, network and an untouched viewport check as
    failed too, all with the exact same detail text.
    """

    def _contract(self, *criteria):
        return AcceptanceContract(feature_id="FEAT-X", criteria=criteria, viewports=(DESKTOP,))

    def test_a_failed_content_check_does_not_fail_console_or_network(self):
        content = Criterion(
            kind=CONTENT, route="/", text="missing text", description="shows the text"
        )
        console = Criterion(kind=CONSOLE, route="/", description="no console errors")
        network = Criterion(kind=NETWORK, route="/", description="no failed requests")
        contract = self._contract(content, console, network)

        result = verifier(PageStateRunner(page_text="something else entirely")).verify(
            contract, preview_url="https://preview.app"
        )

        by_kind = {o.criterion.kind: o for o in result.outcomes}
        assert by_kind[CONTENT].passed is False
        assert by_kind[CONSOLE].passed is True
        assert by_kind[NETWORK].passed is True
        # Each outcome names only its own check, not the content mismatch.
        assert "missing text" in by_kind[CONTENT].detail
        assert by_kind[CONSOLE].detail == ""
        assert by_kind[NETWORK].detail == ""

    def test_a_failed_console_check_does_not_fail_a_passing_content_criterion(self):
        content = Criterion(
            kind=CONTENT, route="/", text="hello", description="shows hello"
        )
        console = Criterion(kind=CONSOLE, route="/", description="no console errors")
        contract = self._contract(content, console)

        result = verifier(
            PageStateRunner(page_text="hello world", console_ok=False)
        ).verify(contract, preview_url="https://preview.app")

        by_kind = {o.criterion.kind: o for o in result.outcomes}
        assert by_kind[CONTENT].passed is True
        assert by_kind[CONTENT].detail == ""
        assert by_kind[CONSOLE].passed is False
        assert "JavaScript error" in by_kind[CONSOLE].detail

    def test_replacement_does_not_require_old_and_new_text_simultaneously(self):
        # The self-contradictory-acceptance bug: a rewording change used to
        # need the page to contain BOTH the new text AND the exact old text
        # it replaced, which no correct implementation could ever satisfy.
        new_text = Criterion(
            kind=CONTENT, route="/", text="the new wording", description="new wording shows"
        )
        old_text = Criterion(
            kind=CONTENT,
            route="/",
            text="the old wording",
            expected="ABSENT",
            description="old wording is gone",
        )
        contract = self._contract(new_text, old_text)

        result = verifier(PageStateRunner(page_text="...the new wording...")).verify(
            contract, preview_url="https://preview.app"
        )

        assert result.passed, result.evidence()

    def test_replacement_can_assert_old_absent_and_new_present_independently(self):
        new_text = Criterion(
            kind=CONTENT, route="/", text="the new wording", description="new wording shows"
        )
        old_text = Criterion(
            kind=CONTENT,
            route="/",
            text="the old wording",
            expected="ABSENT",
            description="old wording is gone",
        )
        contract = self._contract(new_text, old_text)

        # The old wording was never actually removed: ABSENT must catch it
        # independently of whether the new text also happens to be there.
        result = verifier(
            PageStateRunner(page_text="the old wording, plus the new wording")
        ).verify(contract, preview_url="https://preview.app")

        by_text = {o.criterion.text: o for o in result.outcomes}
        assert by_text["the new wording"].passed is True
        assert by_text["the old wording"].passed is False

    def test_unchanged_text_preservation_still_works_when_requested(self):
        # A PRESENT criterion for text the change must leave alone still
        # works exactly as it always did.
        protected = Criterion(
            kind=CONTENT,
            route="/",
            text="unrelated protected heading",
            description="the protected heading is unchanged",
        )
        contract = self._contract(protected)

        still_there = verifier(
            PageStateRunner(page_text="unrelated protected heading, and more")
        ).verify(contract, preview_url="https://preview.app")
        assert still_there.passed

        accidentally_removed = verifier(PageStateRunner(page_text="")).verify(
            contract, preview_url="https://preview.app"
        )
        assert not accidentally_removed.passed

    def test_a_criterion_with_nothing_compiled_is_unchecked_not_contaminated(self):
        # A selector-less VIEWPORT criterion asserts nothing on its own (see
        # AcceptanceContract._spec_for_route); sharing a run with a real
        # failure must not turn "nothing checked" into "failed".
        from openjarvis.wiz.features.acceptance import VIEWPORT

        layout = Criterion(kind=VIEWPORT, route="/", description="renders without overflow")
        content = Criterion(
            kind=CONTENT, route="/", text="missing", description="shows missing text"
        )
        contract = self._contract(layout, content)

        result = verifier(PageStateRunner(page_text="not present here")).verify(
            contract, preview_url="https://preview.app"
        )

        by_kind = {o.criterion.kind: o for o in result.outcomes}
        assert by_kind[CONTENT].passed is False
        assert by_kind[VIEWPORT].passed is None
        assert by_kind[VIEWPORT].checked is False

    def test_a_runner_with_no_per_check_metadata_still_falls_back_correctly(self):
        # FakeRunner (used throughout this file) reports no per-expectation
        # detail at all -- older runners and every existing test double must
        # keep working exactly as before.
        content = Criterion(kind=CONTENT, route="/", text="x", description="d")
        console = Criterion(kind=CONSOLE, route="/", description="no console errors")
        contract = self._contract(content, console)

        result = verifier(FakeRunner(success=False, error="whole run failed")).verify(
            contract, preview_url="https://preview.app"
        )
        by_kind = {o.criterion.kind: o for o in result.outcomes}
        assert by_kind[CONTENT].passed is False
        assert by_kind[CONSOLE].passed is False
        assert by_kind[CONTENT].detail == "whole run failed"
        assert by_kind[CONSOLE].detail == "whole run failed"


class TestBothScreenSizes:
    def test_the_preview_is_checked_on_a_phone_as_well_as_a_desktop(self):
        runner = FakeRunner()
        contract = contract_for(feature_id="FEAT-5", request=UI_CONTRACT_REQUEST)
        verifier(runner).verify(contract, preview_url="https://preview.app")
        viewports = {spec.metadata["viewport"] for spec, _, _ in runner.runs}
        assert viewports == {"desktop", "mobile"}

    def test_every_run_targets_the_preview_and_nothing_else(self):
        runner = FakeRunner()
        contract = contract_for(feature_id="FEAT-5", request=UI_CONTRACT_REQUEST)
        verifier(runner).verify(contract, preview_url="https://preview.app")
        assert {base for _, base, _ in runner.runs} == {"https://preview.app"}


class TestArtifacts:
    def test_screenshots_are_filed_under_the_attempt_that_produced_them(self, tmp_path):
        # Attempt 2's pictures must not overwrite attempt 1's: comparing them is
        # how an operator sees what changed.
        runner = FakeRunner()
        contract = contract_for(feature_id="FEAT-6", request=UI_CONTRACT_REQUEST)
        verifier(runner, evidence_root=tmp_path).verify(
            contract, preview_url="https://preview.app", attempt=2
        )
        dirs = {directory for _, _, directory in runner.runs}
        assert all("attempt-2" in d for d in dirs)
        assert any("mobile" in d for d in dirs)

    def test_screenshots_are_collected_per_viewport(self, tmp_path):
        shot = Evidence(
            kind=EvidenceKind.SCREENSHOT,
            summary="/coach/summary",
            artifact_path=str(tmp_path / "a.png"),
            trust=TrustLevel.TRUSTED,
        )
        runner = FakeRunner(evidence=[shot])
        contract = contract_for(feature_id="FEAT-7", request=UI_CONTRACT_REQUEST)
        result = verifier(runner).verify(contract, preview_url="https://preview.app")
        assert set(result.screenshots) == {"desktop", "mobile"}


class TestTemporaryChecks:
    def test_a_feature_check_never_becomes_a_production_probe(self):
        # §15. The failure otherwise is a probe suite that grows by one brittle
        # entry per feature until nobody reads any of it.
        runner = FakeRunner()
        contract = contract_for(feature_id="FEAT-8", request=UI_CONTRACT_REQUEST)
        result = verifier(runner).verify(contract, preview_url="https://preview.app")
        assert result.registered_probes == []
        for spec, _, _ in runner.runs:
            assert spec.metadata["temporary"] is True

    def test_no_feature_check_is_allowed_to_write_data(self):
        runner = FakeRunner()
        contract = contract_for(feature_id="FEAT-9", request=UI_CONTRACT_REQUEST)
        verifier(runner).verify(contract, preview_url="https://preview.app")
        for spec, _, _ in runner.runs:
            assert spec.mutating is False


class TestEvidenceForTheNextAttempt:
    def test_console_errors_reach_the_next_attempt(self):
        console = Evidence(
            kind=EvidenceKind.CONSOLE_ERROR,
            summary="TypeError: t.map is not a function",
            trust=TrustLevel.EXTERNAL,
        )
        runner = FakeRunner(success=False, error="assertion failed", evidence=[console])
        contract = contract_for(feature_id="FEAT-10", request=UI_CONTRACT_REQUEST)
        result = verifier(runner).verify(contract, preview_url="https://preview.app")
        assert "t.map is not a function" in result.evidence()

    def test_the_evidence_names_the_preview_it_looked_at(self):
        runner = FakeRunner(success=False, error="nope")
        contract = contract_for(feature_id="FEAT-11", request=UI_CONTRACT_REQUEST)
        result = verifier(runner).verify(contract, preview_url="https://preview.app")
        assert "https://preview.app" in result.evidence()

    def test_evidence_is_bounded(self):
        noisy = [
            Evidence(
                kind=EvidenceKind.CONSOLE_ERROR,
                summary="x" * 500,
                trust=TrustLevel.EXTERNAL,
            )
            for _ in range(50)
        ]
        runner = FakeRunner(success=False, error="y" * 5000, evidence=noisy)
        contract = contract_for(feature_id="FEAT-12", request=UI_CONTRACT_REQUEST)
        result = verifier(runner).verify(contract, preview_url="https://preview.app")
        assert len(result.evidence(max_chars=2000)) <= 2100


class TestBrowserAvailability:
    def test_a_missing_browser_is_said_out_loud(self):
        def no_browser(viewport):
            raise RuntimeError("Playwright is not installed")

        contract = contract_for(feature_id="FEAT-13", request=UI_CONTRACT_REQUEST)
        with pytest.raises(BrowserUnavailable, match="Playwright"):
            FeatureVerifier(runner_factory=no_browser).verify(
                contract, preview_url="https://preview.app"
            )


class TestSerialisation:
    def test_the_verdict_serialises_for_the_feature_record(self):
        contract = contract_for(feature_id="FEAT-14", request=UI_CONTRACT_REQUEST)
        result = verifier(FakeRunner()).verify(
            contract, preview_url="https://preview.app", commit_sha="abc1234def"
        )
        record = result.to_dict()
        assert record["passed"] is True
        assert record["commit_sha"] == "abc1234def"
        assert record["registered_probes"] == []
