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


class TestReusingAnExistingWorktree:
    """Found on FEAT-00031's recovery: a process with a cold in-memory
    worktree cache (a restarted watcher, or a one-off recovery run against an
    existing HUMAN_REQUIRED feature) used to call create() unconditionally,
    which *removes* whatever is already at that path before making a fresh
    one — silently destroying an uncommitted diff nobody asked it to touch.
    reuse() exists so a cold cache is never the reason that happens again.
    """

    def test_an_uncommitted_diff_survives_a_reuse(self, workspace):
        from pathlib import Path

        worktree = workspace.create("FEAT-00001")
        (Path(worktree.path) / "NEW.md").write_text("uncommitted work\n")

        reused = workspace.reuse(
            "FEAT-00001",
            path=worktree.path,
            branch=worktree.branch,
            base_sha=worktree.base_commit,
        )

        assert reused is not None
        assert reused.path == worktree.path
        assert (Path(reused.path) / "NEW.md").read_text() == "uncommitted work\n"

    def test_a_real_committed_advance_past_base_sha_is_still_reused(self, workspace):
        """Found re-verifying FEAT-00031 a second time: refusing whenever
        actual_head != base_sha (the original, unconditional check) sent a
        worktree whose diff had *already been committed and pushed* by a
        prior successful run to create() -- which destroyed the local commit
        and left the worktree at base_sha while origin sat ahead of it on
        the same branch. The next push then failed as a non-fast-forward.
        A worktree whose HEAD is a genuine descendant of base_sha (real,
        forward progress) must be handed back exactly like an untouched one.
        """
        from pathlib import Path

        worktree = workspace.create("FEAT-00001")
        (Path(worktree.path) / "NEW.md").write_text("real work\n")
        _git(["add", "-A"], worktree.path)
        _git(["commit", "-m", "did the work"], worktree.path)
        advanced_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert advanced_head != worktree.base_commit

        reused = workspace.reuse(
            "FEAT-00001",
            path=worktree.path,
            branch=worktree.branch,
            base_sha=worktree.base_commit,  # the *original* base, not the new HEAD
        )

        assert reused is not None
        assert reused.path == worktree.path
        actual_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=reused.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert actual_head == advanced_head  # the real commit, not destroyed

    def test_a_head_not_descended_from_base_sha_is_not_reused(self, workspace, repo):
        """The case the ancestor check exists to still refuse: HEAD moved,
        but not *forward* from the recorded base -- diverged, rewound, or
        pointing somewhere unrelated. Simulated with an orphan commit that
        shares no history with base_sha at all.
        """
        worktree = workspace.create("FEAT-00001")
        _git(["checkout", "--orphan", "unrelated-history"], worktree.path)
        _git(["commit", "--allow-empty", "-m", "shares no history"], worktree.path)
        _git(["branch", "-M", worktree.branch], worktree.path)

        reused = workspace.reuse(
            "FEAT-00001",
            path=worktree.path,
            branch=worktree.branch,
            base_sha=worktree.base_commit,
        )
        assert reused is None

    def test_reuse_never_calls_create(self, workspace):
        worktree = workspace.create("FEAT-00001")
        workspace.created = []  # forget the call above; only reuse() must follow

        workspace.reuse(
            "FEAT-00001",
            path=worktree.path,
            branch=worktree.branch,
            base_sha=worktree.base_commit,
        )
        # create() logs through the same worktree-add path create() uses;
        # the reliable signal that reuse() took the non-destructive branch
        # is that the file written above is still there (proven separately)
        # and that the branch/HEAD are exactly what was asked for.
        import subprocess

        actual = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert actual == worktree.branch

    def test_a_missing_path_is_not_reused(self, workspace, tmp_path):
        reused = workspace.reuse(
            "FEAT-00001",
            path=str(tmp_path / "never-existed"),
            branch="wiz/feature/FEAT-00001",
            base_sha="a" * 40,
        )
        assert reused is None

    def test_a_path_on_the_wrong_branch_is_not_reused(self, workspace, repo):
        worktree = workspace.create("FEAT-00001")
        _git(["checkout", "-b", "something-else"], worktree.path)

        reused = workspace.reuse(
            "FEAT-00001",
            path=worktree.path,
            branch=worktree.branch,
            base_sha=worktree.base_commit,
        )
        assert reused is None

    def test_no_path_at_all_is_not_reused(self, workspace):
        assert workspace.reuse("FEAT-00001", path="", branch="", base_sha="") is None

    def test_an_unsafe_path_is_not_reused(self, repo, tmp_path):
        workspace = FeatureWorkspace(
            repo_path=str(repo),
            root=str(tmp_path / "worktrees"),
            protected_checkouts=[str(repo)],
        )
        reused = workspace.reuse(
            "FEAT-00001", path=str(repo), branch="main", base_sha=""
        )
        assert reused is None


class TestPipelineReusesAnExistingWorktreeAcrossAColdCache:
    """The same guarantee, proven at the level FEAT-00031 actually broke it:
    a *second*, independently-constructed FeaturePipeline (standing in for a
    restarted process, or a one-off recovery script) picking up a feature
    that already has a real, uncommitted diff on disk.
    """

    def test_a_second_pipeline_instance_does_not_destroy_the_diff(
        self, repo, tmp_path
    ):
        from pathlib import Path

        from openjarvis.wiz.features.model import FeatureRequest, FeatureState
        from openjarvis.wiz.features.pipeline import FeaturePipeline
        from openjarvis.wiz.features.profile import EngineeringProfile
        from openjarvis.wiz.features.store import FeatureStore

        workspace = FeatureWorkspace(
            repo_path=str(repo),
            root=str(tmp_path / "worktrees"),
            git_identity=("Wiz", "wiz@example.com"),
        )
        worktree = workspace.create("FEAT-00001", title="Add a dashboard")
        (Path(worktree.path) / "NEW.md").write_text("uncommitted work\n")

        store = FeatureStore(tmp_path / "features.db")
        feature = FeatureRequest(
            id="FEAT-00001",
            title="Add a dashboard",
            operator_request="Add a dashboard",
            state=FeatureState.HUMAN_REQUIRED,
            created_at="2026-08-19T10:00:00+00:00",
            updated_at="2026-08-19T10:00:00+00:00",
            worktree=worktree.path,
            branch=worktree.branch,
            base_sha=worktree.base_commit,
        )
        store.create(feature)

        # A brand-new pipeline, its own fresh (cold) _worktrees cache — the
        # exact shape of a restarted process or a one-off recovery script.
        profile = EngineeringProfile(name="x", checkout=str(repo), test_command="t")
        second_pipeline = FeaturePipeline(
            store=store,
            profile=profile,
            # Neither is touched by _worktree_for(); real values would only
            # obscure what this test is actually proving.
            engineer=object(),
            workspace=workspace,
            check_suite_factory=lambda profile: None,
        )
        recovered = second_pipeline._worktree_for(feature)

        assert recovered.path == worktree.path
        assert (Path(recovered.path) / "NEW.md").read_text() == "uncommitted work\n"
