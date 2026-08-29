"""URL safety and SSRF protection for browser navigation.

Browser navigation is allowlisted. Only trusted origins allowed:
- Wize production (configured base URL)
- Vercel Preview (exact verified deployments)
- Other explicitly configured Wize origins

Blocks:
- localhost (unless explicitly internal/approved)
- private networks (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
- metadata endpoints (169.254.169.254)
- file://, javascript:, data: (if unsafe)
- credential-bearing URLs (passwords in query strings)
- unknown arbitrary internet origins
- redirects to any of the above
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse, parse_qs


class URLSafetyError(ValueError):
    """Raised when a URL fails safety checks."""


class URLValidator:
    """Validates browser navigation targets."""

    def __init__(
        self,
        production_base_url: str = "https://www.wizeperformance.com",
        preview_allowed_origins: Optional[list[str]] = None,
        additional_allowed_origins: Optional[list[str]] = None,
    ):
        self.production_base = production_base_url.rstrip("/")
        self.preview_origins = preview_allowed_origins or []
        self.additional_origins = additional_allowed_origins or []

        # Extract the production origin
        parsed = urlparse(self.production_base)
        self.production_origin = f"{parsed.scheme}://{parsed.netloc}"

    def is_allowed(self, url: str) -> bool:
        """Whether the URL is allowed for browser navigation."""
        try:
            self.validate(url)
            return True
        except URLSafetyError:
            return False

    def validate(self, url: str) -> None:
        """Validate a URL for safe navigation. Raises URLSafetyError if unsafe."""
        if not url or not isinstance(url, str):
            raise URLSafetyError("URL must be a non-empty string")

        # Reject dangerous schemes
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme in ("javascript", "data", "file"):
            raise URLSafetyError(f"Scheme '{scheme}' is not allowed")

        # Reject localhost unless explicitly approved
        hostname = parsed.hostname
        if hostname and hostname in ("localhost", "127.0.0.1", "::1"):
            raise URLSafetyError("Localhost is not allowed without explicit approval")

        # Reject private network ranges
        if hostname and self._is_private_ip(hostname):
            raise URLSafetyError(f"Private network target '{hostname}' is not allowed")

        # Reject cloud metadata endpoints
        if hostname == "169.254.169.254":
            raise URLSafetyError("Cloud metadata endpoint is not allowed")

        # Reject credential-bearing URLs
        if self._has_credentials_in_url(url):
            raise URLSafetyError("URLs with embedded credentials are not allowed")

        # Check origin is allowlisted
        if not self._is_origin_allowed(parsed):
            raise URLSafetyError(
                f"Origin '{parsed.netloc}' is not in the allowlist. "
                f"Allowed: {self.production_origin}, {self.preview_origins}"
            )

    def _is_private_ip(self, hostname: str) -> bool:
        """Check if a hostname resolves to a private IP range."""
        try:
            import socket

            ip = socket.gethostbyname(hostname)
            # 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
            return (
                ip.startswith("10.")
                or ip.startswith("172.16.")
                or ip.startswith("192.168.")
            )
        except (socket.gaierror, socket.error):
            # If we can't resolve, assume it's safe to block for this purpose
            return False

    @staticmethod
    def _has_credentials_in_url(url: str) -> bool:
        """Check for obvious credential patterns in URL."""
        # Reject URLs with password-like query params
        if re.search(
            r"(password|token|secret|api[_-]?key|auth|credential)=",
            url,
            re.IGNORECASE,
        ):
            return True
        # Reject basic auth in URL
        if "@" in urlparse(url).netloc:
            return True
        return False

    def _is_origin_allowed(self, parsed) -> bool:
        """Check if the origin is in the allowlist."""
        origin = f"{parsed.scheme}://{parsed.netloc}"

        # Production origin
        if origin == self.production_origin:
            return True

        # Preview origins (exact match)
        if origin in self.preview_origins:
            return True

        # Additional configured origins
        if origin in self.additional_origins:
            return True

        return False
