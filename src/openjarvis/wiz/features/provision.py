"""Deterministic dependency provisioning: what a coding session cannot do
for itself, done by trusted pipeline code instead.

Found on FEAT-00031: BUILDING has no ``Bash`` (see
:mod:`openjarvis.wiz.features.engineer` — a coding session's own shell is
what let FEAT-00030 merge its own pull request). Nothing was wrong with
that fix. What it missed is that dependency installation — ``npm ci`` in a
fresh worktree — was never a deterministic, pipeline-owned step; it was
something a Bash-enabled session did for itself, implicitly, before running
its own checks. Removing Bash removed the only thing that ever ran it, and
three attempts in a row failed the exact same way: ``tsc``, ``eslint`` and
everything else under ``node_modules/.bin`` simply did not exist.

This module is the replacement: a command derived only from the
repository's own metadata (``package.json``'s ``packageManager`` field, or
which lockfile is actually present — never guessed, never taken from
anything a coding session wrote), run by :func:`~openjarvis.reliability.
checks.run_check`'s own bounded subprocess machinery, before the check
suite. The install command is always the lockfile-preserving, frozen form
(``npm ci``, not ``npm install``) — nothing here is ever allowed to rewrite
a lockfile a person did not write.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from openjarvis.reliability.checks import CheckCommand, CheckResult, run_check
from openjarvis.wiz.features.diskspace import DEFAULT_MIN_FREE_BYTES, has_enough_disk

logger = logging.getLogger(__name__)

__all__ = [
    "PROVISION_CHECK_NAME",
    "PackageManagerSpec",
    "detect_package_manager",
    "needs_provisioning",
    "provision_check",
]

PROVISION_CHECK_NAME = "provision"


@dataclass(frozen=True, slots=True)
class PackageManagerSpec:
    """One package manager: its lockfile, and its frozen install command."""

    name: str
    lockfile: str
    #: Always the lockfile-preserving form. Never "install" bare, which can
    #: rewrite the lockfile — a provisioning step must never do that.
    install_command: str


#: Checked in this order. A project's own declared ``packageManager`` field
#: (checked first, in detect_package_manager) is authoritative over all of
#: these; this list is the lockfile-presence fallback.
_KNOWN_MANAGERS: List[PackageManagerSpec] = [
    PackageManagerSpec("pnpm", "pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
    PackageManagerSpec("yarn", "yarn.lock", "yarn install --frozen-lockfile"),
    PackageManagerSpec("npm", "package-lock.json", "npm ci"),
]


def detect_package_manager(workspace: Path) -> Optional[PackageManagerSpec]:
    """Which package manager this repository actually uses.

    ``package.json``'s own ``packageManager`` field is authoritative when
    present (Corepack's own convention: ``"pnpm@9.1.0"`` etc.) — checked
    first, and if it names a manager this module does not recognise, this
    returns ``None`` rather than silently falling back to a lockfile guess
    that could be stale or wrong. Otherwise, whichever lockfile is actually
    present decides. Neither path ever guesses when the repository has been
    explicit; ``None`` means genuinely undetermined, and callers must treat
    that as "cannot provision safely," not as "assume npm."
    """
    package_json = workspace / "package.json"
    if not package_json.is_file():
        return None
    try:
        raw = json.loads(package_json.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("could not read %s: %s", package_json, exc)
        return None

    declared = str((raw or {}).get("packageManager", "")).strip()
    if declared:
        name = declared.split("@", 1)[0].strip().lower()
        for spec in _KNOWN_MANAGERS:
            if spec.name == name:
                return spec
        logger.warning(
            "package.json declares packageManager=%r, which this module "
            "does not recognise; refusing to guess",
            declared,
        )
        return None

    for spec in _KNOWN_MANAGERS:
        if (workspace / spec.lockfile).is_file():
            return spec
    return None


def needs_provisioning(workspace: Path, spec: PackageManagerSpec) -> bool:
    """Whether *workspace* needs a real (re)install before checks can run.

    Compared by modification time against the lockfile — the same signal
    build tools like Make use for "is this input newer than that output" —
    rather than a package manager's own internal bookkeeping file (npm's
    ``node_modules/.package-lock.json`` was tried and rejected: confirmed
    against a real successful install, it is a different size than the real
    lockfile, not a byte copy of it, so comparing them proves nothing).
    A ``node_modules`` at least as new as the lockfile it was built from is
    trusted; anything older, or missing a populated ``.bin`` directory
    outright, triggers a real (re)install. A feature's own retries do not
    normally touch the lockfile, so the common case is a cheap stat-based
    skip; the one case that matters — the lockfile genuinely changed — is
    never silently missed.
    """
    bin_dir = workspace / "node_modules" / ".bin"
    try:
        if not bin_dir.is_dir() or not any(bin_dir.iterdir()):
            return True
    except OSError:
        return True

    lockfile = workspace / spec.lockfile
    if not lockfile.is_file():
        return True

    try:
        node_modules_mtime = (workspace / "node_modules").stat().st_mtime
        lockfile_mtime = lockfile.stat().st_mtime
    except OSError:
        return True
    return node_modules_mtime < lockfile_mtime


def provision_check(
    workspace: str,
    *,
    path_prepend: Optional[List[str]] = None,
    timeout: int = 600,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> CheckResult:
    """Provision *workspace*'s dependencies, or report exactly why not.

    Pre-flight, in order — each one fails closed with a precise reason
    rather than letting a later, more confusing failure stand in for it:

    - a package manifest exists at all
    - the package manager is determinable from real repository metadata
      (never guessed)
    - its lockfile is present
    - enough free disk remains (the same guard
      :func:`~openjarvis.wiz.features.diskspace.has_enough_disk` already
      applies before every pipeline step — checked again here, locally,
      because an install is exactly the kind of step FEAT-00007 and
      FEAT-00008 crashed on)

    Then: skipped entirely when :func:`needs_provisioning` says the existing
    ``node_modules`` is already current for this exact lockfile — no
    reinstalling hundreds of megabytes on every retry of the same feature.
    Otherwise, the frozen install command runs through
    :func:`~openjarvis.reliability.checks.run_check` — the same bounded
    subprocess machinery every other gate already uses: a fixed timeout,
    output capped and returned for feedback, and a command that is always
    exactly one of the three frozen forms above — never a string built from
    anything a coding session produced.
    """
    root = Path(workspace)
    package_json = root / "package.json"
    if not package_json.is_file():
        return CheckResult(
            name=PROVISION_CHECK_NAME,
            ran=False,
            passed=False,
            summary="no package.json — nothing to provision",
            required=True,
        )

    spec = detect_package_manager(root)
    if spec is None:
        return CheckResult(
            name=PROVISION_CHECK_NAME,
            ran=False,
            passed=False,
            summary=(
                "could not determine the package manager from package.json "
                "(no recognised packageManager field and no known lockfile "
                "present) — refusing to guess an install command"
            ),
            required=True,
        )

    lockfile = root / spec.lockfile
    if not lockfile.is_file():
        return CheckResult(
            name=PROVISION_CHECK_NAME,
            ran=False,
            passed=False,
            summary=f"{spec.name} detected but {spec.lockfile} is missing",
            required=True,
        )

    if not has_enough_disk(str(root), min_free_bytes=min_free_bytes):
        return CheckResult(
            name=PROVISION_CHECK_NAME,
            ran=False,
            passed=False,
            summary="not enough free disk to provision dependencies safely",
            required=True,
        )

    if not needs_provisioning(root, spec):
        return CheckResult(
            name=PROVISION_CHECK_NAME,
            ran=True,
            passed=True,
            summary=f"node_modules already current for {spec.lockfile}; skipped {spec.install_command}",
            required=True,
        )

    command = CheckCommand(
        PROVISION_CHECK_NAME,
        spec.install_command,
        required=True,
        timeout=timeout,
        path_prepend=list(path_prepend or []),
    )
    result = run_check(command, workspace=workspace)
    if result.passed:
        result.summary = f"{spec.install_command}: {result.summary}"
    return result
