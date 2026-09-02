"""Concrete security scanners — secrets and PII detection."""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

from openjarvis._rust_bridge import get_rust_module, scan_result_from_json
from openjarvis.security._stubs import BaseScanner
from openjarvis.security.types import ScanFinding, ScanResult, ThreatLevel

# ---------------------------------------------------------------------------
# SecretScanner
# ---------------------------------------------------------------------------


class SecretScanner(BaseScanner):
    """Detect API keys, tokens, passwords, and other secrets in text."""

    scanner_id = "secrets"

    def __init__(self) -> None:
        _rust = get_rust_module()
        self._rust_impl = _rust.SecretScanner()

    PATTERNS: Dict[str, Tuple[str, ThreatLevel, str]] = {
        "openai_key": (
            r"sk-[A-Za-z0-9_-]{20,}",
            ThreatLevel.CRITICAL,
            "OpenAI API key",
        ),
        "anthropic_key": (
            r"sk-ant-[A-Za-z0-9_-]{20,}",
            ThreatLevel.CRITICAL,
            "Anthropic API key",
        ),
        "aws_access_key": (
            r"AKIA[0-9A-Z]{16}",
            ThreatLevel.CRITICAL,
            "AWS access key",
        ),
        "github_token": (
            r"(?:ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_]{36,}",
            ThreatLevel.CRITICAL,
            "GitHub token",
        ),
        "password_assignment": (
            r"""(?:password|passwd|pwd)\s*[=:]\s*['"]([^'"]{4,})['"]""",
            ThreatLevel.HIGH,
            "Password assignment",
        ),
        "db_connection_string": (
            r"(?:postgres|mysql|mongodb|redis)://[^\s]{10,}",
            ThreatLevel.HIGH,
            "Database connection string",
        ),
        "private_key": (
            r"-----BEGIN (?:RSA )?PRIVATE KEY-----",
            ThreatLevel.CRITICAL,
            "Private key",
        ),
        "slack_token": (
            r"xox[bpors]-[A-Za-z0-9\-]{10,}",
            ThreatLevel.HIGH,
            "Slack token",
        ),
        "stripe_key": (
            r"(?:sk|pk)_(?:test|live)_[A-Za-z0-9]{20,}",
            ThreatLevel.CRITICAL,
            "Stripe key",
        ),
        "generic_api_key": (
            r"""(?:api_key|secret_key|auth_token)\s*[=:]\s*['"]([^'"]{8,})['"]""",
            ThreatLevel.HIGH,
            "Generic API key/secret",
        ),
    }

    def scan(self, text: str) -> ScanResult:
        """Scan *text* for secret patterns — always via Rust backend."""
        return scan_result_from_json(self._rust_impl.scan(text))

    def redact(self, text: str) -> str:
        """Replace secret matches with ``[REDACTED:{pattern_name}]``."""
        return self._rust_impl.redact(text)


# ---------------------------------------------------------------------------
# PIIScanner
# ---------------------------------------------------------------------------


class PIIScanner(BaseScanner):
    """Detect personally identifiable information in text."""

    scanner_id = "pii"

    def __init__(self) -> None:
        _rust = get_rust_module()
        self._rust_impl = _rust.PIIScanner()

    PATTERNS: Dict[str, Tuple[str, ThreatLevel, str]] = {
        "email": (
            r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
            ThreatLevel.MEDIUM,
            "Email address",
        ),
        "us_ssn": (
            r"\b\d{3}-\d{2}-\d{4}\b",
            ThreatLevel.CRITICAL,
            "US Social Security Number",
        ),
        "credit_card_visa": (
            r"\b4\d{3}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",
            ThreatLevel.CRITICAL,
            "Visa credit card",
        ),
        "credit_card_mastercard": (
            r"\b5[1-5]\d{2}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",
            ThreatLevel.CRITICAL,
            "Mastercard credit card",
        ),
        "credit_card_amex": (
            r"\b3[47]\d{2}[\s-]?\d{6}[\s-]?\d{5}\b",
            ThreatLevel.CRITICAL,
            "Amex credit card",
        ),
        "us_phone": (
            r"\b(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
            ThreatLevel.MEDIUM,
            "US phone number",
        ),
        "ipv4_public": (
            r"\b(?!10\.)(?!172\.(?:1[6-9]|2\d|3[01])\.)(?!192\.168\.)(?!127\.)(?!0\.)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
            ThreatLevel.LOW,
            "Public IPv4 address",
        ),
    }

    def scan(self, text: str) -> ScanResult:
        """Scan *text* for PII patterns — always via Rust backend."""
        return scan_result_from_json(self._rust_impl.scan(text))

    def redact(self, text: str) -> str:
        """Replace PII matches with ``[REDACTED:{pattern_name}]``."""
        return self._rust_impl.redact(text)


