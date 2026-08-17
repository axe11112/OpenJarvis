"""Tests for isolated repair worktrees.

These drive **real git**, not a double. A worktree that works against a mock and
fails against git would be worse than no isolation at all, because the whole
point is to keep a coding agent away from a checkout a human is using.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from openjarvis.reliability.workspace import (
    RepairWorkspace,
    WorkspaceError,
    Worktree,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


def _run(args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A small git repository with one commit."""
    path = tmp_path / "repo"
    path.mkdir()
    _run(["git", "init", "-b", "main"], path)
    _run(["git", "config", "user.email", "t@example.com"], path)
    _run(["git", "config", "user.name", "Test"], path)
    (path / "app.py").write_text("VALUE = 1\n")
    _run(["git", "add", "-A"], path)
    _run(["git", "commit", "-m", "initial"], path)
    return path


@pytest.fixture
def manager(repo, tmp_path):
    return RepairWorkspace(repo_path=str(repo), root=str(tmp_path / "worktrees"))


class TestCreate:
    def test_creates_an_isolated_directory_on_its_own_branch(self, manager):
        wt = manager.create("INC-00001")
        assert Path(wt.path).is_dir()
        assert (Path(wt.path) / "app.py").read_text() == "VALUE = 1\n"
        assert wt.branch == "jarvis/incident-INC-00001"

    def test_records_the_base_commit_as_a_full_sha(self, manager, repo):
        wt = manager.create("INC-00001")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert wt.base_commit == head
        assert len(wt.base_commit) == 40

    def test_the_source_checkout_is_not_modified(self, manager, repo):
        wt = manager.create("INC-00001")
        (Path(wt.path) / "app.py").write_text("VALUE = 2\n")
        # The operator's checkout still holds the original content.
        assert (repo / "app.py").read_text() == "VALUE = 1\n"

    def test_two_incidents_get_separate_trees(self, manager):
        a = manager.create("INC-00001")
        b = manager.create("INC-00002")
        assert a.path != b.path
        (Path(a.path) / "app.py").write_text("A\n")
        assert (Path(b.path) / "app.py").read_text() == "VALUE = 1\n"

    def test_recreating_replaces_a_stale_tree(self, manager):
        first = manager.create("INC-00001")
        (Path(first.path) / "leftover.txt").write_text("junk\n")
        second = manager.create("INC-00001")
        assert not (Path(second.path) / "leftover.txt").exists()

    def test_requires_an_incident_id(self, manager):
        with pytest.raises(WorkspaceError):
            manager.create("")

    def test_rejects_a_non_repository(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        manager = RepairWorkspace(repo_path=str(plain), root=str(tmp_path / "wt"))
        with pytest.raises(WorkspaceError):
            manager.create("INC-00001")

    def test_unknown_ref_is_an_error(self, manager):
        with pytest.raises(WorkspaceError):
            manager.create("INC-00001", base_ref="no-such-ref")


class TestInspection:
    def test_reports_modified_files(self, manager):
        wt = manager.create("INC-00001")
        (Path(wt.path) / "app.py").write_text("VALUE = 2\n")
        assert manager.changed_files(wt) == ["app.py"]

    def test_reports_new_untracked_files(self, manager):
        """A fix delivered as a brand-new file is still a change to review."""
        wt = manager.create("INC-00001")
        (Path(wt.path) / "new_module.py").write_text("x = 1\n")
        assert "new_module.py" in manager.changed_files(wt)

    def test_reports_nested_new_files(self, manager):
        wt = manager.create("INC-00001")
        nested = Path(wt.path) / "src" / "deep"
        nested.mkdir(parents=True)
        (nested / "thing.py").write_text("y = 2\n")
        assert "src/deep/thing.py" in manager.changed_files(wt)

    def test_no_changes_is_empty(self, manager):
        wt = manager.create("INC-00001")
        assert manager.changed_files(wt) == []
        assert manager.has_changes(wt) is False

    def test_line_counts(self, manager):
        wt = manager.create("INC-00001")
        (Path(wt.path) / "app.py").write_text("VALUE = 2\nEXTRA = 3\n")
        added, removed = manager.line_counts(wt)
        assert added == 2
        assert removed == 1

    def test_diff_is_truncated(self, manager):
        wt = manager.create("INC-00001")
        (Path(wt.path) / "app.py").write_text("x\n" * 5000)
        diff = manager.diff(wt, max_chars=200)
        assert len(diff) < 400
        assert "truncated" in diff


class TestCommitAndPush:
    def test_commit_produces_a_sha(self, manager):
        wt = manager.create("INC-00001")
        (Path(wt.path) / "app.py").write_text("VALUE = 2\n")
        sha = manager.commit_all(wt, "fix: the thing")
        assert len(sha) == 40

    def test_commit_lands_on_the_incident_branch_only(self, manager, repo):
        wt = manager.create("INC-00001")
        (Path(wt.path) / "app.py").write_text("VALUE = 2\n")
        manager.commit_all(wt, "fix")
        main_content = subprocess.run(
            ["git", "show", "main:app.py"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert main_content == "VALUE = 1\n"

    def test_commit_with_nothing_staged_is_an_error(self, manager):
        wt = manager.create("INC-00001")
        with pytest.raises(WorkspaceError):
            manager.commit_all(wt, "empty")

    def test_push_refuses_a_branch_without_the_incident_prefix(self, manager):
        """Structural: 'main' does not start with the prefix, so it cannot be pushed."""
        rogue = Worktree(
            incident_id="INC-00001",
            path="/tmp",
            branch="main",
            base_commit="0" * 40,
        )
        with pytest.raises(WorkspaceError, match="not an incident branch"):
            manager.push(rogue)


class TestTeardown:
    def test_remove_deletes_the_directory(self, manager):
        wt = manager.create("INC-00001")
        manager.remove(wt, succeeded=True)
        assert not Path(wt.path).exists()

    def test_failures_are_kept_for_inspection(self, manager):
        wt = manager.create("INC-00001")
        manager.remove(wt, succeeded=False)
        assert Path(wt.path).exists()

    def test_failures_are_removed_when_not_keeping(self, repo, tmp_path):
        manager = RepairWorkspace(
            repo_path=str(repo),
            root=str(tmp_path / "wt"),
            keep_on_failure=False,
        )
        wt = manager.create("INC-00001")
        manager.remove(wt, succeeded=False)
        assert not Path(wt.path).exists()

    def test_removal_also_drops_the_branch(self, manager, repo):
        wt = manager.create("INC-00001")
        manager.remove(wt, succeeded=True)
        branches = subprocess.run(
            ["git", "branch", "--list", wt.branch],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert branches.strip() == ""

    def test_cleanup_all_is_safe_when_root_is_absent(self, repo, tmp_path):
        manager = RepairWorkspace(repo_path=str(repo), root=str(tmp_path / "nope"))
        manager.cleanup_all()  # must not raise

    def test_cleanup_all_removes_every_tree(self, manager):
        manager.create("INC-00001")
        manager.create("INC-00002")
        manager.cleanup_all()
        assert manager.changed_files is not None  # manager still usable


class TestWorktreeRecord:
    def test_round_trips(self):
        wt = Worktree(
            incident_id="INC-1",
            path="/tmp/x",
            branch="jarvis/incident-INC-1",
            base_commit="a" * 40,
            base_ref="main",
        )
        payload = wt.to_dict()
        assert payload["base_commit"] == "a" * 40
        assert payload["branch"] == "jarvis/incident-INC-1"

    def test_summary_is_readable(self):
        wt = Worktree(
            incident_id="INC-1",
            path="/tmp/x",
            branch="b",
            base_commit="abcdef1234567890",
            base_ref="main",
        )
        assert "abcdef123456" in wt.summary
        assert "main" in wt.summary


# ---------------------------------------------------------------------------
# Commit author identity
# ---------------------------------------------------------------------------


class TestRepairCommitIdentity:
    """Who a repair commit is authored by is load-bearing, not cosmetic.

    Hosting providers decide whether to build a pushed branch by mapping the
    commit author to an authorized account. A synthetic author gets the preview
    deployment silently refused — which presents as "the repair did not work"
    rather than "nobody was allowed to build it", and sends the operator
    debugging the fix instead of the identity.
    """

    IDENTITY = ("Axel Svahn", "axelsvahn10@gmail.com")

    def _workspace(self, repo, tmp_path, identity):
        return RepairWorkspace(
            repo_path=str(repo),
            root=str(tmp_path / "worktrees"),
            git_identity=identity,
        )

    def _commit(self, ws, repo, incident="INC-1"):
        worktree = ws.create(incident, base_ref="main")
        Path(worktree.path, "app.py").write_text("VALUE = 2\n")
        return worktree, ws.commit_all(worktree, "fix: value")

    def test_configured_identity_is_applied_to_a_new_worktree(self, repo, tmp_path):
        """Set at creation, before the coding agent — which has a shell and can
        commit on its own — ever runs."""
        ws = self._workspace(repo, tmp_path, self.IDENTITY)
        worktree = ws.create("INC-1", base_ref="main")
        assert ws.committer_identity(worktree.path) == self.IDENTITY

    def test_repair_commit_uses_the_configured_identity(self, repo, tmp_path):
        ws = self._workspace(repo, tmp_path, self.IDENTITY)
        worktree, sha = self._commit(ws, repo)
        author = subprocess.run(
            ["git", "log", "-1", "--format=%an <%ae>"],
            cwd=worktree.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert author == "Axel Svahn <axelsvahn10@gmail.com>"

    def test_configured_identity_beats_the_repository_default(self, repo, tmp_path):
        """The repo fixture sets Test <t@example.com>; the configured identity
        must win, or a repair would be authored as whoever last used the
        checkout."""
        ws = self._workspace(repo, tmp_path, self.IDENTITY)
        worktree, _ = self._commit(ws, repo)
        author = subprocess.run(
            ["git", "log", "-1", "--format=%ae"],
            cwd=worktree.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert author == "axelsvahn10@gmail.com"

    def test_no_global_git_config_is_written(self, repo, tmp_path, monkeypatch):
        """JARVIS repairing one target must not change how the operator's
        commits everywhere else are authored."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
        ws = self._workspace(repo, tmp_path, self.IDENTITY)
        self._commit(ws, repo)
        assert not (fake_home / ".gitconfig").exists()
        assert not (fake_home / ".config" / "git" / "config").exists()

    def test_unconfigured_inherits_the_repository_identity(self, repo, tmp_path):
        """Default behaviour stays safe: no identity configured means git
        resolves it normally, rather than JARVIS imposing a synthetic one."""
        ws = self._workspace(repo, tmp_path, None)
        worktree, _ = self._commit(ws, repo)
        author = subprocess.run(
            ["git", "log", "-1", "--format=%an <%ae>"],
            cwd=worktree.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert author == "Test <t@example.com>"

    def test_fallback_only_when_git_has_no_identity_at_all(self, tmp_path, monkeypatch):
        """A missing setting must not lose a repair, so a last-resort identity
        still applies — but only when git can resolve none."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        repo = tmp_path / "bare-identity-repo"
        repo.mkdir()
        _run(["git", "init", "-b", "main"], repo)
        (repo / "app.py").write_text("VALUE = 1\n")
        _run(["git", "add", "-A"], repo)
        _run(
            [
                "git",
                "-c",
                "user.name=Seed",
                "-c",
                "user.email=s@example.com",
                "commit",
                "-m",
                "initial",
            ],
            repo,
        )
        ws = RepairWorkspace(
            repo_path=str(repo), root=str(tmp_path / "wt"), git_identity=None
        )
        worktree = ws.create("INC-2", base_ref="main")
        Path(worktree.path, "app.py").write_text("VALUE = 3\n")
        ws.commit_all(worktree, "fix")
        author = subprocess.run(
            ["git", "log", "-1", "--format=%ae"],
            cwd=worktree.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert author == "jarvis@localhost"

    def test_incomplete_identity_is_ignored_not_half_applied(self, repo, tmp_path):
        """A name with no email would produce a commit git refuses or a
        nonsense author; leave it to git and warn instead."""
        ws = self._workspace(repo, tmp_path, ("Axel Svahn", ""))
        worktree, _ = self._commit(ws, repo)
        author = subprocess.run(
            ["git", "log", "-1", "--format=%an <%ae>"],
            cwd=worktree.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert author == "Test <t@example.com>"
