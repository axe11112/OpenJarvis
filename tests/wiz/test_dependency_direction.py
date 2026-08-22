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

#: Reliability does not live only in ``reliability/``. Its HTTP surface and its
#: command line are separate files elsewhere, and an import of wiz from one of
#: those would take the reliability dashboard down with a wiz bug just as
#: surely — the package boundary is where the code is, not where the directory
#: is.
RELIABILITY_ELSEWHERE = (
    SRC / "server" / "reliability_routes.py",
    SRC / "server" / "reliability_dashboard.py",
    SRC / "cli" / "reliability_cmd.py",
)


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

    def test_reliabilitys_own_routes_and_cli_do_not_import_wiz_either(self):
        offenders: List[Tuple[str, str]] = []
        for path in RELIABILITY_ELSEWHERE:
            if not path.is_file():  # pragma: no cover - file was renamed
                continue
            for module in _imports(path):
                if module == "openjarvis.wiz" or module.startswith("openjarvis.wiz."):
                    offenders.append((str(path.relative_to(SRC)), module))
        assert offenders == [], (
            "reliability's HTTP and command-line surfaces must not depend on "
            "wiz either; found: "
            + ", ".join(f"{where} imports {what}" for where, what in offenders)
        )

    def test_the_wiz_routes_are_a_separate_file_from_reliabilitys(self):
        # Both dashboards are served by the same app, and both are worked on at
        # once. Separate files mean a change to one cannot break the other and
        # two sessions do not collide in the same diff.
        assert (SRC / "server" / "wiz_routes.py").is_file()
        assert (SRC / "server" / "reliability_routes.py").is_file()

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
