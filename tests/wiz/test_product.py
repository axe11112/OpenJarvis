"""Universal intake: one pipeline, whatever channel the request arrived on."""

from __future__ import annotations

import pytest

from openjarvis.wiz.authority import Actor, Authority, AuthorityPolicy, Channel
from openjarvis.wiz.brain import Request
from openjarvis.wiz.features.model import FeatureRequest, FeatureState, Priority
from openjarvis.wiz.journal import WizJournal
from openjarvis.wiz.memory import ProductMemory
from openjarvis.wiz.product import ProductVerbs, product_intent_rules
from openjarvis.wiz.runtime import build_wiz


class FakeStore:
    def __init__(self):
        self.features = {}
        self._n = 0

    def next_id(self):
        self._n += 1
        return f"FEAT-{self._n:05d}"

    def create(self, feature):
        self.features[feature.id] = feature
        return feature

    def save(self, feature):
        self.features[feature.id] = feature
        return feature

    def get(self, feature_id):
        return self.features.get(feature_id)

    def list(self, *, states=None, limit=50):
        found = list(self.features.values())
        if states is not None:
            wanted = {FeatureState.parse(s) for s in states}
            found = [f for f in found if f.state in wanted]
        return found[:limit]

    def active(self, limit=50):
        return [f for f in self.features.values() if not f.terminal][:limit]


class FakePipeline:
    """Records submissions. Runs nothing; running is a separate verb."""

    def __init__(self, profile=None):
        self.store = FakeStore()
        self.submitted = []
        self.profile = profile

    def submit(self, text, *, actor, title="", priority=Priority.P3, target=""):
        feature = FeatureRequest(
            id=self.store.next_id(),
            title=title or text[:60],
            operator_request=text,
            source=actor.channel.value,
            actor_id=actor.actor_id,
            priority=priority,
            state=FeatureState.RECEIVED,
            created_at="2026-08-19T10:00:00+00:00",
            updated_at="2026-08-19T10:00:00+00:00",
        )
        self.store.create(feature)
        self.submitted.append((text, actor.channel))
        return feature

    def cancel(self, feature_id, *, reason=""):
        """Matches ``FeaturePipeline.cancel``'s contract, for the routes tests."""
        feature = self.store.get(feature_id)
        if feature is None:
            raise KeyError(feature_id)
        if not feature.terminal:
            feature.transition(
                FeatureState.CANCELLED,
                at="2026-08-19T10:05:00+00:00",
                reason=reason or "cancelled",
            )
            self.store.save(feature)
        return feature

    def run(self, feature_id, *, max_steps=30):
        """Matches ``FeaturePipeline.run``'s contract, for the routes tests.

        Stops at READY, same as the real pipeline — this fake never merges
        anything on its own either.
        """
        feature = self.store.get(feature_id)
        if feature is None:
            raise KeyError(feature_id)
        if feature.state is FeatureState.RECEIVED:
            feature.transition(
                FeatureState.READY, at="2026-08-19T10:05:00+00:00", reason="built"
            )
            self.store.save(feature)
        return feature

    def auto_ship_if_eligible(
        self,
        feature_id,
        *,
        emergency_stop_engaged=lambda: False,
        reliability_busy=lambda: False,
        audit_healthy=lambda: True,
    ):
        """Matches ``FeaturePipeline.auto_ship_if_eligible``'s contract.

        Deliberately re-checks the same narrow conditions the real method
        does — LOW risk, and none of the cross-cutting blockers — so a route
        test proving "a MEDIUM-risk feature never gets shipped automatically"
        is proving something about the wiring, not just about this fake.
        """
        feature = self.store.get(feature_id)
        if feature is None:
            raise KeyError(feature_id)
        if feature.state is not FeatureState.READY:
            return feature
        if (feature.risk or "").strip().upper() != "LOW":
            return feature
        if emergency_stop_engaged() or reliability_busy() or not audit_healthy():
            return feature
        return self.ship(feature_id)

    def ship(self, feature_id, *, operator_approved=False):
        """Matches ``FeaturePipeline.ship``'s contract, for the routes tests."""
        self.shipped = getattr(self, "shipped", [])
        self.shipped.append((feature_id, operator_approved))
        feature = self.store.get(feature_id)
        if feature is None:
            raise KeyError(feature_id)
        if feature.state is FeatureState.READY:
            for target in (
                FeatureState.MERGING,
                FeatureState.DEPLOYING,
                FeatureState.PRODUCTION_VERIFYING,
                FeatureState.COMPLETE,
            ):
                feature.transition(
                    target, at="2026-08-19T10:05:00+00:00", reason="shipped"
                )
            self.store.save(feature)
        return feature


@pytest.fixture
def product(tmp_path):
    return ProductVerbs(
        pipeline=FakePipeline(),
        memory=ProductMemory(tmp_path / "memory.db"),
    )


