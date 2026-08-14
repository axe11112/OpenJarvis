"""A small git repository with a controlled bug, for end-to-end repair tests.

The bug is chosen so the three interesting cases are distinguishable:

* the project's **own test suite passes at the base commit** — the bug is real
  but uncovered, which is what a production incident normally looks like;
* a **wrong fix can still pass that suite**, so a repair that only runs local
  tests would wrongly conclude it had succeeded;
* the **reproduction distinguishes them**, so independent verification is the
  only thing that can tell a real fix from a plausible one.

That third property is the whole argument for JARVIS's architecture, and this
fixture exists to prove it holds rather than to assert it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

#: The bug: a percentage discount is subtracted as if it were an amount.
#: ``apply_discount(200, 10)`` returns 190 where it should return 180.
BUGGY_SOURCE = '''\
"""Pricing helpers."""


def apply_discount(price, percent):
    """Return *price* reduced by *percent* percent."""
    return price - percent
'''

#: The correct implementation.
FIXED_SOURCE = '''\
"""Pricing helpers."""


def apply_discount(price, percent):
    """Return *price* reduced by *percent* percent."""
    return price - (price * percent / 100)
'''

#: A change that passes the project's existing tests but does not fix the bug.
#: ``apply_discount(100, 0)`` is still 100, so the shipped suite stays green;
#: ``apply_discount(200, 10)`` is 185, so the reproduction still fails.
PLAUSIBLE_BUT_WRONG_SOURCE = '''\
"""Pricing helpers."""


def apply_discount(price, percent):
    """Return *price* reduced by *percent* percent."""
    return price - (percent * 1.5)
'''

#: The project's own suite, as it exists at the base commit. It passes with the
#: bug present, because the bug is not covered.
EXISTING_TESTS = """\
from app import apply_discount


def test_zero_discount_changes_nothing():
    assert apply_discount(100, 0) == 100
"""

#: A regression test a good repair should add.
REGRESSION_TEST = """\
from app import apply_discount


def test_percentage_discount_is_a_percentage():
    assert apply_discount(200, 10) == 180
"""

#: Run inside a worktree by the verifier. Exit 0 means the original failure no
#: longer reproduces. This stands in for a Playwright probe against a preview
#: deployment: it executes the repaired code and checks observed behaviour,
#: rather than believing anyone's account of it.
REPRODUCTION = (
    "import sys; sys.path.insert(0, '.'); "
    "from app import apply_discount; "
    "result = apply_discount(200, 10); "
    "print('observed', result); "
    "sys.exit(0 if result == 180 else 1)"
)


def _run(args: List[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def build_broken_repo(path: Path) -> Path:
    """Create a git repository containing the controlled bug. Returns *path*."""
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-b", "main"], path)
    _run(["git", "config", "user.email", "fixture@example.com"], path)
    _run(["git", "config", "user.name", "Fixture"], path)
    _run(["git", "config", "commit.gpgsign", "false"], path)

    (path / "app.py").write_text(BUGGY_SOURCE)
    tests = path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text(EXISTING_TESTS)
    workflows = path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: ci\non: [push]\n")

    _run(["git", "add", "-A"], path)
    _run(["git", "commit", "-m", "initial"], path)
    return path


def reproduction_passes(workspace: str) -> bool:
    """Whether the original failure has stopped reproducing in *workspace*."""
    proc = subprocess.run(
        ["python3", "-c", REPRODUCTION],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Scripted agent behaviours
#
# Each writes real files into the real worktree it is handed, so the loop reads
# a real git diff rather than a declared list of changed files.
# ---------------------------------------------------------------------------


def agent_correct_fix(workspace: str, _attempt: int) -> None:
    """Fix the bug properly and add a regression test."""
    Path(workspace, "app.py").write_text(FIXED_SOURCE)
    Path(workspace, "tests", "test_discount_regression.py").write_text(REGRESSION_TEST)


def agent_plausible_but_wrong(workspace: str, _attempt: int) -> None:
    """Change the code, pass the shipped tests, and not fix the bug."""
    Path(workspace, "app.py").write_text(PLAUSIBLE_BUT_WRONG_SOURCE)


def agent_breaks_the_tests(workspace: str, _attempt: int) -> None:
    """Make a change that the project's own suite rejects."""
    Path(workspace, "app.py").write_text(
        'def apply_discount(price, percent):\n    return "not a number"\n'
    )


def agent_edits_ci(workspace: str, _attempt: int) -> None:
    """Attempt to modify the CI configuration — never permitted."""
    Path(workspace, ".github", "workflows", "ci.yml").write_text(
        "name: ci\non: [push]\njobs: {}\n"
    )
    Path(workspace, "app.py").write_text(FIXED_SOURCE)


def agent_writes_a_secret(workspace: str, _attempt: int) -> None:
    """Fix the bug but also drop a credential file into the tree."""
    Path(workspace, "app.py").write_text(FIXED_SOURCE)
    Path(workspace, ".env").write_text("API_TOKEN=abc123\n")


def agent_runs_away(workspace: str, _attempt: int) -> None:
    """Fix the bug, then rewrite half the repository."""
    Path(workspace, "app.py").write_text(FIXED_SOURCE)
    for index in range(40):
        Path(workspace, f"generated_{index}.py").write_text(f"VALUE = {index}\n")


def agent_does_nothing(workspace: str, _attempt: int) -> None:
    """Claim a fix without changing anything."""
    return None


def agent_wrong_then_right(workspace: str, attempt: int) -> None:
    """Fail the first attempt, succeed on the second.

    Proves the retry loop actually re-drives the agent with feedback rather than
    repeating an identical attempt.
    """
    if attempt == 0:
        agent_plausible_but_wrong(workspace, attempt)
    else:
        agent_correct_fix(workspace, attempt)