# ---------------------------------------------------------------------------
# Pure-Python fallback, for when the compiled extension is unavailable
# ---------------------------------------------------------------------------


class _PatternTableScanner(BaseScanner):
    """Scan/redact from a plain ``PATTERNS`` dict, in pure Python.

    Found on a real, silent gap: :class:`~openjarvis.security.boundary.
    BoundaryGuard` fell back to an *empty* scanner list whenever the
    compiled Rust extension was unavailable — its own constructor caught
    exactly that ImportError and returned ``[]`` rather than raising —
    which made every :meth:`~.boundary.BoundaryGuard.scan_outbound` call a
    silent no-op: no exception for a caller's own fallback path to catch,
    just text returned unchanged. A real credential reached a live HTTP
    response through exactly this gap (an incident's evidence, rendered by
    the Control Center dashboard) on a machine where the extension had
    never been built.

    Reuses ``SecretScanner.PATTERNS``/``PIIScanner.PATTERNS`` directly
    rather than keeping a third copy: a pattern added for the compiled
    scanner is picked up here automatically, matching the same reasoning
    :func:`~openjarvis.reliability.briefing._scan_with_pattern_table`
    already applied for boolean-only detection — this is its scan+redact
    counterpart, usable as a real, if weaker-coverage, drop-in ``BaseScanner``.
    """

    def __init__(
        self, scanner_id: str, patterns: Dict[str, Tuple[str, ThreatLevel, str]]
    ) -> None:
        self.scanner_id = scanner_id
        self._patterns = patterns

    def scan(self, text: str) -> ScanResult:
        findings: List[ScanFinding] = []
        for name, (pattern, level, label) in self._patterns.items():
            try:
                for match in re.finditer(pattern, text, re.IGNORECASE):
                    findings.append(
                        ScanFinding(
                            pattern_name=name,
                            matched_text=match.group(0),
                            threat_level=level,
                            start=match.start(),
                            end=match.end(),
                            description=f"{label} (pattern-table fallback)",
                        )
                    )
            except re.error:  # pragma: no cover - a malformed pattern is not a finding
                continue
        return ScanResult(findings=findings)

    def redact(self, text: str) -> str:
        redacted = text
        for name, (pattern, _level, _label) in self._patterns.items():
            try:
                redacted = re.sub(
                    pattern, f"[REDACTED:{name}]", redacted, flags=re.IGNORECASE
                )
            except re.error:  # pragma: no cover - a malformed pattern redacts nothing
                continue
        return redacted


def fallback_scanners() -> List[BaseScanner]:
    """Pure-Python stand-ins for ``[SecretScanner(), PIIScanner()]``.

    Used by :class:`~openjarvis.security.boundary.BoundaryGuard` when the
    compiled Rust extension cannot be imported, so a machine that merely
    needs ``uv run maturin develop`` degrades to weaker pattern-table
    coverage instead of no coverage at all.
    """
    return [
        _PatternTableScanner("secrets-fallback", SecretScanner.PATTERNS),
        _PatternTableScanner("pii-fallback", PIIScanner.PATTERNS),
    ]


__all__ = ["PIIScanner", "SecretScanner", "fallback_scanners"]
