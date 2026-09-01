"""One door, and what happens to each sentence that arrives at it.

The cases here are the operator's own examples — "How is Wize?", "Fix it",
"Improve the onboarding", "What is Claude working on?", "Stop that task" — plus
the two that decide whether the routing is sound rather than merely plausible:
what "fix it" means when nothing is failing, and what a stranger gets.
"""

from __future__ import annotations

import pytest

from openjarvis.core.config import JarvisConfig
from openjarvis.reliability.outage import OutageRegistry
from openjarvis.reliability.owner_commands import OwnerCommands
from openjarvis.reliability.types import Correlation, Incident, Severity
from openjarvis.wiz.authority import Authority, AuthorityPolicy, Channel
from openjarvis.wiz.features.model import FeatureState
from openjarvis.wiz.intake import TelegramIntake
from openjarvis.wiz.journal import WizJournal
from openjarvis.wiz.memory import ProductMemory
from openjarvis.wiz.owner_channel import OwnerDoor, TelegramOwnerDoor, build_owner_door
from openjarvis.wiz.product import ProductVerbs
from openjarvis.wiz.runtime import build_wiz
from tests.wiz.test_product import FakePipeline

OWNER = "555"
STRANGER = "999"


class Gate:
    def __init__(self):
        self.cleared = []

    def clear_cooldown(self, *keys):
        self.cleared.extend(k for k in keys if k)
        return list(keys)


@pytest.fixture
def outages():
    return OutageRegistry()


@pytest.fixture
def incidents(tmp_path):
    """A real incident store, because the read verbs consult one.

    Injected rather than defaulted: a probe and a handler that disagree about
    which database they are looking at can both be right in one process, and
    the way that shows up is a test passing only on a machine that happens to
    have a live database at the default path.
    """
    from openjarvis.reliability.store import IncidentStore

    path = tmp_path / "incidents.db"
    IncidentStore(path).close()
    return lambda: IncidentStore(path)


@pytest.fixture
def runtime(tmp_path, incidents):
    return build_wiz(
        home=tmp_path,
        policy=AuthorityPolicy(
            grants={
                Channel.TELEGRAM: frozenset({Authority.READ, Authority.SAFE_ACTION})
            }
        ),
        journal=WizJournal(tmp_path / "journal.jsonl"),
        config=JarvisConfig(),
        store_factory=incidents,
    )


@pytest.fixture
def door(runtime, outages):
    return OwnerDoor(
        commands=OwnerCommands(allowed_chat_ids=OWNER, outages=outages, gate=Gate()),
        intake=TelegramIntake(wiz=runtime.wiz, owner_chat_ids=[OWNER]),
        allowed_chat_ids=OWNER,
        outages=outages,
    )


def open_an_outage(outages, component="website"):
    incident = Incident(
        fingerprint=f"fp_{component}",
        severity=Severity.CRITICAL,
        component=component,
        title=f"{component} is unreachable",
        id="INC-00001",
        probe_id="homepage",
        correlation=Correlation(deployment_id="9f31c04"),
        metadata={"failure_kind": "navigation"},
    )
    outages.assign(incident)
    return incident


# ---------------------------------------------------------------------------
# Who is allowed to speak
# ---------------------------------------------------------------------------


def test_a_stranger_is_answered_with_silence(door):
    reply = door.receive(chat_id=STRANGER, text="Fix it")
    assert reply.authorized is False
    assert reply.text == ""


def test_an_empty_allowlist_authorises_nobody(runtime, outages):
    door = OwnerDoor(
        intake=TelegramIntake(wiz=runtime.wiz, owner_chat_ids=[]),
        allowed_chat_ids="",
        outages=outages,
    )
    assert door.receive(chat_id=OWNER, text="how are you").authorized is False


def test_an_empty_message_is_not_answered(door):
    assert door.receive(chat_id=OWNER, text="   ").text == ""


# ---------------------------------------------------------------------------
# Which half answers
# ---------------------------------------------------------------------------


def test_fix_it_reaches_reliability_when_something_is_failing(door, outages):
    open_an_outage(outages)
    reply = door.receive(chat_id=OWNER, text="Fix it")
    assert reply.route == "reliability"
    assert reply.text == "Sir, I'm working on it."


