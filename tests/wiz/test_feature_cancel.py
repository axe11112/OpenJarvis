"""Stopping work, which has to be the easiest thing to ask for.

An autonomous system whose stop button needs configuring, an approval or an
exact identifier is one an operator cannot stop. So cancelling takes no
authority beyond being the operator, resolves "that one" when it safely can,
and asks rather than guesses when it cannot.

What it deliberately does not do is undo anything. Cancelling is Wiz agreeing to
take no further steps; unwinding a step already taken is a different decision
with different risks.
"""

from __future__ import annotations

import pytest

from openjarvis.wiz.authority import Actor, Authority, Channel
from openjarvis.wiz.brain import Request
from openjarvis.wiz.features.model import FeatureRequest, FeatureState, Priority
from openjarvis.wiz.features.pipeline import FeaturePipeline
from openjarvis.wiz.features.profile import EngineeringProfile
from openjarvis.wiz.features.store import FeatureStore
from openjarvis.wiz.memory import ProductMemory
from openjarvis.wiz.product import ProductVerbs, product_capabilities


@pytest.fixture
def store(tmp_path):
    store = FeatureStore(tmp_path / "features.db")
    yield store
    store.close()


class NoEngineer:
    """Cancelling never invokes the coding engine, so a stub proves it."""

    def available(self):
        return False


def build_pipeline(store, *, queue=None):
    """A pipeline with nothing attached but a store.

    Cancelling is the one verb that must work when everything else is missing:
    a stop button that needs a coding engine, a workspace and a browser is a
    stop button an operator cannot reach in the moment they need it.
    """
    return FeaturePipeline(
        store=store,
        profile=EngineeringProfile(name="wize", checkout="/tmp/x"),
        engineer=NoEngineer(),
        workspace=None,
        check_suite_factory=lambda profile: None,
        queue=queue,
        clock=lambda: "2026-08-22T10:00:00+00:00",
    )


@pytest.fixture
def pipeline(store):
    return build_pipeline(store)


def add(store, title, state=FeatureState.BUILDING):
    feature = FeatureRequest(
        id=store.next_id(),
        title=title,
        operator_request=title,
        source="telegram",
        actor_id="owner",
        priority=Priority.P3,
        state=state,
        created_at="2026-08-22T09:00:00+00:00",
        updated_at="2026-08-22T09:00:00+00:00",
    )
    return store.create(feature)


@pytest.fixture
def verbs(pipeline, tmp_path):
    return ProductVerbs(pipeline=pipeline, memory=ProductMemory(tmp_path / "memory.db"))


def ask(text="", **arguments):
    return Request(
        text=text,
        actor=Actor(actor_id="owner", channel=Channel.TELEGRAM, authenticated=True),
        arguments=arguments,
    )


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def test_cancelling_moves_a_running_feature_to_cancelled(pipeline, store):
    feature = add(store, "dark mode")
    cancelled = pipeline.cancel(feature.id)
    assert cancelled.state is FeatureState.CANCELLED
    assert store.get(feature.id).state is FeatureState.CANCELLED


def test_cancelling_records_why(pipeline, store):
    feature = add(store, "dark mode")
    pipeline.cancel(feature.id, reason="stopped by telegram")
    saved = store.get(feature.id)
    assert any("stopped by telegram" in entry["reason"] for entry in saved.history)


def test_cancelling_twice_is_not_an_error(pipeline, store):
    """An operator who did not see the first answer asks again."""
    feature = add(store, "dark mode")
    pipeline.cancel(feature.id)
    again = pipeline.cancel(feature.id)
    assert again.state is FeatureState.CANCELLED


def test_a_completed_feature_is_not_reopened_to_cancel_it(pipeline, store):
    feature = add(store, "shipped thing", state=FeatureState.COMPLETE)
    assert pipeline.cancel(feature.id).state is FeatureState.COMPLETE


def test_an_unknown_id_raises(pipeline):
    with pytest.raises(KeyError):
        pipeline.cancel("FEAT-99999")


def test_the_queue_slot_is_released(store):
    """A cancelled feature must not hold the slot reliability might need."""

    class Queue:
        def __init__(self):
            self.finished, self.cancelled = [], []

        def finish(self, feature_id):
            self.finished.append(feature_id)

        def cancel(self, feature_id):
            self.cancelled.append(feature_id)
            return True

    queue = Queue()
    pipeline = build_pipeline(store, queue=queue)
    feature = add(store, "dark mode")
    pipeline.cancel(feature.id)
    assert feature.id in queue.finished
    assert feature.id in queue.cancelled


def test_a_queue_that_cannot_forget_does_not_break_the_cancel(store):
    class Queue:
        def finish(self, feature_id):
            return None

        def cancel(self, feature_id):
            raise RuntimeError("boom")

    pipeline = build_pipeline(store, queue=Queue())
    feature = add(store, "dark mode")
    assert pipeline.cancel(feature.id).state is FeatureState.CANCELLED


# ---------------------------------------------------------------------------
# The verb
# ---------------------------------------------------------------------------


def test_stopping_the_only_running_thing_needs_no_identifier(verbs, store):
    feature = add(store, "dark mode")
    result = verbs.cancel_feature(ask("stop that task"))
    assert result["cancelled"] is True
    assert result["id"] == feature.id
    assert "stopped" in result["say"]
    assert "Nothing was undone" in result["say"]


def test_an_id_in_the_sentence_is_used(verbs, store):
    add(store, "dark mode")
    second = add(store, "download button")
    result = verbs.cancel_feature(ask(f"cancel {second.id}"))
    assert result["cancelled"] is True
    assert result["id"] == second.id


def test_two_running_things_produce_one_question_not_a_guess(verbs, store):
    add(store, "dark mode")
    add(store, "download button")
    result = verbs.cancel_feature(ask("stop that"))
    assert result["cancelled"] is False
    assert result["ambiguous"] is True
    assert result["say"].count("?") == 1
    assert len(result["candidates"]) == 2


def test_nothing_running_is_said_plainly(verbs):
    result = verbs.cancel_feature(ask("stop that"))
    assert result["cancelled"] is False
    assert "nothing is running" in result["say"]


def test_an_unknown_id_is_answered_not_raised(verbs):
    result = verbs.cancel_feature(ask("", feature_id="FEAT-99999"))
    assert result["cancelled"] is False
    assert "FEAT-99999" in result["say"]


def test_an_unconfigured_pipeline_says_so(tmp_path):
    verbs = ProductVerbs(pipeline=None, memory=None)
    assert verbs.cancel_feature(ask("stop that"))["cancelled"] is False


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


def test_stopping_needs_less_authority_than_starting():
    """The channel that could begin something must be able to end it."""
    specs = {
        s.name: s
        for s in product_capabilities(
            pipeline_available=lambda: None, memory_available=lambda: None
        )
    }
    assert specs["feature.cancel"].authority is Authority.SAFE_ACTION
    assert specs["feature.build"].authority is Authority.CODE_WRITE
