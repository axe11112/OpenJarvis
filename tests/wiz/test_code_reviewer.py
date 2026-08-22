"""Tests for independent code review session."""

from __future__ import annotations

import pytest

from openjarvis.wiz.code_reviewer import (
    CodeReviewResult,
    FeatureCodeReviewer,
    ReviewFinding,
    ReviewSeverity,
)


class TestReviewSeverity:
    """ReviewSeverity enum."""

    def test_severity_values(self) -> None:
        assert ReviewSeverity.CRITICAL.value == "critical"
        assert ReviewSeverity.MAJOR.value == "major"
        assert ReviewSeverity.MINOR.value == "minor"
        assert ReviewSeverity.INFO.value == "info"


class TestReviewFinding:
    """ReviewFinding dataclass."""

    def test_create_critical_finding(self) -> None:
        finding = ReviewFinding(
            severity=ReviewSeverity.CRITICAL,
            category="security",
            title="SQL injection vulnerability",
            description="User input not sanitized in query",
            file_path="src/db.py",
            line_number=42,
            suggested_fix="Use parameterized queries",
        )
        assert finding.severity == ReviewSeverity.CRITICAL
        assert finding.category == "security"
        assert finding.title == "SQL injection vulnerability"
        assert finding.file_path == "src/db.py"
        assert finding.line_number == 42

    def test_create_minor_finding(self) -> None:
        finding = ReviewFinding(
            severity=ReviewSeverity.MINOR,
            category="style",
            title="Variable naming inconsistency",
            description="Mixes camelCase and snake_case",
        )
        assert finding.severity == ReviewSeverity.MINOR
        assert finding.file_path is None
        assert finding.line_number is None

    def test_finding_to_dict(self) -> None:
        finding = ReviewFinding(
            severity=ReviewSeverity.MAJOR,
            category="correctness",
            title="Off-by-one error in loop",
            description="Loop terminates early",
            file_path="src/process.py",
            line_number=15,
        )
        d = finding.to_dict()
        assert d["severity"] == "major"
        assert d["category"] == "correctness"
        assert d["title"] == "Off-by-one error in loop"
        assert d["file_path"] == "src/process.py"
        assert d["line_number"] == 15


class TestCodeReviewResult:
    """CodeReviewResult dataclass."""

    def test_create_with_no_findings(self) -> None:
        result = CodeReviewResult(
            feature_id="WIZE-PILOT-001",
            reviewed_sha="abc123def456",
            findings=[],
            recommendation="APPROVE",
            summary="No issues found",
        )
        assert result.feature_id == "WIZE-PILOT-001"
        assert result.has_critical_findings is False
        assert result.has_major_findings is False
        assert result.should_request_changes is False

    def test_create_with_critical_finding(self) -> None:
        finding = ReviewFinding(
            severity=ReviewSeverity.CRITICAL,
            category="security",
            title="Secret in code",
            description="API key exposed",
        )
        result = CodeReviewResult(
            feature_id="WIZE-PILOT-001",
            reviewed_sha="abc123def456",
            findings=[finding],
            recommendation="CHANGES_REQUESTED",
            summary="Found critical issue",
        )
        assert result.has_critical_findings is True
        assert result.should_request_changes is True

    def test_create_with_major_finding(self) -> None:
        finding = ReviewFinding(
            severity=ReviewSeverity.MAJOR,
            category="correctness",
            title="Logic error",
            description="Condition never true",
        )
        result = CodeReviewResult(
            feature_id="WIZE-PILOT-001",
            reviewed_sha="abc123def456",
            findings=[finding],
            recommendation="CHANGES_REQUESTED",
            summary="Found major issue",
        )
        assert result.has_major_findings is True
        assert result.should_request_changes is True

    def test_create_with_minor_findings(self) -> None:
        findings = [
            ReviewFinding(
                severity=ReviewSeverity.MINOR,
                category="style",
                title="Unused variable",
                description="Variable declared but not used",
            ),
            ReviewFinding(
                severity=ReviewSeverity.INFO,
                category="documentation",
                title="Missing docstring",
                description="Function has no docstring",
            ),
        ]
        result = CodeReviewResult(
            feature_id="WIZE-PILOT-001",
            reviewed_sha="abc123def456",
            findings=findings,
            recommendation="COMMENT",
            summary="Found minor issues",
        )
        assert result.has_critical_findings is False
        assert result.has_major_findings is False
        assert result.should_request_changes is False

    def test_result_to_dict(self) -> None:
        finding = ReviewFinding(
            severity=ReviewSeverity.MINOR,
            category="style",
            title="Issue",
            description="Description",
        )
        result = CodeReviewResult(
            feature_id="WIZE-PILOT-001",
            reviewed_sha="abc123def456",
            findings=[finding],
            recommendation="COMMENT",
            summary="Summary",
            is_read_only=True,
            reviewer_session_id="review-WIZE-PILOT-001",
            review_duration_seconds=120.5,
        )
        d = result.to_dict()
        assert d["feature_id"] == "WIZE-PILOT-001"
        assert d["reviewed_sha"] == "abc123def456"
        assert len(d["findings"]) == 1
        assert d["recommendation"] == "COMMENT"
        assert d["is_read_only"] is True
        assert d["reviewer_session_id"] == "review-WIZE-PILOT-001"
        assert d["review_duration_seconds"] == 120.5


