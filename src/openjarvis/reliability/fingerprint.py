"""Stable incident fingerprints.

Two observations of the *same underlying problem* must produce the same
fingerprint even though their error text differs in timestamps, request IDs,
ports and line offsets.  Without this, a flapping failure opens a fresh
incident every few minutes and the repair loop never converges.

The normalizer is deliberately aggressive: over-merging two distinct problems is
recoverable (the incident gains extra evidence), while under-merging produces
incident spam and repeated repair attempts against the same bug.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, List

__all__ = ["fingerprint", "normalize_error"]

#: Ordered (pattern, placeholder) pairs applied to error text before hashing.
#: Order matters — the more specific patterns run first.
_NORMALIZERS: List[tuple[re.Pattern[str], str]] = [
    # ISO 8601 timestamps, with or without fractional seconds / offset
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?",
            re.IGNORECASE,
        ),
        "<ts>",
    ),
    # Bare clock times
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"), "<time>"),
    # UUIDs
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
        "<uuid>",
    ),
    # Long hex blobs: git SHAs, request IDs, memory addresses
    (re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE), "<addr>"),
    (re.compile(r"\b[0-9a-f]{7,}\b", re.IGNORECASE), "<hex>"),
    # Host:port and bare ports
    (re.compile(r":\d{2,5}\b"), ":<port>"),
    # Source locations — "file.ts:120:8", "line 42"
    (re.compile(r"\bline\s+\d+\b", re.IGNORECASE), "line <n>"),
    # Query strings and fragments carry cache-busters and session ids
    (re.compile(r"\?[^\s\"')]*"), "?<query>"),
    # Durations and byte counts
    (re.compile(r"\b\d+(?:\.\d+)?\s*(ms|s|kb|mb|gb)\b", re.IGNORECASE), "<size>"),
    # Any remaining standalone number
    (re.compile(r"\b\d+\b"), "<n>"),
    # Collapse whitespace last
    (re.compile(r"\s+"), " "),
]

#: Length of the hex digest kept in a fingerprint.  16 hex chars = 64 bits,
#: far beyond collision risk for the number of incidents a single site produces.
_DIGEST_LENGTH = 16


def normalize_error(text: str) -> str:
    """Strip volatile detail from *text* so equivalent errors compare equal.

    Lowercases, then replaces timestamps, UUIDs, hex blobs, ports, line numbers,
    query strings, sizes and bare numbers with stable placeholders.

    Examples
    --------
    >>> normalize_error("Timeout after 30013ms waiting for #login")
    'timeout after <size> waiting for #login'
    """
    if not text:
        return ""
    normalized = text.strip().lower()
    for pattern, placeholder in _NORMALIZERS:
        normalized = pattern.sub(placeholder, normalized)
    return normalized.strip()


def fingerprint(
    *,
    component: str,
    failure_kind: str,
    probe_id: str = "",
    error: str = "",
    extra: Iterable[str] = (),
) -> str:
    """Return a stable fingerprint for a failure.

    Parameters
    ----------
    component:
        Logical area, e.g. ``"authentication"``.
    failure_kind:
        Category of failure, e.g. ``"assertion"`` or ``"http_error"``.
    probe_id:
        Probe that observed the failure, when there was one.
    error:
        Raw error text — normalized via :func:`normalize_error` before hashing.
    extra:
        Additional discriminators (e.g. a failing selector or status code).

    Returns
    -------
    str
        ``"fp_"`` followed by 16 hex characters.
    """
    parts = [
        component.strip().lower(),
        failure_kind.strip().lower(),
        probe_id.strip().lower(),
        normalize_error(error),
        *(normalize_error(str(item)) for item in extra),
    ]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"fp_{digest[:_DIGEST_LENGTH]}"