def request(text, channel=Channel.CONTROL_CENTER, **arguments):
    return Request(
        text=text,
        actor=Actor(actor_id="operator", channel=channel, authenticated=True),
        arguments=arguments,
    )


class TestOneDoorForEveryChannel:
    @pytest.mark.parametrize(
        "channel",
        [Channel.CONTROL_CENTER, Channel.CLI, Channel.TELEGRAM, Channel.VOICE],
    )
    def test_every_channel_reaches_the_same_pipeline(self, product, channel):
        result = product.request_feature(
            request("Add a download button to reports", channel)
        )
        assert result["recorded"]
        assert product.pipeline.submitted[-1][1] is channel

    def test_a_recorded_request_is_answered_in_plain_english(self, product):
        result = product.request_feature(request("Add a download button"))
        assert result["say"].startswith("Sir, I'll work on it")
        assert result["id"] in result["say"]

    def test_a_request_with_nothing_in_it_is_admitted_not_recorded(self, product):
        result = product.request_feature(request("please"))
        assert not result["recorded"]
        assert "did not catch" in result["detail"]

    def test_chat_pleasantries_do_not_become_part_of_the_brief(self, product):
        # The recorded text becomes the goal Claude is given. "Sir, could you
        # please add a download button" is a worse brief than the requirement.
        product.request_feature(
            request("Sir, could you please add a download button to reports")
        )
        recorded, _ = product.pipeline.submitted[-1]
        assert recorded.lower().startswith("add a download button")

    def test_recording_a_request_starts_no_coding_session(self, product):
        # "I would like X" and "go and build X" are not the same sentence.
        product.request_feature(request("Add a download button"))
        feature = product.pipeline.store.list()[0]
        assert feature.state is FeatureState.RECEIVED
        assert feature.attempts == []


class TestClassification:
    @pytest.mark.parametrize(
        "text",
        [
            "Build a coach weekly summary",
            "Add a download button",
            "Make onboarding easier",
            "Can you build a comparison between two swimmers",
            "I'd like a better mobile dashboard",
            # The brief's own examples. An anchor demanding the verb first
            # classified these as unrecognised, which is how a channel ends up
            # feeling broken while every test passes.
            "Sir, add export to reports",
            "Wiz, build a coach weekly summary",
            "please add a download button",
            "We need a comparison between two swimmers",
        ],
    )
    def test_a_request_to_build_is_recognised(self, text):
        from openjarvis.wiz.intents import RuleClassifier, default_rules

        classifier = RuleClassifier(default_rules() + product_intent_rules())
        assert classifier.classify_text(text) == "feature.request"

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("what are you building", "feature.list"),
            ("what did we build yesterday", "product.recent"),
            ("why did we change onboarding", "product.search"),
            ("search for the coach dashboard", "product.search"),
            ("is the site up", "reliability.status"),
            ("what can you do", "wiz.capabilities"),
        ],
    )
    def test_the_other_verbs_are_still_reachable(self, text, expected):
        from openjarvis.wiz.intents import RuleClassifier, default_rules

        classifier = RuleClassifier(default_rules() + product_intent_rules())
        assert classifier.classify_text(text) == expected

    def test_nonsense_still_names_nothing(self):
        from openjarvis.wiz.intents import RuleClassifier, default_rules

        classifier = RuleClassifier(default_rules() + product_intent_rules())
        assert classifier.classify_text("the quick brown fox") is None


class TestAuthoritySplit:
    def test_asking_for_something_and_building_it_are_different_authorities(
        self, tmp_path, product
    ):
        runtime = build_wiz(
            home=tmp_path,
            # SAFE_ACTION only: may record a wish, may not have code written.
            policy=AuthorityPolicy(
                grants={Channel.TELEGRAM: frozenset({Authority.SAFE_ACTION})}
            ),
            journal=WizJournal(tmp_path / "j.jsonl"),
            store_factory=lambda: (_ for _ in ()).throw(FileNotFoundError("none")),
            product=product,
        )
        actor = Actor(actor_id="owner", channel=Channel.TELEGRAM, authenticated=True)

        recorded = runtime.wiz.handle(
            Request(text="Add a download button", actor=actor),
            capability="feature.request",
        )
        assert recorded.handled

        built = runtime.wiz.handle(
            Request(text="", actor=actor, arguments={"feature_id": "FEAT-00001"}),
            capability="feature.build",
        )
        assert not built.handled

    def test_an_unauthenticated_sender_cannot_even_record_a_request(
        self, tmp_path, product
    ):
        # An arbitrary inbound message is not the operator, whatever it claims.
        runtime = build_wiz(
            home=tmp_path,
            policy=AuthorityPolicy(
                grants={Channel.TELEGRAM: frozenset({Authority.CODE_WRITE})}
            ),
            journal=WizJournal(tmp_path / "j.jsonl"),
            store_factory=lambda: (_ for _ in ()).throw(FileNotFoundError("none")),
            product=product,
        )
        stranger = Actor(
            actor_id="someone", channel=Channel.TELEGRAM, authenticated=False
        )
        outcome = runtime.wiz.handle(
            Request(text="Add a download button", actor=stranger),
            capability="feature.request",
        )
        assert not outcome.handled

    def test_voice_can_ask_for_a_feature(self, tmp_path, product):
        # §26: "Add a better coach dashboard" spoken should create a request.
        runtime = build_wiz(
            home=tmp_path,
            policy=AuthorityPolicy(
                grants={Channel.VOICE: frozenset({Authority.SAFE_ACTION})}
            ),
            journal=WizJournal(tmp_path / "j.jsonl"),
            store_factory=lambda: (_ for _ in ()).throw(FileNotFoundError("none")),
            product=product,
        )
        outcome = runtime.wiz.handle(
            Request(
                text="Add a better coach dashboard",
                actor=Actor(
                    actor_id="operator", channel=Channel.VOICE, authenticated=True
                ),
            )
        )
        assert outcome.handled
        assert outcome.result["recorded"]

    def test_voice_can_never_reach_a_production_change(self, tmp_path, product):
        # The ceiling is in source. Configuration cannot raise it.
        from openjarvis.wiz.authority import CHANNEL_CEILING

        assert Authority.PRODUCTION_CHANGE not in CHANNEL_CEILING[Channel.VOICE]
        assert Authority.PRODUCTION_CHANGE not in CHANNEL_CEILING[Channel.TELEGRAM]