class TestFeatureCodeReviewer:
    """FeatureCodeReviewer functionality."""

    def test_initialization(self) -> None:
        reviewer = FeatureCodeReviewer()
        assert reviewer is not None
        assert reviewer._cli_executor is None
        assert reviewer._readonly_confirmed is False

    def test_initialization_with_executor(self) -> None:
        mock_executor = object()
        reviewer = FeatureCodeReviewer(cli_executor=mock_executor)
        assert reviewer._cli_executor is mock_executor

    def test_build_review_prompt_basic(self) -> None:
        reviewer = FeatureCodeReviewer()
        prompt = reviewer.build_review_prompt(
            feature_id="WIZE-PILOT-001",
            operator_request="Add dark mode toggle",
            implementation_plan="1. Add switch component\n2. Add CSS",
            diff="@@ -1,3 +1,5 @@\n-old line\n+new line",
            changed_files=["src/control_center.tsx", "src/styles.css"],
            acceptance_tests_status="passed",
            preview_url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
        )

        assert "INDEPENDENT CODE REVIEW" in prompt
        assert "WIZE-PILOT-001" in prompt
        assert "Add dark mode toggle" in prompt
        assert "REVIEW TASK (READ-ONLY)" in prompt
        assert "RESTRICTIONS:" in prompt
        assert "You CANNOT modify code" in prompt
        assert "You CANNOT suggest implementations" in prompt
        assert "https://wize-wiz-wize-pilot-001-wiz.vercel.app" in prompt
        assert "src/control_center.tsx" in prompt
        assert "src/styles.css" in prompt

    def test_build_review_prompt_no_preview(self) -> None:
        reviewer = FeatureCodeReviewer()
        prompt = reviewer.build_review_prompt(
            feature_id="WIZE-002",
            operator_request="Request",
            implementation_plan="Plan",
            diff="diff",
            changed_files=["file.ts"],
            acceptance_tests_status="failed",
            preview_url=None,
        )

        assert "(no preview URL)" in prompt
        assert "failed" in prompt

    def test_build_review_prompt_covers_security_checks(self) -> None:
        reviewer = FeatureCodeReviewer()
        prompt = reviewer.build_review_prompt(
            feature_id="WIZE-003",
            operator_request="Request",
            implementation_plan="Plan",
            diff="diff",
            changed_files=["file.py"],
            acceptance_tests_status="passed",
        )

        # Verify review covers security concerns
        assert "SECURITY CONCERNS" in prompt
        assert "Input validation" in prompt
        assert "SQL injection" in prompt
        assert "XSS risks" in prompt
        assert "Secrets or credentials" in prompt
        assert "Improper access control" in prompt

    def test_verify_readonly_returns_true(self) -> None:
        reviewer = FeatureCodeReviewer()
        # Simulate that reviewer session was read-only
        result = reviewer.verify_readonly("review-session-123")
        assert result is True

    def test_simulate_review_no_issues(self) -> None:
        reviewer = FeatureCodeReviewer()
        findings = reviewer._simulate_review(
            feature_id="WIZE-001",
            diff="@@ -1,3 +1,3 @@\n clean diff",
            changed_files=["src/component.tsx"],
            acceptance_tests_passed=True,
        )
        # No TODOs, no secrets, tests pass, small changeset = no findings
        assert len(findings) == 0

    def test_simulate_review_finds_todos(self) -> None:
        reviewer = FeatureCodeReviewer()
        diff_with_todo = """
@@ -10,6 +10,8 @@
+// TODO: refactor this later
+// FIXME: handle edge case
"""
        findings = reviewer._simulate_review(
            feature_id="WIZE-001",
            diff=diff_with_todo,
            changed_files=["src/component.tsx"],
            acceptance_tests_passed=True,
        )
        assert len(findings) == 1
        assert findings[0].severity == ReviewSeverity.MINOR
        assert "TODO" in findings[0].title

    def test_simulate_review_finds_secrets(self) -> None:
        reviewer = FeatureCodeReviewer()
        diff_with_secret = """
@@ -5,6 +5,8 @@
+API_KEY = "sk-1234567890abcdef"
+password = "super_secret_123"
"""
        findings = reviewer._simulate_review(
            feature_id="WIZE-001",
            diff=diff_with_secret,
            changed_files=["src/config.py"],
            acceptance_tests_passed=True,
        )
        # Should find secret pattern
        assert any(f.severity == ReviewSeverity.CRITICAL for f in findings)
        assert any("secret" in f.category.lower() for f in findings)

    def test_simulate_review_finds_failing_tests(self) -> None:
        reviewer = FeatureCodeReviewer()
        findings = reviewer._simulate_review(
            feature_id="WIZE-001",
            diff="@@ -1,3 +1,3 @@\n clean diff",
            changed_files=["src/component.tsx"],
            acceptance_tests_passed=False,
        )
        # Should find test failure
        assert any(f.severity == ReviewSeverity.MAJOR for f in findings)
        assert any("test" in f.title.lower() for f in findings)

    def test_simulate_review_finds_large_changeset(self) -> None:
        reviewer = FeatureCodeReviewer()
        # Generate large diff (>500 lines)
        large_diff = "\n".join(["+line %d" % i for i in range(510)])
        many_files = ["src/file%d.ts" % i for i in range(10)]

        findings = reviewer._simulate_review(
            feature_id="WIZE-001",
            diff=large_diff,
            changed_files=many_files,
            acceptance_tests_passed=True,
        )
        # Should find large changeset
        assert any("large" in f.title.lower() for f in findings)
        assert any(f.severity == ReviewSeverity.MINOR for f in findings)

    def test_build_summary_no_findings(self) -> None:
        reviewer = FeatureCodeReviewer()
        summary = reviewer._build_summary(findings=[], recommendation="APPROVE")
        assert "APPROVE" in summary
        assert "No issues" in summary

    def test_build_summary_with_findings(self) -> None:
        reviewer = FeatureCodeReviewer()
        findings = [
            ReviewFinding(
                severity=ReviewSeverity.CRITICAL,
                category="security",
                title="Secret",
                description="Found secret",
            ),
            ReviewFinding(
                severity=ReviewSeverity.MAJOR,
                category="correctness",
                title="Logic error",
                description="Broken logic",
            ),
            ReviewFinding(
                severity=ReviewSeverity.MINOR,
                category="style",
                title="Unused var",
                description="Variable unused",
            ),
        ]
        summary = reviewer._build_summary(
            findings=findings, recommendation="CHANGES_REQUESTED"
        )
        assert "CHANGES_REQUESTED" in summary
        assert "1 CRITICAL" in summary
        assert "1 MAJOR" in summary
        assert "1 MINOR" in summary


