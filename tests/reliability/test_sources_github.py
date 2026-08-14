"""Tests for the GitHub source, including its write-safety interlocks."""

from __future__ import annotations

import httpx
import pytest

from openjarvis.reliability.sources._stubs import ResilientClient
from openjarvis.reliability.sources.github import (
    GitHubSource,
    ProtectedPathError,
    UnsafeBranchError,
    is_protected_path,
)
from openjarvis.reliability.types import Severity

API = "https://api.github.com"
REPO = "acme/site"


def _source(**kwargs) -> GitHubSource:
    kwargs.setdefault("repo", REPO)
    kwargs.setdefault(
        "client",
        ResilientClient(
            base_url=API,
            source="github",
            headers={"Authorization": "Bearer test"},
            sleep=lambda _: None,
            jitter=lambda: 0.0,
        ),
    )
    return GitHubSource(**kwargs)


# ---------------------------------------------------------------------------
# Protected paths
# ---------------------------------------------------------------------------


class TestProtectedPaths:
    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/ci.yml",
            ".github/workflows/nested/deploy.yaml",
            ".github/actions/setup/action.yml",
        ],
    )
    def test_ci_configuration_is_always_protected(self, path):
        """A self-modifying CI config would let a repair disable its own checks."""
        assert is_protected_path(path)

    def test_ordinary_paths_are_not_protected(self):
        assert not is_protected_path("app/auth/callback.ts")
        assert not is_protected_path("README.md")

    def test_extra_patterns_apply(self):
        assert is_protected_path("app/auth/session.ts", ["**/auth/**"])
        assert is_protected_path("supabase/rls_policies.sql", ["*rls*"])

    def test_leading_dot_slash_is_normalized(self):
        assert is_protected_path("./.github/workflows/ci.yml")

    def test_assert_paths_allowed_passes_clean_diff(self):
        _source().assert_paths_allowed(["app/page.tsx", "lib/util.ts"])

    def test_assert_paths_allowed_blocks_workflow_edit(self):
        with pytest.raises(ProtectedPathError, match=".github/workflows/ci.yml"):
            _source().assert_paths_allowed(["app/x.ts", ".github/workflows/ci.yml"])


# ---------------------------------------------------------------------------
# Branch safety
# ---------------------------------------------------------------------------


