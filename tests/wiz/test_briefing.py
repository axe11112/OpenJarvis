"""The morning summary: short, factual, and quiet when there is nothing to say."""

from __future__ import annotations

import pytest

from openjarvis.wiz.briefing import Briefing, compose
from openjarvis.wiz.features.model import FeatureRequest, FeatureState
from openjarvis.wiz.features.store import FeatureStore


@pytest.fixture
def store(tmp_path):
    return FeatureStore(tmp_path / "features.db")


def feature(
    store, *, title, state, risk="LOW", updated="2026-08-18T10:00:00+00:00", **kw
):
    request = FeatureRequest(
        title=title,
        operator_request=title,
        state=FeatureState.RECEIVED,
        risk=risk,
        created_at=updated,
        updated_at=updated,
        **kw,
    )
    store.create(request)
    request.state = state
    request.updated_at = updated
    store.save(request)
    return request


class TestQuietWhenThereIsNothing:
    def test_an_empty_day_is_not_worth_sending(self, store):
        briefing = compose(store=store, now="2026-08-19")
        assert not briefing.worth_sending

    def test_it_still_renders_something_civil(self, store):
        rendered = compose(store=store, now="2026-08-19").render()
        assert "Good morning, Sir." in rendered
        assert "Nothing needs you." in rendered

    def test_anything_at_all_makes_it_worth_sending(self, store):
        feature(store, title="Add a download button", state=FeatureState.READY)
        assert compose(store=store, now="2026-08-19").worth_sending


class TestApprovalsComeFirst:
    def test_the_thing_with_a_deadline_is_above_the_good_news(self, store):
        feature(
            store,
            title="Change who can see results",
            state=FeatureState.HUMAN_REQUIRED,
            risk="HIGH",
        )
        feature(store, title="Add a download button", state=FeatureState.COMPLETE)
        rendered = compose(store=store, now="2026-08-19").render()
        assert rendered.index("needs your approval") < rendered.index("Yesterday")

    def test_an_approval_says_why(self, store):
        # "Z needs your approval because it changes permissions" is actionable;
        # "Z needs you" is a to-do somebody has to go and investigate.
        request = feature(
            store,
            title="Change who can see results",
            state=FeatureState.HUMAN_REQUIRED,
            risk="HIGH",
        )
        request.metadata["risk_reasons"] = ["it changes who can see other swimmers"]
        store.save(request)
        briefing = compose(store=store, now="2026-08-19")
        assert "because it changes who can see other swimmers" in briefing.needs_you[0]

    def test_a_ready_feature_waiting_on_a_person_is_surfaced(self, store):
        feature(
            store,
            title="Add a download button",
            state=FeatureState.READY,
            pr_url="https://github.com/a/b/pull/7",
        )
        briefing = compose(store=store, now="2026-08-19")
        assert any("ready and waiting" in line for line in briefing.needs_you)
        assert any("pull/7" in line for line in briefing.needs_you)

    def test_a_stopped_feature_says_what_stopped_it(self, store):
        request = feature(store, title="Add a chart", state=FeatureState.HUMAN_REQUIRED)
        request.history.append(
            {
                "at": "t",
                "from": "TESTING",
                "to": "HUMAN_REQUIRED",
                "reason": "I tried 3 times and could not get the chart working. "
                "The last problem was: a type error",
            }
        )
        store.save(request)
        briefing = compose(store=store, now="2026-08-19")
        assert "could not get the chart working" in briefing.needs_you[0]

    def test_a_stopped_reason_carrying_a_secret_is_redacted(self, store):
        # Some stop reasons are raw exception text from a GitHub or Vercel
        # client failure. This is an owner notification — the same surface
        # redact_secrets() is documented to protect.
        request = feature(store, title="Add a chart", state=FeatureState.HUMAN_REQUIRED)
        request.history.append(
            {
                "at": "t",
                "from": "READY",
                "to": "HUMAN_REQUIRED",
                "reason": "could not read the pull request: ghp_"
                + "a" * 36
                + " was rejected",
            }
        )
        store.save(request)
        briefing = compose(store=store, now="2026-08-19")
        assert "ghp_" + "a" * 36 not in briefing.needs_you[0]


class TestWhatHappened:
    def test_yesterdays_finished_work_is_reported(self, store):
        feature(
            store,
            title="Add a download button",
            state=FeatureState.COMPLETE,
            updated="2026-08-18T16:00:00+00:00",
        )
        briefing = compose(store=store, now="2026-08-19")
        assert briefing.built == ["Add a download button"]

    def test_todays_work_is_not_yesterdays(self, store):
        feature(
            store,
            title="Add a download button",
            state=FeatureState.COMPLETE,
            updated="2026-08-19T09:00:00+00:00",
        )
        assert compose(store=store, now="2026-08-19").built == []

    def test_work_in_progress_is_described_in_plain_words(self, store):
        # The internal vocabulary is precise and the operator did not agree to
        # learn it.
        feature(store, title="Add a coach summary", state=FeatureState.BUILDING)
        briefing = compose(store=store, now="2026-08-19")
        assert briefing.in_progress == ["Add a coach summary (writing the code)"]
        assert "BUILDING" not in briefing.render()

    def test_the_summary_stays_short(self, store):
        for index in range(30):
            feature(
                store,
                title=f"Feature {index}",
                state=FeatureState.COMPLETE,
                updated="2026-08-18T10:00:00+00:00",
            )
        briefing = compose(store=store, now="2026-08-19")
        assert len(briefing.built) <= 5


