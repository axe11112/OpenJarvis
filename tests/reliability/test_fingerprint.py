"""Tests for incident fingerprint stability."""

from __future__ import annotations

import pytest

from openjarvis.reliability.fingerprint import fingerprint, normalize_error


class TestNormalizeError:
    def test_empty(self):
        assert normalize_error("") == ""

    @pytest.mark.parametrize(
        "text",
        [
            "Failed at 2026-08-14T10:42:00+02:00",
            "Failed at 2026-08-15T23:11:59Z",
            "Failed at 2026-01-02 03:04:05.123456",
        ],
    )
    def test_timestamps_collapse(self, text):
        assert normalize_error(text) == "failed at <ts>"

    def test_uuid_collapses(self):
        a = normalize_error("request 3f2504e0-4f89-11d3-9a0c-0305e82c3301 failed")
        b = normalize_error("request 550e8400-e29b-41d4-a716-446655440000 failed")
        assert a == b

    def test_hex_blob_collapses(self):
        a = normalize_error("commit a1b2c3d4e5f is broken")
        b = normalize_error("commit ffffffffff0 is broken")
        assert a == b

    def test_port_collapses(self):
        a = normalize_error("connect to localhost:51234 refused")
        b = normalize_error("connect to localhost:8080 refused")
        assert a == b

    def test_durations_collapse(self):
        a = normalize_error("Timeout after 30013ms waiting for #login")
        b = normalize_error("Timeout after 29998ms waiting for #login")
        assert a == b
        assert "<size>" in a

    def test_query_string_collapses(self):
        a = normalize_error("GET /api/me?t=1723627320 failed")
        b = normalize_error("GET /api/me?t=9999999999 failed")
        assert a == b

    def test_line_numbers_collapse(self):
        a = normalize_error("TypeError at line 42")
        b = normalize_error("TypeError at line 1337")
        assert a == b

    def test_case_insensitive(self):
        assert normalize_error("Boom") == normalize_error("BOOM")

    def test_whitespace_collapses(self):
        assert normalize_error("a   b\n\tc") == "a b c"

    def test_distinct_messages_stay_distinct(self):
        assert normalize_error("login redirect loop") != normalize_error("500 on /api")


class TestFingerprint:
    def test_deterministic(self):
        kwargs = dict(component="auth", failure_kind="assertion", probe_id="login")
        assert fingerprint(**kwargs) == fingerprint(**kwargs)

    def test_prefixed_and_short(self):
        value = fingerprint(component="auth", failure_kind="assertion")
        assert value.startswith("fp_")
        assert len(value) == len("fp_") + 16

    def test_same_failure_different_volatile_detail(self):
        """The whole point: a flapping failure keeps one fingerprint."""
        first = fingerprint(
            component="auth",
            failure_kind="timeout",
            probe_id="login",
            error="Timeout 30011ms waiting for #dash at 2026-08-14T10:42:00Z",
        )
        second = fingerprint(
            component="auth",
            failure_kind="timeout",
            probe_id="login",
            error="Timeout 29874ms waiting for #dash at 2026-08-14T10:47:31Z",
        )
        assert first == second

    def test_different_component_differs(self):
        a = fingerprint(component="auth", failure_kind="assertion")
        b = fingerprint(component="billing", failure_kind="assertion")
        assert a != b

    def test_different_failure_kind_differs(self):
        a = fingerprint(component="auth", failure_kind="assertion")
        b = fingerprint(component="auth", failure_kind="timeout")
        assert a != b

    def test_different_probe_differs(self):
        a = fingerprint(component="auth", failure_kind="assertion", probe_id="login")
        b = fingerprint(component="auth", failure_kind="assertion", probe_id="signup")
        assert a != b

    def test_extra_discriminators_matter(self):
        a = fingerprint(component="auth", failure_kind="assertion", extra=["#email"])
        b = fingerprint(component="auth", failure_kind="assertion", extra=["#password"])
        assert a != b

    def test_component_case_insensitive(self):
        a = fingerprint(component="Auth", failure_kind="Assertion")
        b = fingerprint(component="auth", failure_kind="assertion")
        assert a == b
