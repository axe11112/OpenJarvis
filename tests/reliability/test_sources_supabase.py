"""Tests for the Supabase source."""

from __future__ import annotations

import httpx
import pytest

from openjarvis.reliability.sources._stubs import ResilientClient
from openjarvis.reliability.sources.sql_guard import WriteGateClosedError
from openjarvis.reliability.sources.supabase import SupabaseSource
from openjarvis.reliability.types import Severity

API = "https://api.supabase.com"
REF = "abcdefgh"


def _source(**kwargs) -> SupabaseSource:
    kwargs.setdefault("project_ref", REF)
    kwargs.setdefault(
        "client",
        ResilientClient(
            base_url=API,
            source="supabase",
            headers={"Authorization": "Bearer test"},
            sleep=lambda _: None,
            jitter=lambda: 0.0,
        ),
    )
    return SupabaseSource(**kwargs)


def _logs(respx_mock, rows):
    respx_mock.get(f"{API}/v1/projects/{REF}/analytics/endpoints/logs.all").mock(
        return_value=httpx.Response(200, json={"result": rows})
    )


class TestProjectState:
    def test_get_project(self, respx_mock):
        respx_mock.get(f"{API}/v1/projects/{REF}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": REF,
                    "name": "site",
                    "status": "ACTIVE_HEALTHY",
                    "region": "eu-west-1",
                },
            )
        )
        project = _source().get_project()
        assert project["status"] == "ACTIVE_HEALTHY"

    def test_list_migrations(self, respx_mock):
        respx_mock.get(f"{API}/v1/projects/{REF}/database/migrations").mock(
            return_value=httpx.Response(
                200, json=[{"version": "20260101", "name": "init"}]
            )
        )
        assert _source().list_migrations()[0]["version"] == "20260101"

    def test_list_edge_functions(self, respx_mock):
        respx_mock.get(f"{API}/v1/projects/{REF}/functions").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"slug": "hello", "name": "hello", "status": "ACTIVE", "version": 3}
                ],
            )
        )
        assert _source().list_edge_functions()[0]["slug"] == "hello"

    def test_migration_drift(self, respx_mock):
        """Schema drift is invisible to a browser probe but 500s in production."""
        respx_mock.get(f"{API}/v1/projects/{REF}/database/migrations").mock(
            return_value=httpx.Response(200, json=[{"version": "1", "name": "a"}])
        )
        drift = _source().detect_migration_drift(["1", "2", "3"])
        assert drift == ["2", "3"]

    def test_migration_drift_survives_api_failure(self, respx_mock):
        respx_mock.get(f"{API}/v1/projects/{REF}/database/migrations").mock(
            return_value=httpx.Response(500)
        )
        assert _source().detect_migration_drift(["1"]) == []


class TestLogs:
    def test_query_logs(self, respx_mock):
        _logs(respx_mock, [{"timestamp": "t", "event_message": "hello"}])
        assert len(_source().query_logs()) == 1

    def test_log_query_goes_through_the_guard(self, respx_mock):
        """Even a 'log query' must not be able to drop a table."""
        with pytest.raises(WriteGateClosedError):
            _source().query_logs(sql="DROP TABLE users")

    def test_api_failure_returns_empty(self, respx_mock):
        respx_mock.get(f"{API}/v1/projects/{REF}/analytics/endpoints/logs.all").mock(
            return_value=httpx.Response(500)
        )
        assert _source().query_logs() == []

    def test_non_list_result(self, respx_mock):
        respx_mock.get(f"{API}/v1/projects/{REF}/analytics/endpoints/logs.all").mock(
            return_value=httpx.Response(200, json={"result": "oops"})
        )
        assert _source().query_logs() == []


class TestAuthDiagnostics:
    def test_counts_failures_by_kind(self, respx_mock):
        _logs(
            respx_mock,
            [
                {"event_message": "invalid_grant for request"},
                {"event_message": "invalid_grant again"},
                {"event_message": "Email not confirmed"},
                {"event_message": "all good"},
            ],
        )
        result = _source().auth_diagnostics()
        assert result["failure_count"] == 3
        assert result["by_kind"]["invalid_grant"] == 2

    def test_reports_counts_not_records(self, respx_mock):
        """Aggregates only — JARVIS never reads user records."""
        _logs(respx_mock, [{"event_message": "invalid_grant for alice@example.com"}])
        result = _source().auth_diagnostics()
        assert set(result) == {"sampled", "failure_count", "by_kind"}
        assert "alice@example.com" not in str(result)

    def test_no_failures(self, respx_mock):
        _logs(respx_mock, [{"event_message": "fine"}])
        assert _source().auth_diagnostics()["failure_count"] == 0