class TestBranchSafety:
    def test_refuses_to_create_the_default_branch(self):
        with pytest.raises(UnsafeBranchError, match="default branch"):
            _source(base_branch="main").create_branch("main")

    def test_refuses_pr_from_the_default_branch(self):
        with pytest.raises(UnsafeBranchError):
            _source().create_pull_request(head="main", title="t", body="b")

    def test_branch_name_is_derived_from_the_incident(self):
        assert _source().branch_name_for("INC-00042") == "jarvis/incident-INC-00042"

    def test_override_allows_default_branch_write(self, respx_mock):
        """The interlock is overridable, but only explicitly."""
        respx_mock.get(f"{API}/repos/{REPO}/git/ref/heads/main").mock(
            return_value=httpx.Response(200, json={"object": {"sha": "abc123"}})
        )
        respx_mock.post(f"{API}/repos/{REPO}/git/refs").mock(
            return_value=httpx.Response(201, json={})
        )
        source = _source(allow_push_to_default_branch=True)
        assert source.create_branch("main") == "abc123"

    def test_has_no_merge_method(self):
        """JARVIS opens pull requests; humans merge them."""
        assert not hasattr(GitHubSource, "merge_pull_request")
        assert not hasattr(GitHubSource, "merge")

    def test_rejects_a_malformed_repo(self):
        with pytest.raises(ValueError, match="owner/name"):
            GitHubSource(repo="justaname")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class TestReads:
    def test_list_commits(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}/commits").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "sha": "aaa111",
                        "html_url": "https://github.com/acme/site/commit/aaa111",
                        "commit": {
                            "message": "fix: auth callback\n\nlonger body",
                            "author": {"name": "Dev", "date": "2026-08-14T10:00:00Z"},
                        },
                    }
                ],
            )
        )
        commits = _source().list_commits(since="2026-08-14T09:00:00Z")
        assert len(commits) == 1
        assert commits[0]["sha"] == "aaa111"
        assert commits[0]["message"] == "fix: auth callback"  # first line only

    def test_list_commits_respects_limit(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}/commits").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"sha": f"s{i}", "commit": {"message": "m", "author": {}}}
                    for i in range(10)
                ],
            )
        )
        assert len(_source().list_commits(limit=3)) == 3

    def test_get_commit_includes_files(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}/commits/aaa111").mock(
            return_value=httpx.Response(
                200,
                json={
                    "sha": "aaa111",
                    "commit": {"message": "m", "author": {"name": "D"}},
                    "files": [
                        {
                            "filename": "app/auth/callback.ts",
                            "status": "modified",
                            "additions": 3,
                            "deletions": 1,
                        }
                    ],
                },
            )
        )
        commit = _source().get_commit("aaa111")
        assert commit["files"][0]["filename"] == "app/auth/callback.ts"

    def test_list_branches(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}/branches").mock(
            return_value=httpx.Response(200, json=[{"name": "main"}, {"name": "dev"}])
        )
        assert _source().list_branches() == ["main", "dev"]

    def test_list_pull_requests(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}/pulls").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "number": 7,
                        "title": "Fix auth",
                        "state": "open",
                        "head": {"ref": "jarvis/incident-INC-00001"},
                        "base": {"ref": "main"},
                        "html_url": "https://github.com/acme/site/pull/7",
                        "merged_at": None,
                        "user": {"login": "jarvis-bot"},
                    }
                ],
            )
        )
        prs = _source().list_pull_requests()
        assert prs[0]["number"] == 7
        assert prs[0]["head"] == "jarvis/incident-INC-00001"

    def test_list_workflow_runs(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}/actions/runs").mock(
            return_value=httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 99,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "failure",
                            "head_branch": "main",
                            "head_sha": "aaa111",
                            "html_url": "https://github.com/acme/site/actions/runs/99",
                            "created_at": "2026-08-14T10:00:00Z",
                        }
                    ]
                },
            )
        )
        runs = _source().list_workflow_runs()
        assert runs[0]["conclusion"] == "failure"
        assert runs[0]["commit_sha"] == "aaa111"

    def test_job_logs_are_truncated(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}/actions/runs/99/logs").mock(
            return_value=httpx.Response(200, text="x" * 20000)
        )
        logs = _source().get_job_logs(99, max_chars=100)
        assert len(logs) < 200
        assert logs.endswith("(truncated)")

    def test_job_logs_failure_returns_empty(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}/actions/runs/99/logs").mock(
            return_value=httpx.Response(404)
        )
        assert _source().get_job_logs(99) == ""


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class TestWrites:
    def test_create_branch_from_base(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}/git/ref/heads/main").mock(
            return_value=httpx.Response(200, json={"object": {"sha": "base-sha"}})
        )
        route = respx_mock.post(f"{API}/repos/{REPO}/git/refs").mock(
            return_value=httpx.Response(201, json={})
        )
        sha = _source().create_branch("jarvis/incident-INC-00001")
        assert sha == "base-sha"
        import json

        body = json.loads(route.calls[0].request.read())
        assert body == {
            "ref": "refs/heads/jarvis/incident-INC-00001",
            "sha": "base-sha",
        }

    def test_create_branch_unresolvable_base(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}/git/ref/heads/main").mock(
            return_value=httpx.Response(200, json={})
        )
        with pytest.raises(RuntimeError, match="could not resolve"):
            _source().create_branch("jarvis/x")

    def test_create_pull_request(self, respx_mock):
        respx_mock.post(f"{API}/repos/{REPO}/pulls").mock(
            return_value=httpx.Response(
                201,
                json={"number": 12, "html_url": "https://github.com/acme/site/pull/12"},
            )
        )
        respx_mock.post(f"{API}/repos/{REPO}/issues/12/labels").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = _source().create_pull_request(
            head="jarvis/incident-INC-00001",
            title="JARVIS: fix auth callback",
            body="...",
            labels=["jarvis"],
        )
        assert result == {
            "number": 12,
            "url": "https://github.com/acme/site/pull/12",
        }

    def test_pr_succeeds_even_if_labelling_fails(self, respx_mock):
        respx_mock.post(f"{API}/repos/{REPO}/pulls").mock(
            return_value=httpx.Response(201, json={"number": 12, "html_url": "u"})
        )
        respx_mock.post(f"{API}/repos/{REPO}/issues/12/labels").mock(
            return_value=httpx.Response(403)
        )
        result = _source().create_pull_request(
            head="jarvis/x", title="t", body="b", labels=["jarvis"]
        )
        assert result["number"] == 12


