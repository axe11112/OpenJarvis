"""The scanner recognises the credentials this deployment actually holds.

Every pattern the secret scanner shipped with catches somebody else's provider
-- OpenAI, Anthropic, AWS, GitHub, Slack, Stripe. The three services JARVIS is
wired to are Supabase, Telegram and Vercel, and none of them matched anything.
So both redaction layers ran over a leaked Supabase service-role key -- the
highest-privilege credential in the system, the one that bypasses row-level
security -- reported the text clean, and passed it through to a log, a
notification, the journal or an HTTP response.

Every token below is obviously fake and structurally shaped like the real thing.
"""

from __future__ import annotations

import re

import pytest

from openjarvis.security.scanner import SecretScanner

# Structure-accurate, value-fake. "FAKE" appears in every secret part so a
# redaction failure is visible in an assertion rather than inferred.
FAKE_SUPABASE_SERVICE_ROLE = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJyb2xlIjoic2VydmljZV9yb2xlIiwiaXNzIjoic3VwYWJhc2UifQ"
    ".FAKEsignatureFAKEsignatureFAKE"
)
FAKE_TELEGRAM_BOT_TOKEN = "7123456789:AAFAKEfakeFAKEfakeFAKEfakeFAKEfakeFAK"
FAKE_VERCEL_TOKEN = "vercel_FAKEfakeFAKEfakeFAKEfake1234"
FAKE_BEARER_HEADER = "Authorization: Bearer FAKEfakeFAKEfakeFAKEfake1234"

DEPLOYMENT_CREDENTIALS = [
    pytest.param(FAKE_SUPABASE_SERVICE_ROLE, "FAKEsignature", id="supabase-jwt"),
    pytest.param(FAKE_TELEGRAM_BOT_TOKEN, "AAFAKEfake", id="telegram-bot-token"),
    pytest.param(FAKE_VERCEL_TOKEN, "FAKEfakeFAKEfake", id="vercel-token"),
    pytest.param(FAKE_BEARER_HEADER, "FAKEfakeFAKEfake", id="bare-bearer-header"),
]


class TestTheCredentialsThisSystemHolds:
    @pytest.mark.parametrize("text,secret_part", DEPLOYMENT_CREDENTIALS)
    def test_it_is_detected(self, text, secret_part):
        result = SecretScanner().scan(text)
        assert result.findings, f"not detected as a secret at all: {text[:40]}..."

    @pytest.mark.parametrize("text,secret_part", DEPLOYMENT_CREDENTIALS)
    def test_it_is_redacted(self, text, secret_part):
        """Detection without redaction still leaks; this is the property that
        matters at every egress -- a log line, a Telegram message, the journal,
        an HTTP response."""
        redacted = SecretScanner().redact(text)
        assert secret_part not in redacted, (
            f"the secret survived redaction: {redacted[:80]}"
        )

    def test_a_credential_in_an_env_assignment_is_caught_by_its_shape(self):
        """No pattern matches `NAME=<anything long>`, and none should.

        One was tried and removed: an opaque credential and an ordinary
        identifier are indistinguishable in that position. What catches this is
        the JWT's own shape, which is what makes it safe.
        """
        line = f"SUPABASE_SERVICE_ROLE_KEY={FAKE_SUPABASE_SERVICE_ROLE}"
        assert SecretScanner().scan(line).findings
        assert "FAKEsignature" not in SecretScanner().redact(line)

    def test_a_credential_embedded_in_a_sentence_is_still_caught(self):
        """How it actually leaks: inside subprocess output or a traceback."""
        line = (
            "error: request failed\n"
            f"  env: SUPABASE_KEY={FAKE_SUPABASE_SERVICE_ROLE}\n"
            "  retrying in 2s"
        )
        redacted = SecretScanner().redact(line)
        assert "FAKEsignature" not in redacted
        assert "retrying in 2s" in redacted, "redaction ate the surrounding text"


class TestNoFalsePositivesOnOrdinaryOperationalText:
    """Over-redaction is its own failure: it makes a diagnostic useless.

    These are the shapes that actually flow through this system's logs.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "The deployment is READY at https://wize.vercel.app/coach/summary",
            "attempt 3 failed: node_modules was never installed",
            "FEAT-00031 is READY; base_sha 4f857b0a1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f",
            "merge commit e1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4 verified",
            "git push origin wiz/feature/FEAT-00031 --force-with-lease",
            # The one that catches a careless `Bearer\\s+\\S+` pattern.
            "error: Bearer with me, this will take a moment",
            "vercel_project_id is not a secret",
            "Playwright timed out after 30000ms waiting for .download-btn",
            # Source code, which is what a repair briefing is made of. A
            # pattern matching `api_key = <anything long>` flagged all of
            # these, and has_critical_secret() refuses a briefing outright --
            # so it dead-ended every repair touching a file that assigns a
            # credential from a variable. Removed; these keep it out.
            "api_key = self._resolve_api_key_from_config()",
            "const apiKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY",
            "auth_token = build_auth_token(user_identifier)",
            "ACCESS_TOKEN = resolve_access_token_for_session()",
            "self.secret_key = configuration.secret_key_material",
            'ACCESS_TOKEN_HEADER = "X-Access-Token-Value-Here"',
        ],
    )
    def test_it_is_not_flagged(self, text):
        result = SecretScanner().scan(text)
        assert not result.findings, (
            f"ordinary text flagged as a secret by "
            f"{[f.pattern_name for f in result.findings]}: {text}"
        )


class TestTheFallbackProtectsExactlyAsMuchAsTheRustBackend:
    """The Python PATTERNS table is the fallback used when the compiled
    extension is unavailable. A fallback that protects less than the primary is
    worse than no fallback, because it reports clean with less coverage -- so
    the two tables have to carry the same pattern names.
    """

    def _rust_pattern_names(self) -> set:
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2]
            / "rust"
            / "crates"
            / "openjarvis-security"
            / "src"
            / "scanner.rs"
        ).read_text()
        secrets_block = source.split("static SECRET_PATTERNS")[1].split(
            "static PII_PATTERNS"
        )[0]
        return set(re.findall(r'pattern!\(\s*"([a-z0-9_]+)"', secrets_block))

    def test_the_two_tables_carry_the_same_patterns(self):
        rust_names = self._rust_pattern_names()
        assert rust_names, "could not parse the Rust secret pattern table"
        python_names = set(SecretScanner.PATTERNS)
        assert python_names == rust_names, (
            "the Rust backend and the pure-Python fallback disagree; "
            f"only in Rust: {sorted(rust_names - python_names)}; "
            f"only in Python: {sorted(python_names - rust_names)}"
        )

    @pytest.mark.parametrize("text,secret_part", DEPLOYMENT_CREDENTIALS)
    def test_the_fallback_also_redacts_them(self, text, secret_part):
        from openjarvis.security.scanner import _PatternTableScanner

        fallback = _PatternTableScanner("secrets-fallback", SecretScanner.PATTERNS)
        assert secret_part not in fallback.redact(text)