class TestHonestyAboutWhatIsConfigured:
    def test_without_a_product_side_the_feature_verbs_are_declared_unavailable(
        self, tmp_path
    ):
        """Understood, and refused for the real reason.

        The verbs used to be left out of the registry entirely on a machine
        with no engineering target, which made "add a download button" come
        back as "I did not recognise that as something I know how to do". Wiz
        understood it perfectly; the missing thing was a configured target, and
        that is what the refusal has to say — otherwise the operator goes
        looking for a better sentence instead of a settings file.
        """
        runtime = build_wiz(
            home=tmp_path,
            policy=AuthorityPolicy(grants={Channel.CLI: frozenset({Authority.READ})}),
            journal=WizJournal(tmp_path / "j.jsonl"),
            store_factory=lambda: (_ for _ in ()).throw(FileNotFoundError("none")),
        )
        assert "feature.request" in runtime.registry.names()
        assert not runtime.registry.get("feature.request").availability().configured

        outcome = runtime.wiz.handle(
            Request(
                text="Add a download button",
                actor=Actor(actor_id="o", channel=Channel.CLI, authenticated=True),
            )
        )
        assert not outcome.handled
        assert outcome.capability == "feature.request"
        assert "no engineering target is configured" in outcome.message

    def test_a_target_with_no_test_command_cannot_build(self, tmp_path, product):
        from openjarvis.wiz.features.profile import EngineeringProfile
        from openjarvis.wiz.runtime import _product_available

        product.pipeline.profile = EngineeringProfile(name="wize", checkout="/tmp/x")
        available = _product_available(product)
        assert not available.configured
        assert "no test command" in available.detail

    def test_a_missing_claude_cli_is_named_as_the_reason(self, tmp_path, product):
        from openjarvis.wiz.features.profile import EngineeringProfile
        from openjarvis.wiz.runtime import _product_available

        class NoCli:
            def available(self):
                return False

        product.pipeline.profile = EngineeringProfile(
            name="wize", checkout="/tmp/x", test_command="npm test"
        )
        product.pipeline.engineer = NoCli()
        available = _product_available(product)
        assert not available.configured
        assert "claude" in available.detail


class TestReading:
    def test_the_queue_can_be_listed(self, product):
        product.request_feature(request("Add a download button"))
        listed = product.list_features(request("what are you building"))
        assert listed["available"]
        assert len(listed["building"]) == 1

    def test_a_single_request_can_be_shown_in_full(self, product):
        recorded = product.request_feature(request("Add a download button"))
        shown = product.feature_status(request("", feature_id=recorded["id"]))
        assert shown["available"]
        assert shown["feature"]["operator_request"] == "Add a download button"

    def test_asking_about_a_request_that_does_not_exist_says_so(self, product):
        shown = product.feature_status(request("", feature_id="FEAT-99999"))
        assert not shown["available"]
        assert "FEAT-99999" in shown["detail"]

    def test_what_was_built_recently_is_answerable(self, product):
        product.request_feature(request("Add a download button"))
        recent = product.recent(request("what did we build"))
        assert recent["available"]
        assert "download button" in recent["say"].lower()

    def test_search_finds_a_recorded_request(self, product):
        product.request_feature(request("Add a coach weekly summary"))
        found = product.search(request("search for coach summary"))
        assert found["entries"]
        assert "coach" in found["say"].lower()

    def test_search_strips_the_lead_in_before_searching(self, product):
        product.request_feature(request("Add a coach weekly summary"))
        found = product.search(request("find the coach summary"))
        assert found["query"] == "the coach summary"
