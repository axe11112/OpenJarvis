"""Wiz's own settings file, deliberately not part of the global config.

Two reasons for a separate file rather than another section of
``~/.openjarvis/config.toml``.

The first is ownership. The reliability subsystem's configuration is edited by
the reliability subsystem's maintainers, and a feature-target change should not
be able to touch it. Separate files fail separately.

The second is the one that matters for safety. Wiz's authority lives in
``authority.json`` in the same directory, and the protected-path rules forbid
Wiz from editing anything in it. Keeping the engineering targets and the
shipping policy in that same protected directory means changing what Wiz may
build, and where, is an act a person performs — not something a coding session
could arrange as a side effect of a feature.

Everything defaults to off or absent. A missing file produces a Wiz that can
answer questions and build nothing, which is the correct state for a machine
nobody has configured yet.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from openjarvis.wiz.features.profile import EngineeringProfile, load_profiles
from openjarvis.wiz.features.shipping import FeatureShippingPolicy

logger = logging.getLogger(__name__)

__all__ = ["WizSettings", "SETTINGS_FILENAME", "load_settings"]

SETTINGS_FILENAME = "wiz.json"

#: Checkouts a coding agent must never be handed, on top of whatever the
#: operator lists. These are the running system: the live application and
#: OpenJarvis's own tree. Named here rather than only in configuration so that
#: an empty settings file still protects them.
ALWAYS_PROTECTED = ("~/Wize", "~/wize", "~/OpenJarvis", "~/openjarvis")


@dataclass(frozen=True)
class WizSettings:
    """What Wiz may build, where, and what happens when it is done."""

    #: Engineering targets by name. Empty means Wiz builds nothing.
    targets: Dict[str, EngineeringProfile] = field(default_factory=dict)

    #: The target used when a request does not name one.
    default_target: str = ""

    #: Where feature worktrees are created. Must be outside every protected
    #: checkout; :class:`FeatureWorkspace` refuses otherwise.
    worktree_root: str = "~/.openjarvis/wiz/worktrees"

    #: Directories a coding agent may never be given, beyond the built-in list.
    protected_checkouts: List[str] = field(default_factory=list)

    #: ``(name, email)`` feature commits are authored as. Hosting providers
    #: decide whether to build a branch based on whether the author maps to an
    #: authorised account, so a synthetic identity reads as "the feature did
    #: not work" rather than "nobody was allowed to build it".
    git_author_name: str = ""
    git_author_email: str = ""

    shipping: FeatureShippingPolicy = field(default_factory=FeatureShippingPolicy)

    #: How many times Claude may try before a person is asked.
    max_attempts: int = 3

    #: Where screenshots and traces from feature verification are written.
    evidence_root: str = "~/.openjarvis/wiz/evidence"

    #: Whether a second Claude session reviews meaningful changes.
    independent_review: bool = True

    @property
    def configured(self) -> bool:
        """Whether Wiz has anything it could build."""
        return bool(self.profile())

    def profile(self, name: str = "") -> Optional[EngineeringProfile]:
        """The named target, the default, or nothing."""
        wanted = name or self.default_target
        if wanted:
            return self.targets.get(wanted)
        if len(self.targets) == 1:
            return next(iter(self.targets.values()))
        return None

    def all_protected(self) -> List[str]:
        """Every directory a coding agent must never be handed.

        The built-in list is always included. An operator who removes their
        live checkout from configuration has not thereby made it editable.
        """
        return list(dict.fromkeys([*ALWAYS_PROTECTED, *self.protected_checkouts]))

    def git_identity(self) -> Optional[tuple]:
        if self.git_author_name and self.git_author_email:
            return (self.git_author_name, self.git_author_email)
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "targets": {name: p.to_dict() for name, p in self.targets.items()},
            "default_target": self.default_target,
            "worktree_root": self.worktree_root,
            "protected_checkouts": self.all_protected(),
            "git_author_name": self.git_author_name,
            "git_author_email": self.git_author_email,
            "shipping": self.shipping.to_dict(),
            "max_attempts": self.max_attempts,
            "evidence_root": self.evidence_root,
            "independent_review": self.independent_review,
            "configured": self.configured,
        }

    @classmethod
    def from_mapping(cls, raw: Optional[Mapping[str, Any]]) -> "WizSettings":
        if not raw:
            return cls()
        targets = load_profiles(raw.get("targets") or {})
        # Discovery fills in what configuration left out — a repository already
        # lists the scripts it has, and a guessed command that does not exist is
        # a gate that always fails, which teaches everyone to ignore gates.
        targets = {
            name: profile.merged_with_discovery() for name, profile in targets.items()
        }
        return cls(
            targets=targets,
            default_target=str(raw.get("default_target", "")),
            worktree_root=str(raw.get("worktree_root", "~/.openjarvis/wiz/worktrees")),
            protected_checkouts=[
                str(p) for p in (raw.get("protected_checkouts") or ())
            ],
            git_author_name=str(raw.get("git_author_name", "")),
            git_author_email=str(raw.get("git_author_email", "")),
            shipping=FeatureShippingPolicy.from_mapping(raw.get("shipping")),
            max_attempts=max(1, int(raw.get("max_attempts", 3) or 3)),
            evidence_root=str(raw.get("evidence_root", "~/.openjarvis/wiz/evidence")),
            independent_review=bool(raw.get("independent_review", True)),
        )


def load_settings(path: str | Path) -> WizSettings:
    """Read the settings file, defaulting to "build nothing".

    A malformed file is a warning and defaults, not a crash. Wiz refusing to
    start because a comma is missing means an operator loses the reliability
    subsystem too, and that one is watching production.
    """
    file = Path(path).expanduser()
    if not file.is_file():
        return WizSettings()
    try:
        raw = json.loads(file.read_text())
    except (OSError, ValueError) as exc:
        logger.error(
            "could not read %s (%s); Wiz will not build anything until it is fixed",
            file,
            exc,
        )
        return WizSettings()
    if not isinstance(raw, dict):
        logger.error("%s is not an object; ignoring it", file)
        return WizSettings()
    return WizSettings.from_mapping(raw)
