"""Change-scope control.

The protected-path guard answers "may this file be touched at all?". This module
answers a different and looser question: "is this diff the *shape* of the repair
we asked for?"

A coding agent asked to fix a login redirect and returning a 400-file diff has
not necessarily touched anything forbidden — it has done something nobody
intended, and the right response is to stop and fetch a human rather than to
push on and open a pull request. The same goes for a diff that quietly adds a
``.env``, rewrites a lockfile, or edits the CI configuration.

Every verdict carries its reasons, because "JARVIS stopped" is only actionable
if the owner can see what it saw.

Nothing here decides whether a repair *works* — that is verification's job, and
verification has final authority. This is a guard on blast radius, not on
correctness.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from openjarvis.reliability.sources.github import (
    is_protected_path,
    matches_path_pattern,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ScopeLimits",
    "ScopeVerdict",
    "assess_scope",
    "find_test_files",
    "SECRET_LIKE_PATHS",
    "INFRASTRUCTURE_PATHS",
]

#: Files that hold, or are shaped like, credentials. A repair has no legitimate
#: reason to create or modify one, and an agent that does has either been
#: injected into or has misunderstood the task badly enough to warrant a human.
SECRET_LIKE_PATHS = (
    ".env",
    ".env.*",
    "*.env",
    "**/.env",
    "**/.env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "id_rsa*",
    "id_ed25519*",
    "**/secrets.*",
    "**/credentials.*",
    "**/*credentials*",
    ".npmrc",
    ".netrc",
    ".pypirc",
    "**/service-account*.json",
)

#: Deployment, container and infrastructure definitions. Changing how the
#: application is *built and shipped* is not a bug fix.
INFRASTRUCTURE_PATHS = (
    "Dockerfile*",
    "**/Dockerfile*",
    "docker-compose*.y*ml",
    "**/docker-compose*.y*ml",
    "vercel.json",
    "**/vercel.json",
    "now.json",
    "fly.toml",
    "render.yaml",
    "Procfile",
    "**/*.tf",
    "**/*.tfvars",
    "k8s/**",
    "**/kustomization.y*ml",
    "**/helm/**",
    ".dockerignore",
)

#: Declarative security controls. Editing row-level security, a middleware
#: matcher or a migration to make a probe pass is the textbook way an automated
#: repair turns an outage into a breach, so these stop the loop.
#:
#: Note what is deliberately *not* here: ordinary application authentication
#: code. Blocking every file whose name contains "auth" would make JARVIS unable
#: to repair a login failure, which is the exact incident class it was built
#: for. Such changes are surfaced through :data:`REVIEW_REQUIRED_PATHS` instead —
#: reported loudly, and separately barred from automatic deployment by
#: :data:`SafetyPolicy.SECURITY_SENSITIVE` — but they do not abort the repair.
SECURITY_CONFIG_PATHS = (
    "**/*rls*",
    "**/*.policy.*",
    "**/policies/**",
    "middleware.*",
    "**/middleware.*",
    "supabase/migrations/**",
    "**/migrations/**",
    "**/*.sql",
)

#: Changes that a human should look at closely but that a repair may legitimately
#: need to make. Recorded on the verdict and surfaced in the pull request; never
#: a reason to stop on their own.
REVIEW_REQUIRED_PATHS = (
    "**/auth/**",
    "**/*auth*",
    "**/*session*",
    "**/*permission*",
    "**/*role*",
)


@dataclass(slots=True)
class ScopeLimits:
    """How large and how broad a repair diff may be before a human is needed.

    The defaults are deliberately generous: this guard exists to catch a runaway
    agent, not to second-guess a legitimate multi-file fix. Tightening them is a
    configuration decision.
    """

    max_files: int = 20
    max_lines_changed: int = 800
    #: When set, files outside these globs are reported as out-of-scope. Empty
    #: means "no declared expectation", which is the normal case — most
    #: incidents cannot predict which files the fix will land in.
    expected_paths: List[str] = field(default_factory=list)


@dataclass(slots=True)
class ScopeVerdict:
    """The result of assessing a diff's blast radius."""

    allowed: bool
    reasons: List[str] = field(default_factory=list)
    protected: List[str] = field(default_factory=list)
    secret_like: List[str] = field(default_factory=list)
    infrastructure: List[str] = field(default_factory=list)
    security_config: List[str] = field(default_factory=list)
    unexpected: List[str] = field(default_factory=list)
    #: Touched files a reviewer should read carefully. Never blocking.
    review_required: List[str] = field(default_factory=list)
    files_changed: int = 0
    lines_changed: int = 0

    def __bool__(self) -> bool:
        return self.allowed

    @property
    def reason(self) -> str:
        """All reasons as one sentence, for the audit log and notifications."""
        return "; ".join(self.reasons)

    def to_dict(self) -> dict:
        """Serialize for the incident record."""
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "protected": list(self.protected),
            "secret_like": list(self.secret_like),
            "infrastructure": list(self.infrastructure),
            "security_config": list(self.security_config),
            "unexpected": list(self.unexpected),
            "review_required": list(self.review_required),
            "files_changed": self.files_changed,
            "lines_changed": self.lines_changed,
        }