def test_fix_it_is_a_request_for_work_when_nothing_is_failing(door):
    """The tie-break that makes the routing sound rather than plausible.

    Both halves have a claim on "fix". Answering "nothing is failing at the
    moment" to somebody asking for a broken form to be fixed is true and
    useless; the world decides, not the wording.
    """
    reply = door.receive(chat_id=OWNER, text="Fix the sign-up form")
    assert reply.route == "wiz"
    assert reply.capability == "feature.request"


def test_fix_whatever_is_wrong_reaches_reliability_during_an_outage(door, outages):
    open_an_outage(outages)
    assert (
        door.receive(chat_id=OWNER, text="Fix whatever is wrong with production").route
        == "reliability"
    )


def test_a_status_question_always_reaches_the_half_that_knows(door):
    """Only the reliability half knows whether anything is wrong."""
    assert door.receive(chat_id=OWNER, text="what's happening?").route == "reliability"


# ---------------------------------------------------------------------------
# The operator's own sentences
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence,capability",
    [
        ("How is Wize?", "reliability.status"),
        ("what can you do", "wiz.capabilities"),
        ("how are you", "wiz.health"),
        ("what are you allowed to do", "wiz.authority"),
        ("Add a better recovery dashboard", "feature.request"),
        ("Improve the onboarding", "feature.request"),
        ("Build this feature: dark mode", "feature.request"),
        (
            "Users are having trouble signing up — investigate and fix it",
            "feature.request",
        ),
        ("What is Claude working on?", "feature.list"),
        ("Why hasn't this shipped?", "feature.status"),
        ("Stop that task", "feature.cancel"),
        ("What happened last night?", "product.recent"),
        ("Show me what changed", "product.recent"),
        ("Why did conversions drop?", "product.search"),
    ],
)
def test_every_sentence_the_operator_gave_reaches_a_verb(door, sentence, capability):
    reply = door.receive(chat_id=OWNER, text=sentence)
    assert reply.capability == capability, sentence
    assert reply.text, "the owner must never be answered with silence"


def test_an_unrecognised_sentence_offers_the_list_rather_than_guessing(door):
    reply = door.receive(chat_id=OWNER, text="the weather is nice today")
    assert reply.capability == ""
    assert "what I can do" in reply.text


# ---------------------------------------------------------------------------
# Honesty
# ---------------------------------------------------------------------------


def test_an_unconfigured_verb_names_the_missing_thing(door):
    """Understood, and refused for the real reason."""
    reply = door.receive(chat_id=OWNER, text="Add a download button")
    assert reply.handled is False
    assert "no engineering target is configured" in reply.text


def test_a_missing_incident_database_is_explained_not_quoted(tmp_path, outages):
    """The refusal used to be "I cannot do that here: none."

    ``FileNotFoundError("none")`` reached the operator's phone as its own
    ``str()``. An exception message is written for a developer reading a
    traceback; it is not an explanation, and it must not be used as one.
    """
    runtime = build_wiz(
        home=tmp_path,
        policy=AuthorityPolicy(grants={Channel.TELEGRAM: frozenset({Authority.READ})}),
        journal=WizJournal(tmp_path / "j.jsonl"),
        config=JarvisConfig(),
        store_factory=lambda: (_ for _ in ()).throw(FileNotFoundError("none")),
    )
    door = OwnerDoor(
        intake=TelegramIntake(wiz=runtime.wiz, owner_chat_ids=[OWNER]),
        allowed_chat_ids=OWNER,
        outages=outages,
    )
    reply = door.receive(chat_id=OWNER, text="How is Wize?")
    assert "none." not in reply.text
    assert "no incident database" in reply.text


def test_a_read_verb_answers_in_english_rather_than_facts(door):
    reply = door.receive(chat_id=OWNER, text="How is Wize?")
    assert reply.text.startswith("Sir,")
    assert "{" not in reply.text and "available" not in reply.text


def test_no_internal_vocabulary_reaches_the_owner(door, outages):
    open_an_outage(outages)
    for sentence in ("How is Wize?", "how are you", "what can you do", "Fix it"):
        text = door.receive(chat_id=OWNER, text=sentence).text.lower()
        for jargon in (
            "capability",
            "fingerprint",
            "human_required",
            "outage_key",
            "authority.",
            "traceback",
        ):
            assert jargon not in text, (sentence, jargon)


