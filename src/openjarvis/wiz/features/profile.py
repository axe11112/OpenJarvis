"""What it takes to build and check a particular repository.

The brief is explicit that Wize's commands must not be hardcoded globally, and
the reason generalises: the moment ``npm test`` is baked into Wiz, Wiz is a tool
for one repository. A profile is therefore configuration first and *discovery*
second, with hardcoding nowhere.

Discovery reads the repository rather than guessing: ``package.json`` already
lists the scripts a project actually has, and a project without a ``typecheck``
script should have no typecheck gate rather than a gate that fails because the
script does not exist. A missing gate is honest; an always-failing gate teaches
everyone to ignore gates.

Explicit configuration always wins over discovery. An operator who writes
``test_command = "npm run test:ci"`` means it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = ["EngineeringProfile", "discover_profile", "load_profiles"]

#: Script names looked for in ``package.json``, in the order they should run.
#: Cheapest first, so a type error surfaces before a three-minute build.
_NODE_SCRIPTS = {
    "lint_command": ("lint",),
    "typecheck_command": ("typecheck", "type-check", "tsc"),
    "test_command": ("test", "test:ci", "test:unit"),
    "build_command": ("build",),
}


@dataclass(frozen=True, slots=True)
class EngineeringProfile:
    """How to build, check and ship one repository."""

    name: str
    repository: str = ""

    #: Absolute path to the checkout Wiz branches from.
    checkout: str = ""

    #: The branch features are cut from and merged back into.
    base_branch: str = "main"

    lint_command: str = ""
    typecheck_command: str = ""
    test_command: str = ""
    build_command: str = ""

    #: Runtime pinning, when the project needs it. Recorded rather than
    #: enforced here; the check runner puts it in the environment.
    node_version: str = ""

    #: Paths Wiz may never modify in this repository, on top of the global
    #: protected paths. Per-repository because what is sacred differs.
    protected_paths: List[str] = field(default_factory=list)

    #: Where a preview deployment can be found, when the project has one.
    preview_provider: str = ""

    def check_commands(self) -> Dict[str, str]:
        """The gate commands, in the shape ``CheckSuite.from_config`` wants."""
        return {
            "lint_command": self.lint_command,
            "typecheck_command": self.typecheck_command,
            "test_command": self.test_command,
            "build_command": self.build_command,
        }

    @property
    def configured_gates(self) -> List[str]:
        """Which gates this profile actually has.

        Reported to the operator so that "tests passed" is never ambiguous
        about which tests, or about whether there were any.
        """
        return [
            name.replace("_command", "")
            for name, command in self.check_commands().items()
            if command.strip()
        ]

    @property
    def complete(self) -> bool:
        """Whether this profile can gate a change at all.

        A profile with no test command cannot prove anything about a change,
        and a feature pipeline that runs on it would be theatre.
        """
        return bool(self.test_command.strip() and self.checkout.strip())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "repository": self.repository,
            "checkout": self.checkout,
            "base_branch": self.base_branch,
            "node_version": self.node_version,
            "protected_paths": list(self.protected_paths),
            "preview_provider": self.preview_provider,
            "gates": self.configured_gates,
            **self.check_commands(),
        }

    @classmethod
    def from_mapping(cls, name: str, raw: Mapping[str, Any]) -> "EngineeringProfile":
        return cls(
            name=name,
            repository=str(raw.get("repository", "")),
            checkout=str(raw.get("checkout", "")),
            base_branch=str(raw.get("base_branch", "main")),
            lint_command=str(raw.get("lint_command", "")),
            typecheck_command=str(raw.get("typecheck_command", "")),
            test_command=str(raw.get("test_command", "")),
            build_command=str(raw.get("build_command", "")),
            node_version=str(raw.get("node_version", "")),
            protected_paths=list(raw.get("protected_paths") or []),
            preview_provider=str(raw.get("preview_provider", "")),
        )

    def merged_with_discovery(
        self, checkout: Optional[str] = None
    ) -> "EngineeringProfile":
        """This profile, with any unset command filled in from the repository."""
        root = checkout or self.checkout
        if not root:
            return self
        discovered = discover_profile(self.name, root)
        if discovered is None:
            return self
        return EngineeringProfile(
            name=self.name,
            repository=self.repository or discovered.repository,
            checkout=root,
            base_branch=self.base_branch,
            lint_command=self.lint_command or discovered.lint_command,
            typecheck_command=self.typecheck_command or discovered.typecheck_command,
            test_command=self.test_command or discovered.test_command,
            build_command=self.build_command or discovered.build_command,
            node_version=self.node_version or discovered.node_version,
            protected_paths=list(self.protected_paths),
            preview_provider=self.preview_provider or discovered.preview_provider,
        )


def discover_profile(name: str, checkout: str | Path) -> Optional[EngineeringProfile]:
    """Read a repository and work out how it is built.

    Returns ``None`` when the checkout does not exist or is not a shape this
    understands. Returning ``None`` rather than an empty profile keeps "I could
    not tell" distinguishable from "there are no gates".
    """
    root = Path(checkout)
    package_json = root / "package.json"
    if not package_json.is_file():
        return None

    try:
        package = json.loads(package_json.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("could not read %s: %s", package_json, exc)
        return None

    scripts = package.get("scripts") or {}
    if not isinstance(scripts, dict):
        scripts = {}

    commands: Dict[str, str] = {}
    for field_name, candidates in _NODE_SCRIPTS.items():
        for candidate in candidates:
            if candidate in scripts:
                commands[field_name] = f"npm run {candidate}"
                break
        else:
            commands[field_name] = ""

    # ``npm test`` is the one script npm special-cases, and the one projects
    # most often define. Prefer the idiomatic invocation when it is the plain
    # "test" script.
    if commands.get("test_command") == "npm run test":
        commands["test_command"] = "npm test"

    node_version = ""
    engines = package.get("engines")
    if isinstance(engines, dict):
        node_version = str(engines.get("node", "")).strip()
    if not node_version:
        nvmrc = root / ".nvmrc"
        if nvmrc.is_file():
            try:
                node_version = nvmrc.read_text().strip()
            except OSError:
                node_version = ""

    preview_provider = "vercel" if (root / "vercel.json").is_file() else ""

    return EngineeringProfile(
        name=name,
        checkout=str(root),
        node_version=node_version,
        preview_provider=preview_provider,
        **commands,
    )


def load_profiles(raw: Mapping[str, Any]) -> Dict[str, EngineeringProfile]:
    """Build every profile from a ``{name: {...}}`` mapping."""
    profiles: Dict[str, EngineeringProfile] = {}
    for name, values in (raw or {}).items():
        if not isinstance(values, Mapping):
            logger.warning("engineering target '%s' is not a table; ignored", name)
            continue
        profiles[str(name)] = EngineeringProfile.from_mapping(str(name), values)
    return profiles
