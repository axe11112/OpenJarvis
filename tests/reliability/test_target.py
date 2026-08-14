"""Tests for target identification and the connectivity preflight.

Both exist to stop an activation attempt failing for a reason nobody can see:
a variable spelled differently from the one JARVIS reads, or a network that
cannot reach the integration at all.
"""

from __future__ import annotations

import httpx
import pytest

from openjarvis.reliability.target import (
    accepted_names,
    connectivity_report,
    probe_host,
    resolve_target,
)


class TestEnvironmentAliases:
    """An operator should not have to guess which spelling JARVIS reads.

    Accepting only one form fails silently: the variable is exported, the value
    is ignored, and `doctor` still reports the identifier missing with nothing
    to suggest the name was the problem.
    """

    @pytest.mark.parametrize(
        ("variable", "attribute"),
        [
            ("TARGET_REPO", "repository"),
            ("GITHUB_REPOSITORY", "repository"),
            ("PRODUCTION_URL", "production_url"),
            ("TARGET_URL", "production_url"),
            ("VERCEL_PROJECT_ID", "vercel_project"),
            ("VERCEL_TEAM_ID", "vercel_team"),
            ("SUPABASE_PROJECT_ID", "supabase_ref"),
            ("SUPABASE_REF", "supabase_ref"),
        ],
    )
    def test_alias_spellings_are_honoured(self, monkeypatch, variable, attribute):
        from openjarvis.core.config import JarvisConfig

        monkeypatch.setenv(variable, "value-x")
        target = resolve_target(JarvisConfig())
        assert getattr(target, attribute) == "value-x"

    def test_the_canonical_name_wins_over_an_alias(self, monkeypatch):
        from openjarvis.core.config import JarvisConfig

        monkeypatch.setenv("TARGET_REPOSITORY", "canonical/repo")
        monkeypatch.setenv("TARGET_REPO", "alias/repo")
        assert resolve_target(JarvisConfig()).repository == "canonical/repo"

    def test_accepted_names_lists_every_spelling(self):
        rendered = accepted_names("TARGET_REPOSITORY")
        assert "$TARGET_REPOSITORY" in rendered
        assert "TARGET_REPO" in rendered

    def test_an_unknown_key_renders_itself(self):
        assert accepted_names("SOMETHING_ELSE") == "$SOMETHING_ELSE"


class TestConnectivityPreflight:
    """The preflight must reflect what the real client will experience."""

    def test_a_reachable_host_is_reported_reachable(self, respx_mock):
        respx_mock.get("https://api.github.com/").mock(return_value=httpx.Response(200))
        assert probe_host("api.github.com")[0] == "REACHABLE"

    def test_an_auth_error_still_counts_as_reachable(self, respx_mock):
        """A 401 means we got an answer, which is what is being tested."""
        respx_mock.get("https://api.github.com/").mock(return_value=httpx.Response(401))
        state, detail = probe_host("api.github.com")
        assert state == "REACHABLE"
        assert "401" in detail

    def test_a_refusing_proxy_is_BLOCKED_not_FAILED(self, respx_mock):
        """The request never left the network; the target is probably fine."""
        respx_mock.get("https://api.vercel.com/").mock(
            side_effect=httpx.ProxyError("403 Forbidden")
        )
        state, detail = probe_host("api.vercel.com")
        assert state == "BLOCKED"
        assert "proxy" in detail.lower()

    def test_a_connect_error_is_blocked(self, respx_mock):
        respx_mock.get("https://api.telegram.org/").mock(
            side_effect=httpx.ConnectError("no route to host")
        )
        assert probe_host("api.telegram.org")[0] == "BLOCKED"

    def test_the_report_covers_every_integration(self, respx_mock):
        from openjarvis.core.config import JarvisConfig

        for host in (
            "api.github.com",
            "api.vercel.com",
            "api.supabase.com",
            "api.telegram.org",
        ):
            respx_mock.get(f"https://{host}/").mock(return_value=httpx.Response(200))
        rows = connectivity_report(resolve_target(JarvisConfig()))
        names = {row["name"] for row in rows}
        assert {"GitHub", "Vercel", "Supabase", "Telegram", "Website"} <= names

    def test_an_unconfigured_website_names_the_variable(self, respx_mock):
        from openjarvis.core.config import JarvisConfig

        for host in (
            "api.github.com",
            "api.vercel.com",
            "api.supabase.com",
            "api.telegram.org",
        ):
            respx_mock.get(f"https://{host}/").mock(return_value=httpx.Response(200))
        website = next(
            r
            for r in connectivity_report(resolve_target(JarvisConfig()))
            if r["name"] == "Website"
        )
        assert website["state"] == "NOT_CONFIGURED"
        assert "PRODUCTION_URL" in website["detail"]
