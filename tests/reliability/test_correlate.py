"""Tests for failure→commit correlation."""

from __future__ import annotations

from datetime import timedelta

from openjarvis.reliability.correlate import correlate, parse_iso, score_commit

FAILURE = "2026-08-14T12:00:00Z"


def _commit(sha, minutes_before, files=(), message="fix: something"):
    from datetime import datetime, timezone

    when = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc) - timedelta(
        minutes=minutes_before
    )
    return {
        "sha": sha,
        "message": message,
        "date": when.isoformat().replace("+00:00", "Z"),
        "files": [{"filename": f} for f in files],
    }


class TestParseIso:
    def test_z_suffix(self):
        assert parse_iso("2026-08-14T12:00:00Z") is not None

    def test_offset(self):
        assert parse_iso("2026-08-14T12:00:00+02:00") is not None

    def test_naive_is_assumed_utc(self):
        parsed = parse_iso("2026-08-14T12:00:00")
        assert parsed is not None and parsed.tzinfo is not None

    def test_malformed_returns_none(self):
        """External APIs send junk; correlation degrades, it does not crash."""
        assert parse_iso("not a date") is None

    def test_empty(self):
        assert parse_iso("") is None


class TestScoreCommit:
    def test_recent_commit_scores_higher_than_old(self):
        failure = parse_iso(FAILURE)
        recent, _ = score_commit(_commit("a", 5), failure_time=failure)
        old, _ = score_commit(_commit("b", 300), failure_time=failure)
        assert recent > old

    def test_commit_after_the_failure_scores_zero(self):
        """A commit that landed after the failure cannot have caused it."""
        failure = parse_iso(FAILURE)
        score, _ = score_commit(_commit("a", -30), failure_time=failure)
        assert score == 0.0

    def test_commit_outside_the_window_scores_zero(self):
        failure = parse_iso(FAILURE)
        score, _ = score_commit(
            _commit("a", 60 * 24), failure_time=failure, window=timedelta(hours=6)
        )
        assert score == 0.0

    def test_component_overlap_raises_the_score(self):
        failure = parse_iso(FAILURE)
        plain, _ = score_commit(
            _commit("a", 10, ["README.md"]), failure_time=failure, component="auth"
        )
        related, _ = score_commit(
            _commit("b", 10, ["app/auth/callback.ts"]),
            failure_time=failure,
            component="auth",
        )
        assert related > plain

    def test_deployment_match_raises_the_score(self):
        failure = parse_iso(FAILURE)
        without, _ = score_commit(_commit("abc1234def", 10), failure_time=failure)
        with_deploy, _ = score_commit(
            _commit("abc1234def", 10), failure_time=failure, deployment_sha="abc1234"
        )
        assert with_deploy > without

    def test_reasons_are_human_readable(self):
        failure = parse_iso(FAILURE)
        _, reasons = score_commit(
            _commit("a", 12, ["app/auth/x.ts"]),
            failure_time=failure,
            component="auth",
        )
        assert any("min before the failure" in r for r in reasons)
        assert any("related to 'auth'" in r for r in reasons)

    def test_missing_failure_time_scores_zero(self):
        score, _ = score_commit(_commit("a", 5), failure_time=None)
        assert score == 0.0


class TestCorrelate:
    def test_no_commits_is_an_honest_empty_answer(self):
        result = correlate(failure_time=FAILURE, commits=[])
        assert result.commit_sha == ""
        assert result.confidence == 0.0
        assert "No commit landed" in result.notes

    def test_all_commits_outside_the_window(self):
        result = correlate(failure_time=FAILURE, commits=[_commit("a", 60 * 48)])
        assert result.confidence == 0.0

    def test_picks_the_most_recent_related_commit(self):
        result = correlate(
            failure_time=FAILURE,
            component="auth",
            commits=[
                _commit("old111", 200, ["app/auth/callback.ts"]),
                _commit("new222", 8, ["app/auth/callback.ts"]),
                _commit("mid333", 60, ["README.md"]),
            ],
        )
        assert result.commit_sha == "new222"
        assert result.confidence > 0.5
        assert result.changed_files == ["app/auth/callback.ts"]

    def test_close_runner_up_lowers_confidence(self):
        """Two equally plausible commits must not produce a confident answer."""
        confident = correlate(
            failure_time=FAILURE,
            component="auth",
            commits=[_commit("a", 5, ["app/auth/x.ts"])],
        )
        ambiguous = correlate(
            failure_time=FAILURE,
            component="auth",
            commits=[
                _commit("a", 5, ["app/auth/x.ts"]),
                _commit("b", 6, ["app/auth/y.ts"]),
            ],
        )
        assert ambiguous.confidence < confident.confidence
        assert "uncertain" in ambiguous.notes

    def test_deployment_id_is_carried_through(self):
        result = correlate(
            failure_time=FAILURE,
            commits=[_commit("a", 5)],
            deployment_id="dpl_123",
        )
        assert result.deployment_id == "dpl_123"

    def test_deployment_id_present_even_with_no_commits(self):
        result = correlate(failure_time=FAILURE, commits=[], deployment_id="dpl_123")
        assert result.deployment_id == "dpl_123"

    def test_matches_pull_request_by_number_in_message(self):
        result = correlate(
            failure_time=FAILURE,
            commits=[_commit("a", 5, message="fix: auth callback (#42)")],
            pull_requests=[{"number": 42, "title": "Fix auth", "head": "fix-auth"}],
        )
        assert result.pr_number == 42
        assert result.branch == "fix-auth"

    def test_matches_pull_request_by_title(self):
        result = correlate(
            failure_time=FAILURE,
            commits=[_commit("a", 5, message="fix auth callback")],
            pull_requests=[
                {"number": 7, "title": "Fix auth callback", "head": "fix-auth"}
            ],
        )
        assert result.pr_number == 7

    def test_no_pull_request_match(self):
        result = correlate(
            failure_time=FAILURE,
            commits=[_commit("a", 5)],
            pull_requests=[{"number": 9, "title": "Unrelated", "head": "x"}],
        )
        assert result.pr_number == 0

    def test_malformed_failure_time_degrades_gracefully(self):
        result = correlate(failure_time="garbage", commits=[_commit("a", 5)])
        assert result.confidence == 0.0

    def test_confidence_never_exceeds_one(self):
        result = correlate(
            failure_time=FAILURE,
            component="auth",
            deployment_sha="abc1234",
            commits=[_commit("abc1234def", 0, ["app/auth/a.ts", "app/auth/b.ts"])],
        )
        assert 0.0 <= result.confidence <= 1.0