# ---------------------------------------------------------------------------
# Signal source contract
# ---------------------------------------------------------------------------


class TestPollAndHealth:
    def _runs(self, **overrides):
        run = {
            "id": 99,
            "name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "head_branch": "main",
            "head_sha": "aaa111",
            "html_url": "u",
            "created_at": "2026-08-14T10:00:00Z",
        }
        run.update(overrides)
        return {"workflow_runs": [run]}

    def test_failed_run_on_default_branch_is_high(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}/actions/runs").mock(
            return_value=httpx.Response(200, json=self._runs())
        )
        signals = _source().poll()
        assert len(signals) == 1
        assert signals[0].severity is Severity.HIGH
        assert signals[0].kind == "workflow_failed"

    def test_failed_run_on_feature_branch_is_medium(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}/actions/runs").mock(
            return_value=httpx.Response(200, json=self._runs(head_branch="feature-x"))
        )
        assert _source().poll()[0].severity is Severity.MEDIUM

    def test_successful_runs_produce_no_signals(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}/actions/runs").mock(
            return_value=httpx.Response(200, json=self._runs(conclusion="success"))
        )
        assert _source().poll() == []

    def test_since_filter(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}/actions/runs").mock(
            return_value=httpx.Response(200, json=self._runs())
        )
        assert _source().poll(since="2026-08-14T11:00:00Z") == []

    def test_signals_are_marked_external(self, respx_mock):
        """A workflow name is author-controlled text and must be untrusted."""
        respx_mock.get(f"{API}/repos/{REPO}/actions/runs").mock(
            return_value=httpx.Response(200, json=self._runs())
        )
        assert _source().poll()[0].trust.value == "external"

    def test_poll_never_raises_on_api_failure(self, respx_mock):
        """An API outage must not take down the monitoring loop."""
        respx_mock.get(f"{API}/repos/{REPO}/actions/runs").mock(
            return_value=httpx.Response(500)
        )
        assert _source().poll() == []

    def test_poll_without_a_token_returns_empty(self, monkeypatch):
        monkeypatch.delenv("GITHUB_READONLY_TOKEN", raising=False)
        source = GitHubSource(repo=REPO)
        assert source.poll() == []

    def test_health_ok(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}").mock(
            return_value=httpx.Response(200, json={"full_name": REPO})
        )
        health = _source().health()
        assert health.reachable
        assert not health.degraded

    def test_health_reports_missing_token_without_leaking_it(self, monkeypatch):
        monkeypatch.delenv("GITHUB_READONLY_TOKEN", raising=False)
        health = GitHubSource(repo=REPO).health()
        assert not health.reachable
        assert "GITHUB_READONLY_TOKEN" in health.detail

    def test_health_reports_http_failure(self, respx_mock):
        respx_mock.get(f"{API}/repos/{REPO}").mock(return_value=httpx.Response(404))
        health = _source().health()
        assert not health.reachable
