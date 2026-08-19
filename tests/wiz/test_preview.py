"""Preview observation: the right build, or none."""

from __future__ import annotations

import pytest

from openjarvis.wiz.features.preview import PreviewObserver, PreviewUnavailable

FEATURE_SHA = "abc1234def5678901234567890abcdef12345678"
OTHER_SHA = "9999999999999999999999999999999999999999"


def deployment(sha, state="READY", branch="wiz/feature/FEAT-1", url="https://p.app"):
    return {
        "id": f"dpl_{sha[:8]}",
        "state": state,
        "target": "preview",
        "url": url,
        "created_at": "2026-08-19T09:00:00+00:00",
        "commit_sha": sha,
        "branch": branch,
    }


class FakeVercel:
    """A Vercel that returns a scripted sequence of deployment listings."""

    def __init__(self, pages, logs=""):
        self._pages = list(pages)
        self.calls = 0
        self.logs = logs
        self.log_requests = []

    def list_deployments(self, *, limit=20, target="", state=""):
        self.calls += 1
        if not self._pages:
            return []
        return self._pages.pop(0) if len(self._pages) > 1 else self._pages[0]

    def get_build_logs(self, deployment_id, **kwargs):
        self.log_requests.append(deployment_id)
        return self.logs


@pytest.fixture
def no_waiting():
    """A clock that advances a minute per sleep, so tests never actually wait."""
    state = {"now": 0.0}

    def sleep(seconds):
        state["now"] += seconds

    def monotonic():
        return state["now"]

    return sleep, monotonic


def observer(vercel, no_waiting, **kwargs):
    sleep, monotonic = no_waiting
    return PreviewObserver(
        vercel=vercel, sleep=sleep, monotonic=monotonic, poll_seconds=15.0, **kwargs
    )


class TestExactMatching:
    def test_a_ready_preview_for_the_exact_commit_is_used(self, no_waiting):
        vercel = FakeVercel([[deployment(FEATURE_SHA)]])
        result = observer(vercel, no_waiting).observe(commit_sha=FEATURE_SHA)
        assert result.usable
        assert result.url == "https://p.app"

    def test_a_green_preview_for_another_commit_is_not_the_feature(self, no_waiting):
        # The failure this exists to prevent: a branch has many deployments,
        # and the newest green one is very often not the commit under test.
        vercel = FakeVercel([[deployment(OTHER_SHA)]])
        result = observer(vercel, no_waiting, timeout_seconds=30).observe(
            commit_sha=FEATURE_SHA, branch="wiz/feature/FEAT-1"
        )
        assert not result.matched
        assert not result.usable
        assert "no preview deployment appeared" in result.reason

    def test_a_near_miss_is_reported_rather_than_hidden(self, no_waiting):
        vercel = FakeVercel([[deployment(OTHER_SHA)]])
        result = observer(vercel, no_waiting, timeout_seconds=30).observe(
            commit_sha=FEATURE_SHA, branch="wiz/feature/FEAT-1"
        )
        assert result.other_deployments
        assert result.other_deployments[0]["state"] == "READY"

    def test_an_abbreviated_sha_still_matches_the_same_commit(self, no_waiting):
        vercel = FakeVercel([[deployment(FEATURE_SHA[:7])]])
        result = observer(vercel, no_waiting).observe(commit_sha=FEATURE_SHA)
        assert result.usable

    def test_a_dangerously_short_identifier_never_matches(self, no_waiting):
        # Six characters is short enough for a collision to be an accident
        # rather than an attack, and the cost of a wrong match is verifying
        # somebody else's code and calling it the feature.
        vercel = FakeVercel([[deployment("abc123")]])
        result = observer(vercel, no_waiting, timeout_seconds=30).observe(
            commit_sha=FEATURE_SHA
        )
        assert not result.matched

    def test_matching_by_branch_alone_is_impossible(self, no_waiting):
        vercel = FakeVercel([[deployment(FEATURE_SHA)]])
        with pytest.raises(PreviewUnavailable, match="commit SHA is required"):
            observer(vercel, no_waiting).observe(commit_sha="", branch="x")


class TestReadiness:
    def test_a_building_preview_is_waited_for(self, no_waiting):
        vercel = FakeVercel(
            [
                [deployment(FEATURE_SHA, state="BUILDING")],
                [deployment(FEATURE_SHA, state="READY")],
            ]
        )
        result = observer(vercel, no_waiting).observe(commit_sha=FEATURE_SHA)
        assert result.usable
        assert result.polls == 2

    def test_a_building_preview_is_never_verified(self, no_waiting):
        vercel = FakeVercel([[deployment(FEATURE_SHA, state="BUILDING")]])
        result = observer(vercel, no_waiting, timeout_seconds=30).observe(
            commit_sha=FEATURE_SHA
        )
        assert result.matched
        assert not result.usable
        assert "still BUILDING" in result.reason

    def test_a_failed_build_stops_immediately_with_its_logs(self, no_waiting):
        # A failed build is evidence for the next attempt, not an absence to
        # keep waiting on.
        vercel = FakeVercel(
            [[deployment(FEATURE_SHA, state="ERROR")]],
            logs="Type error: Property 'total' does not exist",
        )
        result = observer(vercel, no_waiting).observe(commit_sha=FEATURE_SHA)
        assert result.failed_to_build
        assert not result.usable
        assert result.polls == 1
        assert "Type error" in result.build_log
        assert "Type error" in result.evidence()

    def test_a_cancelled_build_is_terminal_too(self, no_waiting):
        vercel = FakeVercel([[deployment(FEATURE_SHA, state="CANCELED")]])
        result = observer(vercel, no_waiting).observe(commit_sha=FEATURE_SHA)
        assert result.failed_to_build

    def test_a_ready_deployment_with_no_url_is_not_usable(self, no_waiting):
        vercel = FakeVercel([[deployment(FEATURE_SHA, url="")]])
        result = observer(vercel, no_waiting).observe(commit_sha=FEATURE_SHA)
        assert result.ready
        assert not result.usable


class TestResilience:
    def test_a_provider_outage_does_not_end_the_feature(self, no_waiting):
        class Broken:
            def __init__(self):
                self.calls = 0

            def list_deployments(self, **kwargs):
                self.calls += 1
                if self.calls < 3:
                    raise RuntimeError("502 Bad Gateway")
                return [deployment(FEATURE_SHA)]

        result = observer(Broken(), no_waiting).observe(commit_sha=FEATURE_SHA)
        assert result.usable
        assert result.polls == 3

    def test_the_deadline_is_kept_even_when_nothing_answers(self, no_waiting):
        class Silent:
            def list_deployments(self, **kwargs):
                raise RuntimeError("down")

        result = observer(Silent(), no_waiting, timeout_seconds=60).observe(
            commit_sha=FEATURE_SHA
        )
        assert not result.usable
        assert result.waited_seconds >= 60

    def test_the_observation_serialises_for_the_audit_record(self, no_waiting):
        vercel = FakeVercel([[deployment(FEATURE_SHA)]])
        result = observer(vercel, no_waiting).observe(commit_sha=FEATURE_SHA)
        record = result.to_dict()
        assert record["ready"] is True
        assert record["commit_sha"] == FEATURE_SHA