# ---------------------------------------------------------------------------
# The door must not be able to break Wiz
# ---------------------------------------------------------------------------


def test_a_handler_that_explodes_is_answered_not_raised(door):
    class Exploding:
        def receive(self, **_):
            raise RuntimeError("boom")

    door.intake = Exploding()
    reply = door.receive(chat_id=OWNER, text="how are you")
    assert reply.route == "refused"
    assert reply.text


def test_an_unreadable_outage_registry_does_not_stop_the_door(door):
    class Broken:
        def open_outages(self):
            raise RuntimeError("disk on fire")

    door.outages = Broken()
    # "Fix it" with no readable outage state falls through to the dispatcher
    # rather than failing: an unanswerable message is worse than a misrouted one.
    assert door.receive(chat_id=OWNER, text="Fix it").route == "wiz"


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def test_the_door_is_off_unless_it_was_turned_on():
    config = JarvisConfig()
    assert build_owner_door(config) is None


def test_the_door_is_built_when_owner_commands_are_enabled(runtime):
    config = JarvisConfig()
    config.reliability.notify.enabled = True
    config.reliability.notify.accept_owner_commands = True
    config.channel.telegram.allowed_chat_ids = OWNER
    door = build_owner_door(config, runtime=runtime)
    assert door is not None
    assert door.intake is not None


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------


class Channel_:
    def __init__(self):
        self.sent = []
        self.handler = None

    def on_message(self, handler):
        self.handler = handler

    def connect(self):
        return None

    def disconnect(self):
        return None

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))


class Transport:
    def __init__(self, channel):
        self.channel = channel


def test_the_listener_replies_through_the_same_channel(door):
    channel = Channel_()
    listener = TelegramOwnerDoor(door=door, notifier=Transport(channel))
    assert listener.start() is True

    channel.handler(
        type(
            "M",
            (),
            {"conversation_id": OWNER, "sender": OWNER, "content": "how are you"},
        )()
    )
    assert channel.sent and channel.sent[0][0] == OWNER
    assert channel.sent[0][1].startswith("Sir,")


def test_a_stranger_gets_no_reply_on_the_wire(door):
    channel = Channel_()
    listener = TelegramOwnerDoor(door=door, notifier=Transport(channel))
    listener.start()
    channel.handler(
        type(
            "M",
            (),
            {"conversation_id": STRANGER, "sender": STRANGER, "content": "Fix it"},
        )()
    )
    assert channel.sent == []


def test_a_malformed_message_never_escapes_into_the_polling_thread(door):
    channel = Channel_()
    listener = TelegramOwnerDoor(door=door, notifier=Transport(channel))
    listener.start()
    channel.handler(object())  # must not raise


def test_a_transport_that_cannot_receive_is_reported_not_crashed(door):
    listener = TelegramOwnerDoor(door=door, notifier=object())
    assert listener.start() is False


# ---------------------------------------------------------------------------
# The auto-build handoff: feature.request -> feature.build, in one message
# ---------------------------------------------------------------------------


def _complete_profile(tmp_path):
    from openjarvis.wiz.features.profile import EngineeringProfile

    return EngineeringProfile(
        name="test-target", checkout=str(tmp_path / "checkout"), test_command="npm test"
    )