class TestHonestyAboutHealth:
    def test_a_healthy_site_is_said_plainly(self, store):
        briefing = compose(
            store=store,
            reliability=lambda: {"available": True, "open": 0},
            now="2026-08-19",
            site_name="Wize",
        )
        assert briefing.health == "Wize is healthy."

    def test_one_problem_is_named(self, store):
        briefing = compose(
            store=store,
            reliability=lambda: {
                "available": True,
                "open": 1,
                "incidents": [{"title": "Login form renders failed"}],
            },
            now="2026-08-19",
            site_name="Wize",
        )
        assert "Login form renders failed" in briefing.health

    def test_an_unavailable_subsystem_admits_it(self, store):
        # "I do not know" is shorter and more useful than a confident sentence
        # that is guessing.
        briefing = compose(
            store=store,
            reliability=lambda: {"available": False},
            now="2026-08-19",
            site_name="Wize",
        )
        assert "cannot see" in briefing.health
        assert "healthy" not in briefing.health

    def test_a_broken_reliability_source_does_not_break_the_summary(self, store):
        def explode():
            raise RuntimeError("the database is gone")

        briefing = compose(store=store, reliability=explode, now="2026-08-19")
        assert briefing.health == ""

    def test_with_no_reliability_at_all_nothing_is_claimed(self, store):
        assert compose(store=store, now="2026-08-19").health == ""


class TestItSendsNothingItself:
    def test_the_module_has_no_way_to_deliver_anything(self):
        # §29: do not enable noisy scheduled delivery without safe existing
        # authority. Enforced by there being nothing here that could — checked
        # on the imports, because that is where a delivery capability would
        # have to come from, rather than on the prose that explains why.
        import ast
        import inspect

        from openjarvis.wiz import briefing

        tree = ast.parse(inspect.getsource(briefing))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        for module in imported:
            assert not any(
                part in module
                for part in ("http", "requests", "notify", "telegram", "channels")
            ), module

    def test_composing_is_the_only_thing_it_does(self):
        # A public surface of exactly two names: the thing and the function
        # that builds it. Nothing to call that reaches anybody.
        from openjarvis.wiz import briefing

        assert set(briefing.__all__) == {"Briefing", "compose"}

    def test_a_broken_store_does_not_break_the_summary(self):
        class Broken:
            def list(self, **kwargs):
                raise RuntimeError("gone")

            def active(self, **kwargs):
                raise RuntimeError("gone")

        briefing = compose(store=Broken(), now="2026-08-19")
        assert isinstance(briefing, Briefing)
        assert not briefing.worth_sending


class _Entry:
    def __init__(self, title):
        self.title = title


class _Memory:
    def __init__(self, entries):
        self._entries = entries
        self.calls = []

    def on_day(self, day, *, kinds):
        self.calls.append((day, tuple(kinds)))
        return list(self._entries)


class TestMemoryIsOnlyAFallbackForAMissingStore:
    """Regression: a feature that failed and stopped at HUMAN_REQUIRED must
    never also be reported as "I built X" from memory's fallback, which
    remembers every feature *touched* that day regardless of outcome — a
    real bug caught by actually running `jarvis wiz morning` against
    FEAT-00002, which appeared under both "needs you" and "I built" at once.
    """

    def test_a_store_with_a_genuinely_quiet_day_does_not_fall_back_to_memory(
        self, store
    ):
        # The store answered — correctly, nothing was built — and that must
        # be the final answer, not a cue to go ask memory instead.
        feature(
            store,
            title="Error page glow: blue to violet",
            state=FeatureState.HUMAN_REQUIRED,
            updated="2026-08-18T13:47:08+00:00",
        )
        memory = _Memory([_Entry("Error page glow: blue to violet")])

        briefing = compose(store=store, memory=memory, now="2026-08-19")

        assert briefing.built == []
        assert not memory.calls, "memory should not even be asked"
        assert any("Error page glow" in item for item in briefing.needs_you)

    def test_memory_is_used_when_the_store_is_not_configured_at_all(self):
        memory = _Memory([_Entry("Add a download button")])
        briefing = compose(store=None, memory=memory, now="2026-08-19")
        assert briefing.built == ["Add a download button"]

    def test_memory_is_used_when_the_store_raises(self):
        class Broken:
            def list(self, **kwargs):
                raise RuntimeError("gone")

            def active(self, **kwargs):
                raise RuntimeError("gone")

        memory = _Memory([_Entry("Add a download button")])
        briefing = compose(store=Broken(), memory=memory, now="2026-08-19")
        assert briefing.built == ["Add a download button"]

    def test_a_store_with_something_genuinely_built_never_asks_memory(self, store):
        feature(
            store,
            title="Add a download button",
            state=FeatureState.READY,
            updated="2026-08-18T10:00:00+00:00",
        )
        memory = _Memory([_Entry("a completely different thing")])

        briefing = compose(store=store, memory=memory, now="2026-08-19")

        assert briefing.built == ["Add a download button"]
        assert not memory.calls
