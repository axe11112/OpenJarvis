"""Tests for the Vercel source."""

from __future__ import annotations

import httpx

from openjarvis.reliability.sources._stubs import ResilientClient
from openjarvis.reliability.sources.vercel import VercelSource
from openjarvis.reliability.types import Severity

API = "https://api.vercel.com"
PROJECT = "prj_abc"


def _source(**kwargs) -> VercelSource:
    kwargs.setdefault("project_id", PROJECT)
    kwargs.setdefault(
        "client",
        ResilientClient(
            base_url=API,
            source="vercel",
            headers={"Authorization": "Bearer test"},
            sleep=lambda _: None,
            jitter=lambda: 0.0,
        ),
    )
    return VercelSource(**kwargs)


def _deployment(**overrides):
    base = {
        "uid": "dpl_1",
        "readyState": "READY",
        "target": "production",
        "url": "site-abc.vercel.app",
        "createdAt": 1786000000000,
        "meta": {
            "githubCommitSha": "aaa111",
            "githubCommitRef": "main",
            "githubCommitMessage": "fix: auth\n\nbody",
        },
    }
    base.update(overrides)
    return base


class TestListDeployments:
    def test_normalizes_payload(self, respx_mock):
        respx_mock.get(f"{API}/v6/deployments").mock(
            return_value=httpx.Response(200, json={"deployments": [_deployment()]})
        )
        deployments = _source().list_deployments()
        assert deployments[0]["id"] == "dpl_1"
        assert deployments[0]["state"] == "READY"
        assert deployments[0]["commit_sha"] == "aaa111"
        assert deployments[0]["branch"] == "main"
        assert deployments[0]["url"].startswith("https://")
        assert deployments[0]["commit_message"] == "fix: auth"
        assert deployments[0]["created_at"].startswith("2026-")

    def test_handles_missing_meta(self, respx_mock):
        respx_mock.get(f"{API}/v6/deployments").mock(
            return_value=httpx.Response(
                200, json={"deployments": [{"uid": "d", "readyState": "ready"}]}
            )
        )
        deployment = _source().list_deployments()[0]
        assert deployment["state"] == "READY"
        assert deployment["commit_sha"] == ""

    def test_handles_bad_timestamp(self, respx_mock):
        respx_mock.get(f"{API}/v6/deployments").mock(
            return_value=httpx.Response(
                200,
                json={"deployments": [_deployment(createdAt="not-a-number")]},
            )
        )
        assert _source().list_deployments()[0]["created_at"] == ""

    def test_team_id_is_sent(self, respx_mock):
        route = respx_mock.get(f"{API}/v6/deployments").mock(
            return_value=httpx.Response(200, json={"deployments": []})
        )
        _source(team_id="team_x").list_deployments()
        assert "teamId=team_x" in str(route.calls[0].request.url)

    def test_no_team_id_is_omitted(self, respx_mock):
        route = respx_mock.get(f"{API}/v6/deployments").mock(
            return_value=httpx.Response(200, json={"deployments": []})
        )
        _source().list_deployments()
        assert "teamId" not in str(route.calls[0].request.url)


class TestBuildLogs:
    def test_joins_event_text(self, respx_mock):
        respx_mock.get(f"{API}/v2/deployments/dpl_1/events").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"payload": {"text": "Installing..."}},
                    {"payload": {"text": "Error: cannot find module 'x'"}},
                ],
            )
        )
        logs = _source().get_build_logs("dpl_1")
        assert "cannot find module" in logs

    def test_keeps_the_tail_when_truncating(self, respx_mock):
        """Build failures put the error at the end — truncating the head would
        throw away the only useful part."""
        respx_mock.get(f"{API}/v2/deployments/dpl_1/events").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"payload": {"text": "noise " * 5000}},
                    {"payload": {"text": "FATAL"}},
                ],
            )
        )
        logs = _source().get_build_logs("dpl_1", max_chars=200)
        assert logs.startswith("... (truncated)")
        assert "FATAL" in logs

    def test_api_failure_returns_empty(self, respx_mock):
        respx_mock.get(f"{API}/v2/deployments/dpl_1/events").mock(
            return_value=httpx.Response(404)
        )
        assert _source().get_build_logs("dpl_1") == ""

    def test_ignores_malformed_events(self, respx_mock):
        respx_mock.get(f"{API}/v2/deployments/dpl_1/events").mock(
            return_value=httpx.Response(200, json=["a string", 42, {"payload": {}}])
        )
        assert _source().get_build_logs("dpl_1") == ""