@pytest.fixture
def buildable_runtime(tmp_path, incidents):
    """A runtime with a working pipeline and CODE_WRITE, for the handoff."""
    pipeline = FakePipeline(profile=_complete_profile(tmp_path))

    def runner(feature_id):
        # FakePipeline.run() jumps straight to READY, which is legal from
        # everywhere else it is used but not from RECEIVED — real
        # FeatureRequest.transition() only allows RECEIVED -> UNDERSTANDING
        # (or CANCELLED/HUMAN_REQUIRED). UNDERSTANDING is also the more
        # honest thing to prove here: it is _understand()'s own real target,
        # the step that ran unconditionally in every earlier trace of this
        # bug and is the one FEAT-00030 itself never reached.
        feature = pipeline.store.get(feature_id)
        if feature is not None and feature.state is FeatureState.RECEIVED:
            feature.transition(
                FeatureState.UNDERSTANDING,
                at="2026-08-19T10:05:00+00:00",
                reason="auto-build reached the real pipeline",
            )
            pipeline.store.save(feature)
        return feature

    product = ProductVerbs(
        pipeline=pipeline,
        memory=ProductMemory(tmp_path / "memory.db"),
        runner=runner,
    )
    runtime = build_wiz(
        home=tmp_path,
        policy=AuthorityPolicy(
            grants={
                Channel.TELEGRAM: frozenset(
                    {Authority.READ, Authority.SAFE_ACTION, Authority.CODE_WRITE}
                )
            }
        ),
        journal=WizJournal(tmp_path / "journal.jsonl"),
        config=JarvisConfig(),
        store_factory=incidents,
        product=product,
    )
    runtime.pipeline = pipeline  # convenience for assertions below
    return runtime


@pytest.fixture
def buildable_door(buildable_runtime, outages):
    return OwnerDoor(
        commands=OwnerCommands(allowed_chat_ids=OWNER, outages=outages, gate=Gate()),
        intake=TelegramIntake(
            wiz=buildable_runtime.wiz,
            owner_chat_ids=[OWNER],
            journal=buildable_runtime.journal,
        ),
        allowed_chat_ids=OWNER,
        outages=outages,
    )


class TestTheClassifierNeverReachesFeatureBuild:
    """Documents the actual bug: no rule names feature.build, and any text
    containing a feature id is always won by feature.status. This is why the
    fix dispatches with an explicit capability rather than free text — this
    test pins the classifier's real behaviour so nobody "fixes" the routing
    instead and reintroduces the same silent misroute.
    """

    def test_synthetic_build_text_would_misroute_to_status(self):
        from openjarvis.wiz.intents import RuleClassifier, default_rules
        from openjarvis.wiz.product import product_intent_rules

        rules = list(default_rules()) + list(product_intent_rules())
        classifier = RuleClassifier(rules)
        assert classifier.classify_text("build FEAT-00030") == "feature.status"

    def test_no_rule_names_feature_build_at_all(self):
        from openjarvis.wiz.product import product_intent_rules

        assert all(r.capability != "feature.build" for r in product_intent_rules())


class TestTheHandoffActuallyBuilds:
    def test_a_request_leaves_received_in_the_same_message(self, buildable_door):
        reply = buildable_door.receive(chat_id=OWNER, text="Add a small badge")
        assert reply.capability == "feature.request+build"
        assert "Starting work now." in reply.text

    def test_the_pipelines_run_is_actually_called(self, buildable_door, buildable_runtime):
        buildable_door.receive(chat_id=OWNER, text="Add a small badge")
        [feature] = buildable_runtime.pipeline.store.list()
        # FakePipeline.run() moves RECEIVED -> READY; the point here is only
        # that it moved at all, proving pipeline.run() was really invoked
        # rather than misrouted to a read-only status lookup.
        assert feature.state is not FeatureState.RECEIVED

    def test_no_duplicate_feature_is_created_by_the_handoff(
        self, buildable_door, buildable_runtime
    ):
        buildable_door.receive(chat_id=OWNER, text="Add a small badge")
        assert len(buildable_runtime.pipeline.store.list()) == 1

    def test_the_grant_journaled_is_code_write_for_feature_build(
        self, buildable_door, buildable_runtime
    ):
        buildable_door.receive(chat_id=OWNER, text="Add a small badge")
        entries = buildable_runtime.journal.entries()
        grants = [
            e
            for e in entries
            if e.kind == "authority.granted" and e.capability == "feature.build"
        ]
        assert grants, "feature.build must have actually been attempted"