class TestRlsDiagnostics:
    def test_detects_denials(self, respx_mock):
        _logs(
            respx_mock,
            [
                {
                    "timestamp": "2026-08-14T10:00:00Z",
                    "event_message": (
                        'new row violates row-level security policy for table "orders"'
                    ),
                },
                {"event_message": "unrelated"},
            ],
        )
        findings = _source().rls_diagnostics()
        assert len(findings) == 1
        assert "orders" in findings[0]["message"]

    def test_no_denials(self, respx_mock):
        _logs(respx_mock, [{"event_message": "fine"}])
        assert _source().rls_diagnostics() == []


class TestExecuteSql:
    def test_read_is_allowed(self, respx_mock):
        respx_mock.post(f"{API}/v1/projects/{REF}/database/query").mock(
            return_value=httpx.Response(200, json=[{"count": 3}])
        )
        assert _source().execute_sql("SELECT count(*) FROM t") == [{"count": 3}]

    def test_write_refused_with_the_gate_closed(self):
        with pytest.raises(WriteGateClosedError, match="allow_production_writes"):
            _source().execute_sql("INSERT INTO t VALUES (1)", force=True)

    def test_write_refused_without_force_even_when_configured(self):
        """Both gates must be open: config alone is not enough."""
        source = _source(allow_production_writes=True)
        with pytest.raises(WriteGateClosedError):
            source.execute_sql("INSERT INTO t VALUES (1)")

    def test_write_allowed_when_every_gate_is_open(self, respx_mock):
        respx_mock.post(f"{API}/v1/projects/{REF}/database/query").mock(
            return_value=httpx.Response(200, json=[])
        )
        source = _source(allow_production_writes=True)
        assert source.execute_sql("INSERT INTO t VALUES (1)", force=True) == []

    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE users",
            "TRUNCATE users",
            "ALTER TABLE t DISABLE ROW LEVEL SECURITY",
            "GRANT ALL ON t TO anon",
            "DELETE FROM users",
        ],
    )
    def test_destructive_refused_even_fully_unlocked(self, sql):
        """No combination of flags permits these."""
        source = _source(allow_production_writes=True)
        with pytest.raises(WriteGateClosedError):
            source.execute_sql(sql, force=True)

    def test_non_json_response(self, respx_mock):
        respx_mock.post(f"{API}/v1/projects/{REF}/database/query").mock(
            return_value=httpx.Response(200, text="not json")
        )
        assert _source().execute_sql("SELECT 1") == []


class TestPollAndHealth:
    def _project(self, respx_mock, status="ACTIVE_HEALTHY"):
        respx_mock.get(f"{API}/v1/projects/{REF}").mock(
            return_value=httpx.Response(200, json={"id": REF, "status": status})
        )

    def test_healthy_project_no_signals(self, respx_mock):
        self._project(respx_mock)
        _logs(respx_mock, [])
        assert _source().poll() == []

    def test_paused_project_is_critical(self, respx_mock):
        self._project(respx_mock, status="PAUSED")
        _logs(respx_mock, [])
        signals = _source().poll()
        assert signals[0].severity is Severity.CRITICAL
        assert signals[0].kind == "project_unhealthy"

    def test_degraded_status_is_high(self, respx_mock):
        self._project(respx_mock, status="RESTORING")
        _logs(respx_mock, [])
        assert _source().poll()[0].severity is Severity.HIGH

    def test_rls_denials_become_a_signal(self, respx_mock):
        self._project(respx_mock)
        _logs(
            respx_mock,
            [{"event_message": "violates row-level security policy", "timestamp": "t"}],
        )
        signals = _source().poll()
        assert any(s.kind == "rls_denials" for s in signals)

    def test_poll_never_raises(self, respx_mock):
        respx_mock.get(f"{API}/v1/projects/{REF}").mock(
            return_value=httpx.Response(500)
        )
        assert _source().poll() == []

    def test_poll_without_token(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_READONLY_TOKEN", raising=False)
        assert SupabaseSource(project_ref=REF).poll() == []

    def test_health_ok(self, respx_mock):
        self._project(respx_mock)
        health = _source().health()
        assert health.reachable
        assert not health.degraded

    def test_health_degraded(self, respx_mock):
        self._project(respx_mock, status="RESTORING")
        assert _source().health().degraded

    def test_health_missing_token_names_the_variable(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_READONLY_TOKEN", raising=False)
        health = SupabaseSource(project_ref=REF).health()
        assert not health.reachable
        assert "SUPABASE_READONLY_TOKEN" in health.detail
