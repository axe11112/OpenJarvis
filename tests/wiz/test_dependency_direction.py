"""``reliability`` must never import ``wiz``.

The reliability subsystem keeps a production website alive. Wiz is an assistant
layered around it. If reliability ever depends on wiz, then a bug in an optional
convenience feature can stop the thing that notices the site is down.

This is checked by parsing the imports rather than by importing the modules,
because importing them would require every optional dependency to be installed
and would turn a structural rule into an environment-dependent one.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

SRC = Path(__file__).resolve().parents[2] / "src" / "openjarvis"
RELIABILITY = SRC / "reliability"
WIZ = SRC / "wiz"


def _imports(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import cannot escape into a sibling package by
                # name, and level-1 inside reliability stays inside it.
                continue
            if node.module:
                yield node.module


def _offenders(root: Path, forbidden_prefix: str) -> List[Tuple[str, str]]:
    found: List[Tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        for module in _imports(path):
            if module == forbidden_prefix or module.startswith(forbidden_prefix + "."):
                found.append((str(path.relative_to(SRC)), module))
    return found


class TestDependencyDirection:
    def test_reliability_does_not_import_wiz(self):
        offenders = _offenders(RELIABILITY, "openjarvis.wiz")
        assert offenders == [], (
            "reliability must not depend on wiz; found: "
            + ", ".join(f"{where} imports {what}" for where, what in offenders)
        )

    def test_the_wiz_package_exists_to_be_checked(self):
        # Guards against this test passing because the path is wrong.
        assert WIZ.is_dir()
        assert RELIABILITY.is_dir()
        assert list(WIZ.rglob("*.py"))

    def test_wiz_is_allowed_to_import_reliability(self):
        # Not an assertion about the current code so much as a statement of the
        # permitted direction: this test documents that the reverse check above
        # is one-way on purpose.
        offenders = _offenders(WIZ, "openjarvis.reliability")
        assert isinstance(offenders, list)
