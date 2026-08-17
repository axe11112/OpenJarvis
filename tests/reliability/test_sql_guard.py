"""Tests for the SQL write guard.

The guard is the last thing standing between an autonomous agent and a
production database, so these tests are deliberately adversarial.
"""

from __future__ import annotations

import pytest

from openjarvis.reliability.sources.sql_guard import check_sql, is_read_only


class TestReadOnlyDetection:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM users",
            "select count(*) from orders where created_at > now()",
            "WITH recent AS (SELECT 1) SELECT * FROM recent",
            "EXPLAIN SELECT 1",
            "SHOW server_version",
        ],
    )
    def test_reads_are_read_only(self, sql):
        assert is_read_only(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET a = 1 WHERE id = 2",
            "DELETE FROM t WHERE id = 2",
            "ALTER TABLE t ADD COLUMN c int",
        ],
    )
    def test_writes_are_not_read_only(self, sql):
        assert not is_read_only(sql)

    def test_writing_cte_is_not_read_only(self):
        """A statement starting with SELECT/WITH can still delete rows."""
        sql = "WITH gone AS (DELETE FROM t WHERE id = 1 RETURNING *) SELECT * FROM gone"
        assert not is_read_only(sql)

    def test_empty_is_read_only(self):
        assert is_read_only("")


class TestNeverAllowed:
    @pytest.mark.parametrize(
        ("sql", "rule"),
        [
            ("DROP TABLE users", "drop"),
            ("DROP SCHEMA public CASCADE", "drop"),
            ("drop function foo()", "drop"),
            ("TRUNCATE users", "truncate"),
            ("ALTER TABLE t DISABLE ROW LEVEL SECURITY", "disable_rls"),
            ("DROP POLICY p ON t", "drop_policy"),
            ("CREATE POLICY p ON t FOR SELECT USING (true)", "modify_policy"),
            ("GRANT ALL ON t TO anon", "grant"),
            ("REVOKE SELECT ON t FROM authenticated", "grant"),
            ("CREATE ROLE evil", "role"),
            ("SELECT * FROM auth.users", "auth_schema"),
            ("SELECT * FROM vault.decrypted_secrets", "vault"),
            ("SELECT pg_read_file('/etc/passwd')", "dangerous_function"),
            ("SET ROLE postgres", "privilege_escalation"),
        ],
    )
    def test_refused(self, sql, rule):
        verdict = check_sql(sql)
        assert not verdict
        assert verdict.matched_rule == rule

    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE users",
            "ALTER TABLE t DISABLE ROW LEVEL SECURITY",
            "GRANT ALL ON t TO anon",
            "SELECT * FROM auth.users",
        ],
    )
    def test_not_overridable_by_the_write_gate(self, sql):
        """There is no configuration that permits these."""
        assert not check_sql(sql, allow_writes=True)

    def test_case_and_whitespace_insensitive(self):
        assert not check_sql("dRoP    TaBlE   users")
        assert not check_sql("drop\n\ttable users")


class TestEvasion:
    def test_line_comment_cannot_hide_a_drop(self):
        assert not check_sql("DROP --harmless\n TABLE users")

    def test_block_comment_cannot_hide_a_drop(self):
        assert not check_sql("DROP/*x*/TABLE users")

    def test_stacked_statement_is_caught(self):
        """The classic: a harmless-looking read followed by a destructive verb."""
        assert not check_sql("SELECT 1; DROP TABLE users")

    def test_stacked_write_behind_a_read(self):
        verdict = check_sql("SELECT 1; DELETE FROM t WHERE id = 1")
        assert not verdict
        assert verdict.matched_rule == "write_gate_closed"

    def test_leading_comment_only(self):
        assert not check_sql("-- just a comment")


class TestUnqualifiedWrites:
    def test_delete_without_where_is_refused_even_when_gated_open(self):
        verdict = check_sql("DELETE FROM users", allow_writes=True)
        assert not verdict
        assert verdict.matched_rule == "unqualified_write"

    def test_update_without_where_is_refused_even_when_gated_open(self):
        verdict = check_sql("UPDATE users SET admin = true", allow_writes=True)
        assert not verdict
        assert verdict.matched_rule == "unqualified_write"

    def test_delete_with_where_passes_when_gated_open(self):
        assert check_sql("DELETE FROM t WHERE id = 1", allow_writes=True)


class TestWriteGate:
    def test_reads_pass_with_the_gate_closed(self):
        verdict = check_sql("SELECT * FROM orders")
        assert verdict
        assert verdict.read_only

    def test_writes_refused_with_the_gate_closed(self):
        verdict = check_sql("INSERT INTO t VALUES (1)")
        assert not verdict
        assert verdict.matched_rule == "write_gate_closed"
        assert "allow_production_writes" in verdict.reason

    def test_writes_pass_with_the_gate_open(self):
        verdict = check_sql("INSERT INTO t VALUES (1)", allow_writes=True)
        assert verdict
        assert not verdict.read_only

    def test_empty_statement_is_refused(self):
        assert not check_sql("")

    def test_verdict_is_truthy(self):
        assert bool(check_sql("SELECT 1")) is True
        assert bool(check_sql("DROP TABLE t")) is False