class TestAnAutoBuildRefusalIsJournaled:
    """Independent of root cause: if the handoff refuses or fails, that must
    survive past the one Telegram reply that carried it — see
    OwnerDoor._journal_auto_build_outcome.
    """

    def test_a_refused_build_is_journaled_with_the_feature_id(self, tmp_path, incidents):
        pipeline = FakePipeline(profile=_complete_profile(tmp_path))
        product = ProductVerbs(
            pipeline=pipeline,
            memory=ProductMemory(tmp_path / "memory.db"),
            runner=lambda feature_id: pipeline.run(feature_id),
        )
        # No CODE_WRITE granted: feature.build must be refused.
        runtime = build_wiz(
            home=tmp_path,
            policy=AuthorityPolicy(
                grants={Channel.TELEGRAM: frozenset({Authority.READ, Authority.SAFE_ACTION})}
            ),
            journal=WizJournal(tmp_path / "journal.jsonl"),
            config=JarvisConfig(),
            store_factory=incidents,
            product=product,
        )
        door = OwnerDoor(
            intake=TelegramIntake(
                wiz=runtime.wiz, owner_chat_ids=[OWNER], journal=runtime.journal
            ),
            allowed_chat_ids=OWNER,
        )
        reply = door.receive(chat_id=OWNER, text="Add a small badge")
        assert reply.capability == "feature.request"  # not "+build"
        # The reply now carries a real reason, not the old empty-string
        # detail that made a silent refusal look identical to a success.
        assert reply.text.strip() != ""

        [feature] = pipeline.store.list()
        entries = [
            e
            for e in runtime.journal.entries()
            if e.kind == "feature.auto_build_refused"
        ]
        assert len(entries) == 1
        assert entries[0].detail["feature_id"] == feature.id
        assert entries[0].reason  # a real, bounded reason, not empty

    def test_the_journaled_reason_is_bounded(self, tmp_path, incidents):
        pipeline = FakePipeline(profile=_complete_profile(tmp_path))
        product = ProductVerbs(
            pipeline=pipeline,
            memory=ProductMemory(tmp_path / "memory.db"),
            runner=lambda feature_id: pipeline.run(feature_id),
        )
        runtime = build_wiz(
            home=tmp_path,
            policy=AuthorityPolicy(
                grants={Channel.TELEGRAM: frozenset({Authority.READ, Authority.SAFE_ACTION})}
            ),
            journal=WizJournal(tmp_path / "journal.jsonl"),
            config=JarvisConfig(),
            store_factory=incidents,
            product=product,
        )
        door = OwnerDoor(
            intake=TelegramIntake(
                wiz=runtime.wiz, owner_chat_ids=[OWNER], journal=runtime.journal
            ),
            allowed_chat_ids=OWNER,
        )
        door.receive(chat_id=OWNER, text="Add a small badge")
        entries = [
            e
            for e in runtime.journal.entries()
            if e.kind == "feature.auto_build_refused"
        ]
        assert len(entries[0].reason) <= 500

    def test_an_exploding_dispatch_is_journaled_as_failed_not_refused(
        self, tmp_path, incidents
    ):
        pipeline = FakePipeline(profile=_complete_profile(tmp_path))
        product = ProductVerbs(
            pipeline=pipeline,
            memory=ProductMemory(tmp_path / "memory.db"),
            runner=lambda feature_id: pipeline.run(feature_id),
        )
        runtime = build_wiz(
            home=tmp_path,
            policy=AuthorityPolicy(
                grants={
                    Channel.TELEGRAM: frozenset(
                        {Authority.READ, Authority.SAFE_ACTION, Authority.CODE_WRITE}
                    )
                }
            ),
            journal=WizJournal(tmp_path / "journal.jsonl"),
            config=JarvisConfig(),
            store_factory=incidents,
            product=product,
        )

        class ExplodingWiz:
            def handle(self, request, *, capability=None):
                raise RuntimeError("boom")

        intake = TelegramIntake(
            wiz=ExplodingWiz(), owner_chat_ids=[OWNER], journal=runtime.journal
        )
        # feature.request still needs the real wiz to record the feature, so
        # build it for real first, then swap wiz out from under the door for
        # the auto-build call only.
        real_intake = TelegramIntake(
            wiz=runtime.wiz, owner_chat_ids=[OWNER], journal=runtime.journal
        )
        door = OwnerDoor(intake=real_intake, allowed_chat_ids=OWNER)
        door.intake = intake  # now .receive() would fail too; call the method directly

        build_result = door._auto_build_feature("FEAT-99999", OWNER, OWNER)
        assert build_result["started"] is False

        entries = [
            e for e in runtime.journal.entries() if e.kind == "feature.auto_build_failed"
        ]
        assert len(entries) == 1
        assert "boom" in entries[0].reason
        assert entries[0].detail["feature_id"] == "FEAT-99999"


