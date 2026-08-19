"""Product memory: what was built, why, and finding it again."""

from __future__ import annotations

import pytest

from openjarvis.wiz.features.model import FeatureRequest, FeatureState
from openjarvis.wiz.memory import MemoryEntry, ProductMemory, summarise


@pytest.fixture
def memory(tmp_path):
    store = ProductMemory(tmp_path / "memory.db")
    yield store
    store.close()


def entry(kind="feature", title="Add a download button", **kwargs):
    return MemoryEntry(kind=kind, title=title, **kwargs)


class TestWhatCanBeRemembered:
    def test_an_unknown_kind_is_refused(self):
        # An entry whose kind nobody recognises cannot be filtered, ranked or
        # explained.
        with pytest.raises(ValueError, match="unknown memory kind"):
            MemoryEntry(kind="vibes", title="something")

    def test_an_entry_needs_a_title(self):
        with pytest.raises(ValueError, match="needs a title"):
            MemoryEntry(kind="feature", title="   ")

    def test_remembering_the_same_thing_twice_updates_it(self, memory):
        memory.remember(entry(subject="FEAT-1", body="building"))
        memory.remember(entry(subject="FEAT-1", body="shipped"))
        assert memory.count() == 1
        assert "shipped" in memory.recent()[0].body


class TestTheQuestionsAnOperatorAsks:
    def test_what_did_we_build_yesterday(self, memory):
        memory.remember(entry(subject="FEAT-1", at="2026-08-18T10:00:00+00:00"))
        memory.remember(
            entry(
                subject="FEAT-2", title="Coach summary", at="2026-08-19T10:00:00+00:00"
            )
        )
        titles = [e.title for e in memory.yesterday(today="2026-08-19")]
        assert titles == ["Add a download button"]

    def test_why_did_we_change_onboarding(self, memory):
        # The reason is the whole point. "We changed onboarding" is in the git
        # log; the reason is not, and it is the useful half.
        memory.record_decision(
            subject="FEAT-9",
            title="Simplified onboarding to two steps",
            because="three people in a row could not find the club code",
            at="2026-08-17T09:00:00+00:00",
        )
        found = memory.search("onboarding")
        assert found
        assert "club code" in found[0].body

    def test_what_is_being_built_now(self, memory):
        memory.remember(
            entry(
                subject="FEAT-3",
                at="2026-08-19T11:00:00+00:00",
                metadata={"state": "BUILDING"},
            )
        )
        building = [
            e
            for e in memory.recent(kinds=["feature"])
            if e.metadata.get("state") == "BUILDING"
        ]
        assert building


class TestSearch:
    def test_full_text_search_is_available(self, memory):
        # If this SQLite lacks FTS5 the store still works, but the operator's
        # experience is materially worse, so it is worth knowing.
        assert memory.full_text_search

    def test_the_best_match_comes_first(self, memory):
        memory.remember(
            entry(
                subject="A", title="Coach weekly summary", body="a summary for coaches"
            )
        )
        memory.remember(entry(subject="B", title="Download button", body="unrelated"))
        found = memory.search("coach summary")
        assert found[0].subject == "A"

    def test_a_question_is_a_search_not_a_syntax_error(self, memory):
        # FTS5 treats quotes and parentheses as syntax. An operator typing a
        # question must not get an exception.
        memory.record_decision(
            subject="FEAT-9",
            title="Simplified onboarding",
            because="people got stuck",
        )
        assert memory.search('why did we change "onboarding" (again)?')

    def test_terms_are_ored_so_a_question_still_finds_things(self, memory):
        memory.remember(entry(subject="A", title="Coach weekly summary"))
        # "why did we build the" matches nothing; "coach" matches. ANDing the
        # terms would return silence for a perfectly reasonable question.
        assert memory.search("why did we build the coach thing")

    def test_search_can_be_narrowed_by_kind(self, memory):
        memory.remember(entry(subject="A", title="Coach summary"))
        memory.remember(
            entry(kind="research", subject="R", title="Coach collaboration")
        )
        found = memory.search("coach", kinds=["research"])
        assert [e.kind for e in found] == ["research"]

    def test_an_empty_query_returns_what_is_recent(self, memory):
        memory.remember(entry(subject="A", at="2026-08-19T10:00:00+00:00"))
        assert memory.search("   ")

    def test_nothing_matching_returns_nothing_rather_than_everything(self, memory):
        memory.remember(entry(subject="A", title="Coach summary"))
        assert memory.search("zzzzqqqq") == []


class TestFromAFeature:
    def test_a_feature_is_remembered_as_a_sentence_not_a_transcript(self, memory):
        feature = FeatureRequest(
            id="FEAT-00042",
            title="Add a download button",
            operator_request='Add a "Download report" button to /coach/summary',
            source="telegram",
            risk="LOW",
            state=FeatureState.READY,
            preview_url="https://preview.app",
            pr_url="https://github.com/a/b/pull/7",
            created_at="2026-08-19T09:00:00+00:00",
            updated_at="2026-08-19T10:00:00+00:00",
        )
        attempt = feature.next_attempt(at="t1")
        attempt.claim = "I did a lot of thinking about this and here is my reasoning"
        attempt.changed_files = ["src/components/Summary.tsx"]
        feature.metadata["verification"] = {"summary": "all 6 checks passed"}

        memory.remember_feature(feature)
        remembered = memory.recent()[0]
        assert "Download report" in remembered.body
        assert "all 6 checks passed" in remembered.body
        assert "pull/7" in remembered.url
        # No reasoning is stored. §27, and because stored reasoning reads as
        # authoritative six months later when it was one session's guess.
        assert "thinking about this" not in remembered.body

    def test_a_feature_remembered_twice_does_not_duplicate(self, memory):
        feature = FeatureRequest(
            id="FEAT-1", title="A thing", operator_request="do a thing"
        )
        memory.remember_feature(feature)
        feature.state = FeatureState.READY
        memory.remember_feature(feature)
        assert memory.count() == 1
        assert "READY" in memory.recent()[0].body


class TestRendering:
    def test_a_summary_is_a_short_list_of_facts(self, memory):
        memory.remember(entry(subject="A", at="2026-08-19T10:00:00+00:00"))
        memory.remember(
            entry(subject="B", title="Coach summary", at="2026-08-19T11:00:00+00:00")
        )
        rendered = summarise(memory.recent())
        assert rendered.count("\n") == 1
        assert "2026-08-19" in rendered

    def test_an_empty_day_says_so_rather_than_inventing_something(self):
        assert summarise([]) == "Nothing to report."

    def test_a_summary_is_bounded(self, memory):
        for index in range(30):
            memory.remember(entry(subject=f"F{index}", title=f"Feature {index}"))
        assert len(summarise(memory.recent(limit=30), limit=5).splitlines()) == 5
