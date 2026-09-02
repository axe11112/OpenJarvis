from __future__ import annotations

import re
from typing import List, Tuple

_CREDENTIAL_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("api_key", re.compile(r"sk-[a-zA-Z0-9_-]{20,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    # {36,} — at least, not exactly. A fixed {36} only consumed the first 36
    # characters of a longer token (GitHub's classic tokens are 36, but a
    # fixture or a future/enterprise format can run longer) and left the
    # remainder as plain text after "[REDACTED:github_token]". Found live:
    # ghp_<39 chars> left "789" exposed after redaction, and because
    # bearer_token runs after this in the same pass, it never got a second
    # chance — the partial [REDACTED:...] replacement no longer looked like
    # "Bearer <20+ alnum chars>" for that pattern to catch either.
    # security/scanner.py's SecretScanner.PATTERNS already uses {36,} for
    # the identical case; this brings the two into agreement.
    ("github_token", re.compile(r"ghp_[a-zA-Z0-9]{36,}")),
    ("github_token", re.compile(r"gho_[a-zA-Z0-9]{36,}")),
    ("slack_token", re.compile(r"xoxb-[0-9A-Za-z\-]+")),
    ("bearer_token", re.compile(r"Bearer\s+[a-zA-Z0-9_\-.]{20,}")),
]


class CredentialStripper:
    """Redacts credentials from text using compiled regex patterns."""

    def __init__(self) -> None:
        self._patterns = _CREDENTIAL_PATTERNS

    def strip(self, text: str) -> str:
        for label, pattern in self._patterns:
            text = pattern.sub(f"[REDACTED:{label}]", text)
        return text


def wrap_tool_output(tool_name: str, content: str, success: bool = True) -> str:
    status = "success" if success else "error"
    header = f'<tool_result name="{tool_name}" status="{status}">'
    return f"{header}\n{content}\n</tool_result>"