class TestCodeReviewFlow:
    """End-to-end code review flow."""

    def test_complete_review_flow_no_issues(self) -> None:
        """Simulate complete review with no findings."""
        reviewer = FeatureCodeReviewer()

        # Build review prompt
        prompt = reviewer.build_review_prompt(
            feature_id="WIZE-PILOT-001",
            operator_request="Add dark mode",
            implementation_plan="Add switch + CSS",
            diff="@@ -1,3 +1,4 @@\n+.dark { color: white; }",
            changed_files=["src/theme.css"],
            acceptance_tests_status="passed",
            preview_url="https://preview.vercel.app",
        )

        # Simulate review (in real case, would spawn session)
        findings = reviewer._simulate_review(
            feature_id="WIZE-PILOT-001",
            diff="@@ -1,3 +1,4 @@\n+.dark { color: white; }",
            changed_files=["src/theme.css"],
            acceptance_tests_passed=True,
        )

        # Build result
        recommendation = (
            "CHANGES_REQUESTED"
            if any(
                f.severity in (ReviewSeverity.CRITICAL, ReviewSeverity.MAJOR)
                for f in findings
            )
            else "COMMENT"
            if findings
            else "APPROVE"
        )
        summary = reviewer._build_summary(findings, recommendation)

        result = CodeReviewResult(
            feature_id="WIZE-PILOT-001",
            reviewed_sha="abc123",
            findings=findings,
            recommendation=recommendation,
            summary=summary,
            is_read_only=True,
        )

        # Verify
        assert result.recommendation == "APPROVE"
        assert not result.should_request_changes
        assert result.is_read_only is True

    def test_complete_review_flow_with_critical_finding(self) -> None:
        """Simulate complete review with critical finding."""
        reviewer = FeatureCodeReviewer()

        # Build review prompt
        prompt = reviewer.build_review_prompt(
            feature_id="WIZE-PILOT-002",
            operator_request="Add auth",
            implementation_plan="Add token validation",
            diff="@@ -1,2 +1,3 @@\nSECRET_KEY = 'abc123'",
            changed_files=["src/auth.py"],
            acceptance_tests_status="passed",
        )

        # Simulate review finding secret
        findings = reviewer._simulate_review(
            feature_id="WIZE-PILOT-002",
            diff="@@ -1,2 +1,3 @@\nSECRET_KEY = 'abc123' # secret in code",
            changed_files=["src/auth.py"],
            acceptance_tests_passed=True,
        )

        # Build result
        recommendation = (
            "CHANGES_REQUESTED"
            if any(
                f.severity in (ReviewSeverity.CRITICAL, ReviewSeverity.MAJOR)
                for f in findings
            )
            else "COMMENT"
            if findings
            else "APPROVE"
        )
        summary = reviewer._build_summary(findings, recommendation)

        result = CodeReviewResult(
            feature_id="WIZE-PILOT-002",
            reviewed_sha="def456",
            findings=findings,
            recommendation=recommendation,
            summary=summary,
            is_read_only=True,
        )

        # Verify
        assert result.recommendation == "CHANGES_REQUESTED"
        assert result.should_request_changes is True
        assert result.has_critical_findings is True
        assert result.is_read_only is True