# ---------------------------------------------------------------------------
# Acknowledgement ordering: the "I'll work on it" reply must not wait for the
# (possibly minutes-long) auto-build to finish. Found on FEAT-00031/32, where
# a mid-run "I need your help" — sent straight to the notifier for a real
# pipeline outcome — could arrive on the owner's phone before the initial
# acknowledgement, because the acknowledgement was only ever sent as part of
# one combined reply built *after* the blocking auto-build call returned.
# ---------------------------------------------------------------------------


def _runtime_with_recording_runner(tmp_path, incidents, *, events):
    """Same shape as the buildable_runtime fixture, but the runner records
    into *events* before doing its (here: instant, but stands in for
    pipeline.run()) work, so tests can see whether send() ran first."""
    pipeline = FakePipeline(profile=_complete_profile(tmp_path))

    def runner(feature_id):
        events.append("runner")
        feature = pipeline.store.get(feature_id)
        if feature is not None and feature.state is FeatureState.RECEIVED:
            feature.transition(
                FeatureState.UNDERSTANDING,
                at="2026-08-19T10:05:00+00:00",
                reason="auto-build reached the real pipeline",
            )
            pipeline.store.save(feature)
        return feature

    product = ProductVerbs(
        pipeline=pipeline,
        memory=ProductMemory(tmp_path / "memory.db"),
        runner=runner,
    )
    runtime = build_wiz(
        home=tmp_path,
        policy=AuthorityPolicy(
            grants={
                Channel.TELEGRAM: frozenset(
                    {Authority.READ, Authority.SAFE_ACTION, Authority.CODE_WRITE}
                )
            }
        ),
        journal=WizJournal(tmp_path / "journal.jsonl"),
        config=JarvisConfig(),
        store_factory=incidents,
        product=product,
    )
    runtime.pipeline = pipeline
    return runtime