class TestPreviewLookup:
    def test_finds_ready_preview_for_branch(self, respx_mock):
        respx_mock.get(f"{API}/v6/deployments").mock(
            return_value=httpx.Response(
                200,
                json={
                    "deployments": [
                        _deployment(
                            uid="dpl_other",
                            target="preview",
                            meta={"githubCommitRef": "other"},
                        ),
                        _deployment(
                            uid="dpl_target",
                            target="preview",
                            meta={"githubCommitRef": "jarvis/incident-INC-00001"},
                        ),
                    ]
                },
            )
        )
        found = _source().find_preview_deployment("jarvis/incident-INC-00001")
        assert found is not None
        assert found["id"] == "dpl_target"

    def test_ignores_previews_that_are_not_ready(self, respx_mock):
        """Verifying against a still-building preview would produce a
        meaningless failure."""
        respx_mock.get(f"{API}/v6/deployments").mock(
            return_value=httpx.Response(
                200,
                json={
                    "deployments": [
                        _deployment(
                            uid="dpl_building",
                            target="preview",
                            readyState="BUILDING",
                            meta={"githubCommitRef": "jarvis/x"},
                        )
                    ]
                },
            )
        )
        assert _source().find_preview_deployment("jarvis/x") is None

    def test_no_match_returns_none(self, respx_mock):
        respx_mock.get(f"{API}/v6/deployments").mock(
            return_value=httpx.Response(200, json={"deployments": []})
        )
        assert _source().find_preview_deployment("jarvis/x") is None


class TestEnvironmentVariables:
    def test_returns_names_only(self, respx_mock):
        """The API can decrypt values; JARVIS must never ask for them."""
        respx_mock.get(f"{API}/v9/projects/{PROJECT}/env").mock(
            return_value=httpx.Response(
                200,
                json={
                    "envs": [
                        {
                            "key": "STRIPE_SECRET_KEY",
                            "value": "sk_live_SHOULD_NOT_LEAK",
                        },
                        {"key": "DATABASE_URL", "value": "postgres://u:p@h/db"},
                    ]
                },
            )
        )
        names = _source().list_environment_variable_names()
        assert names == ["DATABASE_URL", "STRIPE_SECRET_KEY"]
        assert "sk_live_SHOULD_NOT_LEAK" not in str(names)

    def test_no_method_returns_values(self):
        """Structural, not procedural: there is no value-returning method."""
        methods = [m for m in dir(VercelSource) if not m.startswith("_")]
        assert not any("value" in m.lower() for m in methods)
        assert not any("secret" in m.lower() for m in methods)


class TestPollAndHealth:
    def test_failed_production_deployment_is_high(self, respx_mock):
        respx_mock.get(f"{API}/v6/deployments").mock(
            return_value=httpx.Response(
                200, json={"deployments": [_deployment(readyState="ERROR")]}
            )
        )
        signals = _source().poll()
        assert len(signals) == 1
        assert signals[0].severity is Severity.HIGH
        assert signals[0].kind == "deployment_failed"
        assert signals[0].metadata["commit_sha"] == "aaa111"

    def test_failed_preview_is_medium(self, respx_mock):
        respx_mock.get(f"{API}/v6/deployments").mock(
            return_value=httpx.Response(
                200,
                json={
                    "deployments": [_deployment(readyState="ERROR", target="preview")]
                },
            )
        )
        assert _source().poll()[0].severity is Severity.MEDIUM

    def test_canceled_counts_as_failed(self, respx_mock):
        respx_mock.get(f"{API}/v6/deployments").mock(
            return_value=httpx.Response(
                200, json={"deployments": [_deployment(readyState="CANCELED")]}
            )
        )
        assert len(_source().poll()) == 1

    def test_ready_deployments_produce_no_signals(self, respx_mock):
        respx_mock.get(f"{API}/v6/deployments").mock(
            return_value=httpx.Response(200, json={"deployments": [_deployment()]})
        )
        assert _source().poll() == []

    def test_since_filter(self, respx_mock):
        respx_mock.get(f"{API}/v6/deployments").mock(
            return_value=httpx.Response(
                200, json={"deployments": [_deployment(readyState="ERROR")]}
            )
        )
        assert _source().poll(since="2099-01-01T00:00:00+00:00") == []

    def test_signals_are_untrusted(self, respx_mock):
        respx_mock.get(f"{API}/v6/deployments").mock(
            return_value=httpx.Response(
                200, json={"deployments": [_deployment(readyState="ERROR")]}
            )
        )
        assert _source().poll()[0].trust.value == "external"

    def test_poll_never_raises(self, respx_mock):
        respx_mock.get(f"{API}/v6/deployments").mock(return_value=httpx.Response(500))
        assert _source().poll() == []

    def test_poll_without_token_returns_empty(self, monkeypatch):
        monkeypatch.delenv("VERCEL_READONLY_TOKEN", raising=False)
        assert VercelSource(project_id=PROJECT).poll() == []

    def test_health_ok(self, respx_mock):
        respx_mock.get(f"{API}/v9/projects/{PROJECT}").mock(
            return_value=httpx.Response(200, json={"id": PROJECT})
        )
        assert _source().health().reachable

    def test_health_missing_token_names_the_variable(self, monkeypatch):
        monkeypatch.delenv("VERCEL_READONLY_TOKEN", raising=False)
        health = VercelSource(project_id=PROJECT).health()
        assert not health.reachable
        assert "VERCEL_READONLY_TOKEN" in health.detail
