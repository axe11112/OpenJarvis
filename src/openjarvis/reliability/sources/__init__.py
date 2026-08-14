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

__all__ = [
    "BaseSignalSource",
    "CircuitBreaker",
    "CircuitOpenError",
    "GitHubSource",
    "MissingTokenError",
    "ProtectedPathError",
    "ResilientClient",
    "SourceHealth",
    "UnsafeBranchError",
    "is_protected_path",
    "resolve_token",
]