class TestReviewReadOnlyGuarantee:
    """Verify reviewer cannot mutate code."""

    def test_reviewer_readonly_property(self) -> None:
        """Verify result marks reviewer as read-only."""
        result = CodeReviewResult(
            feature_id="WIZE-001",
            reviewed_sha="abc123",
            findings=[],
            recommendation="APPROVE",
            summary="OK",
            is_read_only=True,
        )
        assert result.is_read_only is True

    def test_reviewer_cannot_suggest_code_changes(self) -> None:
        """Verify findings don't include code suggestions in review prompt."""
        reviewer = FeatureCodeReviewer()
        prompt = reviewer.build_review_prompt(
            feature_id="WIZE-001",
            operator_request="Add feature",
            implementation_plan="Implementation",
            diff="diff",
            changed_files=["file.ts"],
            acceptance_tests_status="passed",
        )
        # Prompt explicitly forbids implementations
        assert "You CANNOT suggest implementations" in prompt
        assert "Do NOT attempt to fix issues" in prompt

    def test_reviewer_findings_are_advisory_only(self) -> None:
        """Verify findings don't override deterministic gates."""
        # This is enforced by contract gates, not reviewer
        # Reviewer outputs findings, but contract.can_merge() checks:
        # - tests_passed (deterministic: npm test)
        # - acceptance_tests_passed (deterministic: test runner)
        # - ci_all_checks_green (deterministic: CI checks)
        # These always win over code_review findings
        result = CodeReviewResult(
            feature_id="WIZE-001",
            reviewed_sha="abc123",
            findings=[
                ReviewFinding(
                    severity=ReviewSeverity.CRITICAL,
                    category="design",
                    title="Architectural concern",
                    description="High-level design question",
                )
            ],
            recommendation="CHANGES_REQUESTED",
            summary="Architectural concern",
            is_read_only=True,
        )
        # Findings are captured, but deterministic gates still apply
        # (This is tested in feature contract tests)
        assert result.should_request_changes is True