def _matching(paths: Sequence[str], patterns: Sequence[str]) -> List[str]:
    """Return the subset of *paths* matching any of *patterns*."""
    return sorted(
        {
            path
            for path in paths
            if any(matches_path_pattern(path, pattern) for pattern in patterns)
        }
    )


def assess_scope(
    changed_files: Sequence[str],
    *,
    limits: Optional[ScopeLimits] = None,
    lines_changed: int = 0,
    protected_paths: Optional[Sequence[str]] = None,
) -> ScopeVerdict:
    """Judge whether a diff is within the blast radius a repair may have.

    Every category is evaluated even after the first failure, so the owner sees
    the whole picture in one notification rather than discovering problems one
    escalation at a time.
    """
    limits = limits or ScopeLimits()
    paths = [p for p in changed_files if p]

    protected = sorted(
        {p for p in paths if is_protected_path(p, list(protected_paths or []))}
    )
    secret_like = _matching(paths, SECRET_LIKE_PATHS)
    infrastructure = _matching(paths, INFRASTRUCTURE_PATHS)
    security_config = _matching(paths, SECURITY_CONFIG_PATHS)
    review_required = _matching(paths, REVIEW_REQUIRED_PATHS)

    unexpected: List[str] = []
    if limits.expected_paths:
        unexpected = sorted(
            {
                path
                for path in paths
                if not any(
                    matches_path_pattern(path, pattern)
                    for pattern in limits.expected_paths
                )
            }
        )

    reasons: List[str] = []
    if protected:
        reasons.append("touches protected path(s): " + ", ".join(protected))
    if secret_like:
        reasons.append(
            "creates or modifies credential-bearing file(s): " + ", ".join(secret_like)
        )
    if infrastructure:
        reasons.append(
            "changes deployment/infrastructure definition(s): "
            + ", ".join(infrastructure)
        )
    if security_config:
        reasons.append(
            "changes declarative security configuration: " + ", ".join(security_config)
        )
    if unexpected:
        reasons.append(
            f"{len(unexpected)} file(s) outside the expected scope: "
            + ", ".join(unexpected[:10])
        )
    if len(paths) > limits.max_files:
        reasons.append(
            f"{len(paths)} files changed, over the limit of {limits.max_files}"
        )
    if lines_changed > limits.max_lines_changed:
        reasons.append(
            f"{lines_changed} lines changed, over the limit of "
            f"{limits.max_lines_changed}"
        )

    return ScopeVerdict(
        allowed=not reasons,
        reasons=reasons,
        protected=protected,
        secret_like=secret_like,
        infrastructure=infrastructure,
        security_config=security_config,
        unexpected=unexpected,
        review_required=review_required,
        files_changed=len(paths),
        lines_changed=lines_changed,
    )


#: Path fragments that suggest a file is a test. Used to answer "did the agent
#: add a regression test?" without needing the target project's test layout.
_TEST_MARKERS = (
    "test_",
    "_test.",
    ".test.",
    ".spec.",
    "/tests/",
    "/test/",
    "/__tests__/",
    "/spec/",
)


def looks_like_a_test(path: str) -> bool:
    """Whether *path* is plausibly a test file.

    Deliberately heuristic and generous. It informs the pull-request body and a
    note to the reviewer; it never gates a repair, because a wrong guess must
    not block a correct fix.
    """
    normalized = path.replace("\\", "/").lower()
    if normalized.startswith("test") or "/test" in f"/{normalized}":
        return True
    return any(marker in normalized for marker in _TEST_MARKERS)


def find_test_files(paths: Sequence[str]) -> List[str]:
    """Return the subset of *paths* that look like tests."""
    return [p for p in paths if looks_like_a_test(p)]
