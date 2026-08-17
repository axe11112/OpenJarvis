"""Safety policy — what JARVIS may do without a human.

Every gate here defaults closed.  The policy is data (config), the enforcement
is code, and every denial carries a reason that ends up in the audit log and in
the owner's notification.

The asymmetry is deliberate: refusing a safe action costs a human five minutes;
permitting an unsafe one can cost a production outage.  When a rule is unclear,
it refuses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from openjarvis.reliability.types import Incident, Severity, VerificationResult

logger = logging.getLogger(__name__)

__all__ = ["Decision", "SafetyPolicy"]

#: Deploy modes, least to most permissive.
DEPLOY_NEVER = "never"
DEPLOY_PR_ONLY = "pr_only"
DEPLOY_AUTO_ALLOWLISTED = "auto_deploy_allowlisted"


@dataclass(slots=True)
class Decision:
    """A policy verdict with its reason."""

    allowed: bool
    reason: str = ""
    rule: str = ""
    #: Whether refusing this actually requires a person.
    #:
    #: "JARVIS may not repair this" and "a human must deal with this" are
    #: different statements, and conflating them is what made Sir escalate a
    #: slow login page to a phone call within one second. A severity outside the
    #: auto-repair list means *do not write code for it* — the incident is still
    #: watched, may still recover on its own, and nobody needs waking. Only
    #: refusals that leave a real problem nobody is working on set this.
    needs_human: bool = True

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class SafetyPolicy:
    """Decides whether an autonomous action is permitted.

    Parameters
    ----------
    deploy_mode:
        ``"never"``, ``"pr_only"`` (default) or ``"auto_deploy_allowlisted"``.
    auto_repair_severities:
        Severities JARVIS may attempt to repair without being asked.
    auto_deploy_fix_classes:
        Fix classes eligible for automatic deployment.  Empty by default, which
        means nothing deploys itself regardless of ``deploy_mode``.
    protected_paths:
        Globs a repair may not touch.
    max_attempts:
        Repair attempts before escalating to a human.
    """

    deploy_mode: str = DEPLOY_PR_ONLY
    auto_repair_severities: List[str] = field(
        default_factory=lambda: ["HIGH", "MEDIUM"]
    )
    auto_deploy_fix_classes: List[str] = field(default_factory=list)
    protected_paths: List[str] = field(default_factory=list)
    allow_push_to_default_branch: bool = False
    max_attempts: int = 3
    repair_enabled: bool = False

    # -- repair -----------------------------------------------------------

    def may_attempt_repair(self, incident: Incident) -> Decision:
        """Whether JARVIS may modify code for *incident*."""
        if not self.repair_enabled:
            return Decision(
                False,
                "automated repair is disabled ([reliability.repair] enabled = false)",
                "repair_disabled",
                # An operator switch, not a fault. Escalating because repair is
                # off would page somebody for a setting they chose.
                needs_human=False,
            )
        if incident.attempts_used >= self.max_attempts:
            return Decision(
                False,
                f"already made {incident.attempts_used} of {self.max_attempts} "
                "permitted attempts",
                "attempts_exhausted",
            )
        allowed = {s.upper() for s in self.auto_repair_severities}
        if incident.severity.value not in allowed:
            return Decision(
                False,
                f"severity {incident.severity.value} is not in the auto-repair "
                f"list ({', '.join(sorted(allowed)) or 'none'})",
                "severity_not_permitted",
                # Not a job for a person. It is a decision the operator already
                # made about which severities JARVIS may write code for, and a
                # LOW-severity slow page needs neither a repair nor a human.
                needs_human=False,
            )
        if incident.state.value == "HUMAN_REQUIRED":
            return Decision(
                False,
                "the incident is already awaiting a human",
                "human_required",
                # Already escalated once. Saying so again is the duplicate the
                # owner complained about.
                needs_human=False,
            )
        return Decision(True)

    def may_modify_paths(self, paths: Sequence[str]) -> Decision:
        """Whether a diff touching *paths* is permitted."""
        from openjarvis.reliability.sources.github import is_protected_path

        blocked = [p for p in paths if is_protected_path(p, self.protected_paths)]
        if blocked:
            return Decision(
                False,
                "the change touches protected path(s): " + ", ".join(sorted(blocked)),
                "protected_path",
            )
        return Decision(True)

    # -- deployment -------------------------------------------------------

    def may_deploy(
        self,
        incident: Incident,
        verification: Optional[VerificationResult],
        *,
        fix_class: str = "",
        changed_paths: Sequence[str] = (),
    ) -> Decision:
        """Whether a verified repair may be deployed rather than PR'd.

        Every precondition must hold.  The checks are ordered cheapest-first so
        the reason a human sees is the most fundamental one.
        """
        if self.deploy_mode == DEPLOY_NEVER:
            return Decision(False, "deploy_mode is 'never'", "deploy_disabled")
        if self.deploy_mode == DEPLOY_PR_ONLY:
            return Decision(
                False,
                "deploy_mode is 'pr_only'; opening a pull request instead",
                "pr_only",
            )
        if verification is None or not verification.passed:
            return Decision(
                False,
                "the repair has not passed independent verification",
                "unverified",
            )
        if incident.severity is Severity.CRITICAL:
            # The blast radius of being wrong is exactly the case where a human
            # should look. No allowlist overrides this.
            return Decision(
                False,
                "CRITICAL incidents are never deployed automatically",
                "critical_never_auto",
            )
        if not self.auto_deploy_fix_classes:
            return Decision(
                False,
                "no fix classes are allowlisted for automatic deployment",
                "no_allowlist",
            )
        if fix_class not in self.auto_deploy_fix_classes:
            return Decision(
                False,
                f"fix class '{fix_class or 'unclassified'}' is not allowlisted "
                f"({', '.join(self.auto_deploy_fix_classes)})",
                "class_not_allowlisted",
            )
        paths_ok = self.may_modify_paths(changed_paths)
        if not paths_ok:
            return paths_ok
        sensitive = self._sensitive_paths(changed_paths)
        if sensitive:
            return Decision(
                False,
                "the diff touches security-sensitive path(s): "
                + ", ".join(sorted(sensitive)),
                "security_sensitive",
            )
        return Decision(True)

    def may_push_to(self, branch: str, base_branch: str) -> Decision:
        """Whether JARVIS may push to *branch*."""
        if branch == base_branch and not self.allow_push_to_default_branch:
            return Decision(
                False,
                f"pushing to the default branch '{branch}' is not permitted",
                "default_branch",
            )
        return Decision(True)

    # -- helpers ----------------------------------------------------------

    #: Paths where an automated change is never routine, even when verified.
    SECURITY_SENSITIVE = (
        "**/auth/**",
        "**/*auth*",
        "**/middleware.*",
        "**/*rls*",
        "**/*polic*",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "requirements*.txt",
        "pyproject.toml",
    )

    @classmethod
    def _sensitive_paths(cls, paths: Sequence[str]) -> List[str]:
        """Return the subset of *paths* that are security-sensitive.

        Uses the same matcher as the protected-path guard so the two cannot
        drift apart.
        """
        from openjarvis.reliability.sources.github import matches_path_pattern

        return [
            path
            for path in paths
            if any(
                matches_path_pattern(path, pattern)
                for pattern in cls.SECURITY_SENSITIVE
            )
        ]
