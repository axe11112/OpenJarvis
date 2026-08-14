"""Infrastructure signal sources — Vercel, Supabase, GitHub."""

from __future__ import annotations

from openjarvis.reliability.sources._stubs import (
    BaseSignalSource,
    CircuitBreaker,
    CircuitOpenError,
    MissingTokenError,
    ResilientClient,
    SourceHealth,
    resolve_token,
)
from openjarvis.reliability.sources.github import (
    GitHubSource,
    ProtectedPathError,
    UnsafeBranchError,
    is_protected_path,
)
from openjarvis.reliability.sources.sql_guard import (
    SqlVerdict,
    WriteGateClosedError,
    check_sql,
    is_read_only,
)
from openjarvis.reliability.sources.supabase import SupabaseSource
from openjarvis.reliability.sources.vercel import VercelSource

__all__ = [
    "BaseSignalSource",
    "CircuitBreaker",
    "CircuitOpenError",
    "GitHubSource",
    "MissingTokenError",
    "ProtectedPathError",
    "ResilientClient",
    "SourceHealth",
    "SqlVerdict",
    "SupabaseSource",
    "UnsafeBranchError",
    "VercelSource",
    "WriteGateClosedError",
    "check_sql",
    "is_read_only",
    "is_protected_path",
    "resolve_token",
]
