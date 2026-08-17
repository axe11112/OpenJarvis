"""Tests for the resilient HTTP client and circuit breaker."""

from __future__ import annotations

import httpx
import pytest

from openjarvis.reliability.sources._stubs import (
    CircuitBreaker,
    CircuitOpenError,
    MissingTokenError,
    ResilientClient,
    resolve_token,
)

BASE = "https://api.example.com"


def _client(**kwargs) -> ResilientClient:
    kwargs.setdefault("sleep", lambda _: None)
    kwargs.setdefault("jitter", lambda: 0.0)
    return ResilientClient(base_url=BASE, source="test", **kwargs)


class TestResolveToken:
    def test_reads_env(self, monkeypatch):
        monkeypatch.setenv("SOME_TOKEN", "value")
        assert resolve_token("SOME_TOKEN", source="test") == "value"

    def test_missing_names_the_variable_only(self, monkeypatch):
        monkeypatch.delenv("ABSENT_TOKEN", raising=False)
        with pytest.raises(MissingTokenError, match=r"\$ABSENT_TOKEN is not set"):
            resolve_token("ABSENT_TOKEN", source="test")

    def test_empty_env_name(self):
        with pytest.raises(MissingTokenError, match="no token_env configured"):
            resolve_token("", source="test")


class TestCircuitBreaker:
    def test_starts_closed(self):
        breaker = CircuitBreaker()
        assert breaker.state == "closed"
        assert breaker.allow()

    def test_opens_at_threshold(self):
        breaker = CircuitBreaker(failure_threshold=3)
        for _ in range(2):
            breaker.record_failure()
        assert breaker.state == "closed"
        breaker.record_failure()
        assert breaker.state == "open"
        assert not breaker.allow()

    def test_success_resets_the_count(self):
        breaker = CircuitBreaker(failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        assert breaker.state == "closed"

    def test_half_opens_after_timeout(self):
        now = [0.0]
        breaker = CircuitBreaker(
            failure_threshold=1, reset_timeout=10.0, clock=lambda: now[0]
        )
        breaker.record_failure()
        assert not breaker.allow()
        now[0] = 11.0
        assert breaker.allow()
        assert breaker.state == "half-open"

    def test_half_open_success_closes(self):
        now = [0.0]
        breaker = CircuitBreaker(
            failure_threshold=1, reset_timeout=10.0, clock=lambda: now[0]
        )
        breaker.record_failure()
        now[0] = 11.0
        breaker.allow()
        breaker.record_success()
        assert breaker.state == "closed"

    def test_half_open_failure_reopens_and_restarts_the_timer(self):
        now = [0.0]
        breaker = CircuitBreaker(
            failure_threshold=1, reset_timeout=10.0, clock=lambda: now[0]
        )
        breaker.record_failure()
        now[0] = 11.0
        breaker.allow()
        breaker.record_failure()
        assert breaker.state == "open"
        assert not breaker.allow()

    def test_reset(self):
        breaker = CircuitBreaker(failure_threshold=1)
        breaker.record_failure()
        breaker.reset()
        assert breaker.state == "closed"


class TestResilientClient:
    def test_successful_get(self, respx_mock):
        respx_mock.get(f"{BASE}/thing").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        assert _client().get_json("/thing") == {"ok": True}

    def test_retries_500_then_succeeds(self, respx_mock):
        route = respx_mock.get(f"{BASE}/flaky").mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        assert _client().get_json("/flaky") == {"ok": True}
        assert route.call_count == 2

    def test_gives_up_after_max_retries(self, respx_mock):
        route = respx_mock.get(f"{BASE}/down").mock(return_value=httpx.Response(503))
        with pytest.raises(httpx.HTTPStatusError):
            _client(max_retries=2).get_json("/down")
        assert route.call_count == 3

    def test_404_is_not_retried(self, respx_mock):
        """A 404 will not improve with retrying — surface it immediately."""
        route = respx_mock.get(f"{BASE}/missing").mock(return_value=httpx.Response(404))
        with pytest.raises(httpx.HTTPStatusError):
            _client().get_json("/missing")
        assert route.call_count == 1

    def test_401_is_not_retried(self, respx_mock):
        route = respx_mock.get(f"{BASE}/private").mock(return_value=httpx.Response(401))
        with pytest.raises(httpx.HTTPStatusError):
            _client().get_json("/private")
        assert route.call_count == 1

    def test_429_is_retried(self, respx_mock):
        route = respx_mock.get(f"{BASE}/limited").mock(
            side_effect=[httpx.Response(429), httpx.Response(200, json=[])]
        )
        assert _client().get_json("/limited") == []
        assert route.call_count == 2

    def test_retry_after_header_is_honoured(self, respx_mock):
        slept = []
        respx_mock.get(f"{BASE}/limited").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "7"}),
                httpx.Response(200, json={}),
            ]
        )
        client = _client(sleep=slept.append)
        client.get_json("/limited")
        assert slept == [7.0]

    def test_retry_after_is_capped(self, respx_mock):
        slept = []
        respx_mock.get(f"{BASE}/limited").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "99999"}),
                httpx.Response(200, json={}),
            ]
        )
        client = _client(sleep=slept.append, backoff_cap=30.0)
        client.get_json("/limited")
        assert slept == [30.0]

    def test_rate_limit_reset_header_is_honoured(self, respx_mock):
        """GitHub signals exhaustion with Remaining: 0 and an absolute reset
        timestamp rather than Retry-After."""
        import time

        slept = []
        respx_mock.get(f"{BASE}/limited").mock(
            side_effect=[
                httpx.Response(
                    429,
                    headers={
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(int(time.time()) + 5),
                    },
                ),
                httpx.Response(200, json={}),
            ]
        )
        _client(sleep=slept.append).get_json("/limited")
        assert len(slept) == 1
        assert 3.0 <= slept[0] <= 6.0

    def test_403_is_not_retried(self, respx_mock):
        route = respx_mock.get(f"{BASE}/forbidden").mock(
            return_value=httpx.Response(403)
        )
        with pytest.raises(httpx.HTTPStatusError):
            _client().get_json("/forbidden")
        assert route.call_count == 1

    def test_backoff_grows_exponentially(self, respx_mock):
        slept = []
        respx_mock.get(f"{BASE}/down").mock(return_value=httpx.Response(500))
        client = _client(sleep=slept.append, max_retries=3, backoff_base=1.0)
        with pytest.raises(httpx.HTTPStatusError):
            client.get_json("/down")
        # jitter() is pinned to 0.0, so each delay is exactly half the ceiling.
        assert slept == [0.5, 1.0, 2.0]

    def test_connection_error_is_retried(self, respx_mock):
        route = respx_mock.get(f"{BASE}/net").mock(
            side_effect=[
                httpx.ConnectError("boom"),
                httpx.Response(200, json={"ok": 1}),
            ]
        )
        assert _client().get_json("/net") == {"ok": 1}
        assert route.call_count == 2

    def test_breaker_opens_after_repeated_failures(self, respx_mock):
        respx_mock.get(f"{BASE}/down").mock(return_value=httpx.Response(500))
        breaker = CircuitBreaker(failure_threshold=2)
        client = _client(max_retries=0, breaker=breaker)
        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                client.get_json("/down")
        assert breaker.state == "open"

    def test_open_breaker_short_circuits(self, respx_mock):
        route = respx_mock.get(f"{BASE}/down").mock(return_value=httpx.Response(500))
        breaker = CircuitBreaker(failure_threshold=1)
        client = _client(max_retries=0, breaker=breaker)
        with pytest.raises(httpx.HTTPStatusError):
            client.get_json("/down")
        calls_before = route.call_count
        with pytest.raises(CircuitOpenError):
            client.get_json("/down")
        assert route.call_count == calls_before  # no request was made

    def test_non_json_response_returns_default(self, respx_mock):
        respx_mock.get(f"{BASE}/html").mock(
            return_value=httpx.Response(200, text="<html></html>")
        )
        assert _client().get_json("/html", default={"fallback": True}) == {
            "fallback": True
        }

    def test_headers_are_sent(self, respx_mock):
        route = respx_mock.get(f"{BASE}/auth").mock(
            return_value=httpx.Response(200, json={})
        )
        client = _client(headers={"Authorization": "Bearer abc"})
        client.get_json("/auth")
        assert route.calls[0].request.headers["Authorization"] == "Bearer abc"
