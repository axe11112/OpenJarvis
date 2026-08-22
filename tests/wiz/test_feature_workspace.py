"""Feature worktrees, and the directories an agent must never be handed."""

from __future__ import annotations

import subprocess

import pytest

from openjarvis.wiz.features.workspace import (
    FEATURE_BRANCH_PREFIX,
    FeatureWorkspace,
    UnsafeWorkspace,
    branch_slug,
)


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    _git(["init", "-b", "main"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)
    (root / "README.md").write_text("hello\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "initial"], root)
    return root


@pytest.fixture
def workspace(repo, tmp_path):
    return FeatureWorkspace(
        repo_path=str(repo),
        root=str(tmp_path / "worktrees"),
        git_identity=("Wiz", "wiz@example.com"),
    )


class TestNaming:
    def test_a_branch_reads_like_a_feature(self, workspace):
        branch = workspace.branch_name_for("FEAT-00001", "Add a coach dashboard")
        assert branch == f"{FEATURE_BRANCH_PREFIX}FEAT-00001-add-a-coach-dashboard"

    def test_a_titleless_feature_still_gets_a_branch(self, workspace):
        assert workspace.branch_name_for("FEAT-00001") == (
            f"{FEATURE_BRANCH_PREFIX}FEAT-00001"
        )

    def test_slugs_are_git_safe(self):
        assert branch_slug("Fix the (mobile) layout — urgently!") == (
            "fix-the-mobile-layout-urgently"
        )

    def test_slugs_are_short(self):
        slug = branch_slug("one two three four five six seven eight")
        assert slug.count("-") == 4

    def test_the_prefix_is_not_the_incident_prefix(self):
        # A feature branch and a repair branch must be distinguishable at a
        # glance, and must not collide.
        assert FEATURE_BRANCH_PREFIX.startswith("wiz/feature/")


class TestIsolation:
    def test_a_worktree_is_created_outside_the_checkout(self, workspace, repo):
        from pathlib import Path

        worktree = workspace.create("FEAT-00001", title="Add a dashboard")
        resolved = Path(worktree.path).resolve()
        assert resolved != repo.resolve()
        assert repo.resolve() not in resolved.parents
        assert worktree.branch.startswith(FEATURE_BRANCH_PREFIX)

    def test_the_base_commit_is_an_immutable_sha(self, workspace):
        worktree = workspace.create("FEAT-00001")
        assert len(worktree.base_commit) == 40

    def test_editing_the_worktree_does_not_touch_the_checkout(self, workspace, repo):
        from pathlib import Path

        worktree = workspace.create("FEAT-00001")
        (Path(worktree.path) / "NEW.md").write_text("x")
        assert not (repo / "NEW.md").exists()

    def test_two_features_get_different_worktrees(self, workspace):
        first = workspace.create("FEAT-00001")
        second = workspace.create("FEAT-00002")
        assert first.path != second.path
        assert first.branch != second.branch


class TestProtectedCheckouts:
    def test_the_source_checkout_can_never_be_the_workspace(self, repo):
        workspace = FeatureWorkspace(repo_path=str(repo), root=str(repo))
        with pytest.raises(UnsafeWorkspace):
            workspace.create("FEAT-00001")

    def test_a_root_inside_the_source_checkout_is_refused(self, repo):
        workspace = FeatureWorkspace(repo_path=str(repo), root=str(repo / "worktrees"))
        with pytest.raises(UnsafeWorkspace):
            workspace.create("FEAT-00001")

    def test_a_listed_live_checkout_is_refused(self, repo, tmp_path):
        live = tmp_path / "live-openjarvis"
        live.mkdir()
        workspace = FeatureWorkspace(
            repo_path=str(repo),
            root=str(live / "worktrees"),
            protected_checkouts=[str(live)],
        )
        with pytest.raises(UnsafeWorkspace):
            workspace.create("FEAT-00001")

    def test_check_root_refuses_before_anything_is_created(self, repo):
        workspace = FeatureWorkspace(repo_path=str(repo), root=str(repo))
        with pytest.raises(UnsafeWorkspace):
            workspace.check_root()

    def test_a_safe_root_passes(self, workspace):
        workspace.check_root()  # must not raise


class TestReuse:
    def test_it_composes_the_reliability_workspace(self):
        # Not reimplemented: the SHA resolution, the identity-before-shell
        # ordering and the keep-failures-for-inspection behaviour all come from
        # the class the repair loop already uses.
        import inspect

        from openjarvis.wiz.features import workspace as module

        assert "RepairWorkspace" in inspect.getsource(module)

    def test_commits_are_authored_with_the_configured_identity(self, workspace):
        worktree = workspace.create("FEAT-00001")
        from pathlib import Path

        (Path(worktree.path) / "NEW.md").write_text("x\n")
        sha = workspace.commit_all(worktree, "feat: something")
        assert len(sha) == 40
        author = subprocess.run(
            ["git", "log", "-1", "--format=%an <%ae>"],
            cwd=worktree.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert author == "Wiz <wiz@example.com>"
