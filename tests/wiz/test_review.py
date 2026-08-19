"""The independent review: a second opinion that stays a second opinion."""

from __future__ import annotations

from openjarvis.wiz.features.engineer import (
    ClaudeCodeEngineeringAgent,
    EngineeringSession,
)
from openjarvis.wiz.features.model import FeatureRequest
from openjarvis.wiz.features.review import IndependentReviewer, ReviewReport


class ScriptedEngineer(ClaudeCodeEngineeringAgent):
    def __init__(self, session=None, explode=False):
        super().__init__(agent_factory=self._never_called)
        self.session = session or EngineeringSession(
            mode="plan", succeeded=True, claim="Looks reasonable. One nit in Card.tsx."
        )
        self.explode = explode
        self.calls = []

    @staticmethod
    def _never_called(**kwargs):  # pragma: no cover - guards the double
        raise AssertionError("the scripted engineer should not build a CLI agent")

    def available(self):
        return True

    def plan(self, pack, *, workspace):
        self.calls.append((pack, workspace))
        if self.explode:
            raise RuntimeError("the CLI died")
        return self.session

    def build(self, pack, *, workspace):  # pragma: no cover - must never run
        raise AssertionError("a reviewer must never build")


def reviewed_feature(lines=80, plan="I will rewrite the session store"):
    feature = FeatureRequest(
        id="FEAT-00050",
        title="Add a download button",
        operator_request='Add a "Download report" button',
        repository="acme/wize",
        branch="wiz/feature/FEAT-00050",
        plan=plan,
        acceptance=["CONTENT: the button is there"],
    )
    attempt = feature.next_attempt(at="t1", hypothesis="first attempt")
    attempt.claim = "I added the button and it definitely works"
    attempt.changed_files = ["src/components/Summary.tsx"]
    attempt.lines_changed = lines
    return feature


class TestIndependence:
    def test_the_reviewer_is_not_told_what_the_implementer_thought(self):
        # The whole value is that it has not already decided. Handing it the
        # plan and the implementer's account of its own work is precisely what
        # would talk it into agreeing.
        engineer = ScriptedEngineer()
        IndependentReviewer(engineer=engineer).review(
            reviewed_feature(), workspace="/tmp/wt"
        )
        rendered = engineer.calls[0][0].render()
        assert "I will rewrite the session store" not in rendered
        assert "definitely works" not in rendered

    def test_the_reviewer_is_told_what_the_operator_asked_for(self):
        engineer = ScriptedEngineer()
        IndependentReviewer(engineer=engineer).review(
            reviewed_feature(), workspace="/tmp/wt"
        )
        assert "Download report" in engineer.calls[0][0].render()

    def test_the_reviewer_runs_read_only(self):
        # It uses the planning path, whose tool list has no writer in it.
        engineer = ScriptedEngineer()
        IndependentReviewer(engineer=engineer).review(
            reviewed_feature(), workspace="/tmp/wt"
        )
        assert len(engineer.calls) == 1  # a plan session, never a build one

    def test_every_dimension_is_asked_about(self):
        engineer = ScriptedEngineer()
        IndependentReviewer(engineer=engineer).review(
            reviewed_feature(), workspace="/tmp/wt"
        )
        rendered = engineer.calls[0][0].render()
        for word in ("correctness", "regression risk", "security", "missing tests"):
            assert word in rendered


class TestItStaysAdvisory:
    def test_the_report_carries_no_verdict_the_pipeline_could_read(self):
        # "Advisory" is the sort of property that quietly stops being true the
        # first time a review catches something real.
        fields = set(ReviewReport().to_dict())
        assert "passed" not in fields
        assert "verdict" not in fields
        assert "approved" not in fields

    def test_a_reviewer_calling_something_critical_only_sets_a_suggestion(self):
        engineer = ScriptedEngineer(
            session=EngineeringSession(
                mode="plan",
                succeeded=True,
                claim="This is a critical security issue and must not ship.",
            )
        )
        report = IndependentReviewer(engineer=engineer).review(
            reviewed_feature(), workspace="/tmp/wt"
        )
        assert report["blocking_suggested"] is True
        # And nothing in the pipeline consults it.
        import inspect

        from openjarvis.wiz.features import pipeline

        assert "blocking_suggested" not in inspect.getsource(pipeline)

    def test_a_failed_review_is_not_a_failed_feature(self):
        engineer = ScriptedEngineer(explode=True)
        report = IndependentReviewer(engineer=engineer).review(
            reviewed_feature(), workspace="/tmp/wt"
        )
        assert report["ran"] is False
        assert "CLI died" in report["error"]


class TestWhenItIsWorthRunning:
    def test_a_tiny_change_does_not_get_its_own_session(self):
        # One Claude slot on this machine. A one-line copy change needs the
        # tests that already ran, not a correctness review.
        engineer = ScriptedEngineer()
        report = IndependentReviewer(engineer=engineer, min_lines=20).review(
            reviewed_feature(lines=3), workspace="/tmp/wt"
        )
        assert not report["ran"]
        assert engineer.calls == []

    def test_a_feature_with_nothing_built_is_not_reviewed(self):
        engineer = ScriptedEngineer()
        feature = FeatureRequest(id="FEAT-1", title="x", operator_request="x")
        report = IndependentReviewer(engineer=engineer).review(
            feature, workspace="/tmp/wt"
        )
        assert not report["ran"]
        assert engineer.calls == []


class TestTheDiff:
    def test_the_diff_is_read_from_git_when_a_worktree_is_available(self):
        class FakeWorkspace:
            def __init__(self):
                self.asked = []

            def diff(self, worktree, **kwargs):
                self.asked.append(worktree)
                return "--- a/x.tsx\n+++ b/x.tsx\n+<button>Download</button>"

        workspace = FakeWorkspace()
        engineer = ScriptedEngineer()
        IndependentReviewer(engineer=engineer, workspace=workspace).review(
            reviewed_feature(), workspace="/tmp/wt", worktree=object()
        )
        assert workspace.asked
        assert "<button>Download</button>" in engineer.calls[0][0].render()
