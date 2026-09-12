"""Tests for isolated repair worktrees.

These drive **real git**, not a double. A worktree that works against a mock and
fails against git would be worse than no isolation at all, because the whole
point is to keep a coding agent away from a checkout a human is using.
"""

from __future__ import annotations

import os
import shutil
import socket
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


def _a_pid_that_does_not_exist() -> int:
    """A pid with no live process, found rather than guessed."""
    for candidate in range(99000, 99999):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except OSError:
            continue
    raise AssertionError("could not find an unused pid")


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


def _lock_reason(repo, path) -> str:
    """The lock reason git records for *path*, or '' when it is not locked."""
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    current = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree ") :].strip()
        elif line.startswith("locked") and current == str(path):
            return line[len("locked") :].strip()
    return ""


def _relock(repo, path, reason: str) -> None:
    """Replace whatever lock is on *path* with one carrying *reason*."""
    subprocess.run(
        ["git", "worktree", "unlock", str(path)],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    _run(["git", "worktree", "lock", str(path), "--reason", reason], repo)


class TestWorktreeOwnershipIsCrossProcess:
    """Two processes must not fight over one incident's worktree.

    The worktree path is derived from the incident id and ownership was not
    established across processes at all. A second watcher, or a hand-run
    `jarvis reliability repair`, reaching the same incident went straight into
    create(), found the directory, and force-deleted the tree the first one was
    actively writing in -- a live Claude session's work gone, with nothing
    recorded anywhere.

    create() now claims the worktree with `git worktree lock`, stamping the
    owning pid and host into git's own lock reason so any process can read it.
    Real git throughout: this subsystem has destroyed real work twice before.
    """

    def test_create_claims_the_worktree(self, manager, repo):
        wt = manager.create("INC-00001")
        reason = _lock_reason(repo, wt.path)
        assert reason.startswith("openjarvis-repair"), (
            f"the worktree was not claimed; lock reason was {reason!r}"
        )
        assert f"pid={os.getpid()}" in reason
        assert "incident=INC-00001" in reason

    def test_its_own_worktree_is_still_removable(self, manager):
        """The claim must not stop the owner cleaning up after itself."""
        wt = manager.create("INC-00001")
        manager.remove(wt, succeeded=True)
        assert not Path(wt.path).exists()

    def test_another_live_process_cannot_destroy_it(self, manager, repo):
        """The defect, stated directly."""
        wt = manager.create("INC-00001")
        (Path(wt.path) / "work-in-progress.py").write_text("VALUE = 2\n")
        # A live repair in another process: a pid that exists but is not ours.
        _relock(
            repo,
            wt.path,
            f"openjarvis-repair pid=1 host={socket.gethostname()} incident=INC-00001",
        )

        manager.remove(wt, succeeded=True)

        assert Path(wt.path).exists(), (
            "another process's live repair worktree was deleted"
        )
        assert (Path(wt.path) / "work-in-progress.py").exists(), (
            "the other repair's uncommitted work was destroyed"
        )

    def test_a_lock_from_another_host_is_left_alone(self, manager, repo):
        """Liveness cannot be checked across hosts, so it is never assumed."""
        wt = manager.create("INC-00001")
        _relock(
            repo,
            wt.path,
            f"openjarvis-repair pid={os.getpid()} host=some-other-mac "
            "incident=INC-00001",
        )
        manager.remove(wt, succeeded=True)
        assert Path(wt.path).exists()

    def test_a_crashed_owner_does_not_leak_the_worktree_forever(
        self, manager, repo
    ):
        """Fail-closed must not mean fail-forever.

        A repair killed mid-run leaves its claim behind. Refusing to clean that
        up would leave the incident permanently unrepairable, so a lock naming
        a dead process on this host is broken -- and only that case is.
        """
        wt = manager.create("INC-00001")
        dead_pid = _a_pid_that_does_not_exist()
        _relock(
            repo,
            wt.path,
            f"openjarvis-repair pid={dead_pid} host={socket.gethostname()} "
            "incident=INC-00001",
        )

        manager.remove(wt, succeeded=True)

        assert not Path(wt.path).exists(), (
            "a crashed repair's worktree was never reclaimed, so this incident "
            "can never be repaired again"
        )

    def test_a_lock_this_module_did_not_write_is_respected(self, manager, repo):
        """A person who locked a worktree by hand meant it."""
        wt = manager.create("INC-00001")
        _relock(repo, wt.path, "do not touch, I am debugging this")
        manager.remove(wt, succeeded=True)
        assert Path(wt.path).exists()

    def test_create_refuses_rather_than_stealing_a_live_worktree(
        self, manager, repo, tmp_path
    ):
        """What a second process actually experiences: a loud failure.

        Not a silent reuse (which would mix two repairs) and not a deletion.
        """
        wt = manager.create("INC-00001")
        (Path(wt.path) / "work-in-progress.py").write_text("VALUE = 2\n")
        _relock(
            repo,
            wt.path,
            f"openjarvis-repair pid=1 host={socket.gethostname()} incident=INC-00001",
        )

        second = RepairWorkspace(repo_path=str(repo), root=str(tmp_path / "worktrees"))
        with pytest.raises(WorkspaceError):
            second.create("INC-00001")

        assert (Path(wt.path) / "work-in-progress.py").exists(), (
            "the second process destroyed the first's work on its way to failing"
        )


class TestDestructiveCleanupFailsClosed:
    """A worktree git is protecting must not be deleted anyway.

    _remove_path ran `worktree remove --force` with check=False -- discarding
    whether git had agreed -- and then deleted the directory with
    shutil.rmtree(ignore_errors=True) regardless. Every reason git can have for
    refusing was overridden by a recursive delete.
    """

    def test_a_stale_directory_git_never_registered_is_still_removed(
        self, manager, tmp_path
    ):
        """The case the rmtree was written for, and the reason it cannot go.

        A directory left by a killed process: git declines to remove it because
        it is not a working tree, and the next `worktree add` fails until it is
        gone. Identified from git's own answer now, rather than assumed for
        every refusal.
        """
        stale = tmp_path / "worktrees" / "stale-from-a-killed-process"
        stale.mkdir(parents=True)
        (stale / "leftover.txt").write_text("junk\n")

        manager._remove_path(str(stale))

        assert not stale.exists(), (
            "a stale directory git never tracked was left behind, which is what "
            "makes the next worktree add fail"
        )

    def test_an_ordinary_dirty_worktree_is_still_removed(self, manager):
        """--force is still --force: a dirty tree is not a refusal.

        The fix must not turn every uncommitted change into a permanent
        leftover -- that is what keep_on_failure is for, decided by the caller.
        """
        wt = manager.create("INC-00001")
        (Path(wt.path) / "app.py").write_text("VALUE = 999\n")
        manager.remove(wt, succeeded=True)
        assert not Path(wt.path).exists()

    def test_a_refusal_does_not_drop_the_branch(self, manager, repo):
        """Leaving the tree but deleting its branch would be the worst of both."""
        wt = manager.create("INC-00001")
        _relock(
            repo,
            wt.path,
            f"openjarvis-repair pid=1 host={socket.gethostname()} incident=INC-00001",
        )

        manager.remove(wt, succeeded=True)

        branches = subprocess.run(
            ["git", "branch", "--list", wt.branch],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert wt.branch in branches, (
            "the worktree was preserved but its branch was deleted, leaving a "
            "tree on a branch that no longer exists"
        )


class TestPruningStillWorksWithOwnershipLocks:
    """`git worktree prune` silently skips a locked worktree.

    It exits 0 and removes nothing. Since create() locks every worktree it
    makes, a bare prune stopped working the moment ownership was introduced: a
    directory removed out from under its registration left the lock behind, the
    branch went on counting as checked out somewhere, `branch -D` failed,
    `worktree add -b` failed, and the feature or incident became unretryable
    with a git error an operator cannot act on. That is exactly the bug pruning
    exists to prevent, reintroduced by the fix for a different one.

    The rule that resolves it: a registration whose directory is gone cannot be
    protecting anybody's work, whoever claimed it. One whose directory is still
    there is never touched.
    """

    def test_a_vanished_directory_can_be_recreated(self, manager, repo):
        """The end-to-end symptom: create, lose the directory, create again."""
        first = manager.create("INC-00001")
        shutil.rmtree(first.path)

        second = manager.create("INC-00001")

        assert second.branch == first.branch
        assert Path(second.path).is_dir()

    def test_the_stale_registration_is_actually_gone(self, manager, repo):
        first = manager.create("INC-00001")
        shutil.rmtree(first.path)

        manager.prune_stale_worktrees()

        listing = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert first.path not in listing, (
            "the locked registration survived the prune, so the branch still "
            "counts as checked out and cannot be reused"
        )

    def test_a_live_worktree_keeps_its_lock_through_a_prune(self, manager, repo):
        """Pruning must not become a way to strip another repair's claim."""
        wt = manager.create("INC-00001")
        _relock(
            repo,
            wt.path,
            f"openjarvis-repair pid=1 host={socket.gethostname()} incident=INC-00001",
        )

        manager.prune_stale_worktrees()

        assert Path(wt.path).exists()
        assert _lock_reason(repo, wt.path).startswith("openjarvis-repair"), (
            "a prune released a live repair's claim on its worktree"
        )

    def test_a_foreign_lock_on_a_vanished_directory_is_still_cleared(
        self, manager, repo
    ):
        """Ownership does not matter when there is nothing left to own."""
        wt = manager.create("INC-00001")
        _relock(repo, wt.path, "somebody else entirely, from another machine")
        shutil.rmtree(wt.path)

        manager.prune_stale_worktrees()

        second = manager.create("INC-00001")
        assert Path(second.path).is_dir()