class TestAcknowledgementIsSentBeforeAutoBuildBlocks:
    def test_send_is_called_before_the_blocking_runner(self, tmp_path, incidents, outages):
        events = []
        runtime = _runtime_with_recording_runner(tmp_path, incidents, events=events)
        door = OwnerDoor(
            intake=TelegramIntake(
                wiz=runtime.wiz, owner_chat_ids=[OWNER], journal=runtime.journal
            ),
            allowed_chat_ids=OWNER,
            outages=outages,
        )

        def send(text):
            events.append(("send", text))

        door.receive(chat_id=OWNER, text="Add a small badge", send=send)

        kinds = [e[0] if isinstance(e, tuple) else e for e in events]
        assert kinds == ["send", "runner"]

    def test_the_acknowledgement_names_the_feature_id(self, tmp_path, incidents, outages):
        events = []
        runtime = _runtime_with_recording_runner(tmp_path, incidents, events=events)
        door = OwnerDoor(
            intake=TelegramIntake(
                wiz=runtime.wiz, owner_chat_ids=[OWNER], journal=runtime.journal
            ),
            allowed_chat_ids=OWNER,
            outages=outages,
        )
        sent = []
        door.receive(chat_id=OWNER, text="Add a small badge", send=sent.append)

        [feature] = runtime.pipeline.store.list()
        assert len(sent) == 1
        assert feature.id in sent[0]

    def test_no_duplicate_feature_is_created_via_the_send_path(
        self, tmp_path, incidents, outages
    ):
        events = []
        runtime = _runtime_with_recording_runner(tmp_path, incidents, events=events)
        door = OwnerDoor(
            intake=TelegramIntake(
                wiz=runtime.wiz, owner_chat_ids=[OWNER], journal=runtime.journal
            ),
            allowed_chat_ids=OWNER,
            outages=outages,
        )
        door.receive(chat_id=OWNER, text="Add a small badge", send=lambda text: None)
        assert len(runtime.pipeline.store.list()) == 1

    def test_a_started_build_does_not_send_a_stale_second_ack(
        self, tmp_path, incidents, outages
    ):
        # The real, eventual outcome reaches the owner through owner_notifier
        # once pipeline.run() actually has one; _dispatch() itself has
        # nothing further worth saying once the ack already went out.
        events = []
        runtime = _runtime_with_recording_runner(tmp_path, incidents, events=events)
        door = OwnerDoor(
            intake=TelegramIntake(
                wiz=runtime.wiz, owner_chat_ids=[OWNER], journal=runtime.journal
            ),
            allowed_chat_ids=OWNER,
            outages=outages,
        )
        sent = []
        reply = door.receive(chat_id=OWNER, text="Add a small badge", send=sent.append)
        assert len(sent) == 1
        assert reply.text == ""

    def test_a_refusal_to_start_still_reaches_the_owner_as_a_second_message(
        self, tmp_path, incidents, outages
    ):
        # No CODE_WRITE granted -> feature.build is refused. Nothing else
        # (owner_notifier only fires for NEEDS_OWNER_KINDS journal entries,
        # and a refusal-to-start is not one of them) tells the owner this
        # happened, so it must ride the returned reply.
        pipeline = FakePipeline(profile=_complete_profile(tmp_path))
        product = ProductVerbs(
            pipeline=pipeline,
            memory=ProductMemory(tmp_path / "memory.db"),
            runner=lambda feature_id: pipeline.run(feature_id),
        )
        runtime = build_wiz(
            home=tmp_path,
            policy=AuthorityPolicy(
                grants={Channel.TELEGRAM: frozenset({Authority.READ, Authority.SAFE_ACTION})}
            ),
            journal=WizJournal(tmp_path / "journal.jsonl"),
            config=JarvisConfig(),
            store_factory=incidents,
            product=product,
        )
        door = OwnerDoor(
            intake=TelegramIntake(
                wiz=runtime.wiz, owner_chat_ids=[OWNER], journal=runtime.journal
            ),
            allowed_chat_ids=OWNER,
            outages=outages,
        )
        sent = []
        reply = door.receive(chat_id=OWNER, text="Add a small badge", send=sent.append)

        assert len(sent) == 1  # the ack, sent immediately
        assert reply.text.strip() != ""  # the refusal, sent as a follow-up
        assert reply.text != sent[0]  # two distinct messages, not a duplicate

    def test_callers_with_no_send_keep_the_single_combined_reply(
        self, buildable_door, buildable_runtime
    ):
        # jarvis wiz say, and every pre-existing test in this file, call
        # receive() with no send — behaviour there is unchanged.
        reply = buildable_door.receive(chat_id=OWNER, text="Add a small badge")
        assert reply.capability == "feature.request+build"
        assert "Starting work now." in reply.text

class TestAcknowledgementOrderingThroughTheRealTransport:
    def test_the_ack_goes_out_before_on_message_returns_and_before_any_follow_up(
        self, tmp_path, incidents, outages
    ):
        events = []
        runtime = _runtime_with_recording_runner(tmp_path, incidents, events=events)
        door = OwnerDoor(
            intake=TelegramIntake(
                wiz=runtime.wiz, owner_chat_ids=[OWNER], journal=runtime.journal
            ),
            allowed_chat_ids=OWNER,
            outages=outages,
        )

        class RecordingChannel(Channel_):
            def send(self, chat_id, text):
                events.append(("channel.send", text))
                super().send(chat_id, text)

        channel = RecordingChannel()
        listener = TelegramOwnerDoor(door=door, notifier=Transport(channel))
        listener.start()

        channel.handler(
            type(
                "M",
                (),
                {
                    "conversation_id": OWNER,
                    "sender": OWNER,
                    "content": "Add a small badge",
                    "message_id": "wire-1",
                },
            )()
        )

        kinds = [e[0] if isinstance(e, tuple) else e for e in events]
        # The channel actually sent the ack before the runner (the stand-in
        # for the blocking pipeline.run()) ever ran — not merely computed it.
        assert kinds.index("channel.send") < kinds.index("runner")
        # Exactly one message went out on the wire: the started-build case
        # has nothing further to say once the ack is sent (see above).
        assert len(channel.sent) == 1
