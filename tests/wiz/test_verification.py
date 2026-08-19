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
    CONTENT,
    MANUAL,
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
