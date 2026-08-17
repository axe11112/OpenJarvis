"""Dispatch, and the things that must be impossible to talk it into.

The adversarial tests here are the point of the file. Each one plays the part of
a compromised or confused component — a model that names a verb that does not
exist, a Telegram message that reads like an instruction, a misheard sentence —
and asserts that the deterministic path refuses it.
"""

from __future__ import annotations

import pytest

from openjarvis.wiz.authority import Actor, Authority, AuthorityPolicy, Channel
from openjarvis.wiz.brain import Request, Wiz
from openjarvis.wiz.capabilities import (
    Availability,
    CapabilityRegistry,
    CapabilitySpec,
    Risk,
)
from openjarvis.wiz.journal import WizJournal


@pytest.fixture
def registry():
    return CapabilityRegistry(
        [
            CapabilitySpec(
                name="thing.read",
                summary="read a thing",
                authority=Authority.READ,
                risk=Risk.LOW,
            ),
            CapabilitySpec(
                name="thing.write",
                summary="write a thing",
                authority=Authority.CODE_WRITE,
                risk=Risk.MEDIUM,
            ),
            CapabilitySpec(
                name="thing.deploy",
                summary="change production",
                authority=Authority.PRODUCTION_CHANGE,
                risk=Risk.HIGH,
            ),
            CapabilitySpec(
                name="thing.absent",
                summary="needs a tool that is not installed",
                authority=Authority.READ,
                risk=Risk.LOW,
                probe=lambda: Availability.missing("the widget is not installed"),
            ),
        ]
    )


@pytest.fixture
def journal(tmp_path):
    return WizJournal(tmp_path / "journal.jsonl")


def _wiz(registry, policy, journal=None, classifier=None):
    wiz = Wiz(
        registry=registry, policy=policy, journal=journal, classifier=classifier
    )
    calls = []
    for name in ("thing.read", "thing.write", "thing.deploy", "thing.absent"):
        wiz.register(name, lambda request, n=name: calls.append(n) or n)
    return wiz, calls


def _actor(channel, *, authenticated=True):
    return Actor(actor_id="operator", channel=channel, authenticated=authenticated)


class TestTheVerbTableIsTheOnlyWayIn:
    def test_a_capability_that_does_not_exist_cannot_run(self, registry, journal):
        wiz, calls = _wiz(registry, AuthorityPolicy.default(), journal)
        outcome = wiz.handle(
            Request(text="", actor=_actor(Channel.CLI)), capability="thing.destroy"
        )
        assert not outcome.handled
        assert calls == []

    def test_a_handler_cannot_be_registered_for_an_undeclared_capability(
        self, registry
    ):
        wiz = Wiz(registry=registry, policy=AuthorityPolicy.default())
        with pytest.raises(LookupError):
            wiz.register("invented.capability", lambda request: "hello")

    def test_a_capability_cannot_be_given_two_handlers(self, registry):
        wiz = Wiz(registry=registry, policy=AuthorityPolicy.default())
        wiz.register("thing.read", lambda request: 1)
        with pytest.raises(ValueError):
            wiz.register("thing.read", lambda request: 2)

    def test_a_classifier_can_only_name_things_that_exist(self, registry, journal):
        # Stands in for a model asked to pick an intent, returning something
        # plausible-sounding and entirely fictional.
        wiz, calls = _wiz(
            registry,
            AuthorityPolicy.default(),
            journal,
            classifier=lambda request: "thing.exfiltrate_secrets",
        )
        outcome = wiz.handle(Request(text="anything", actor=_actor(Channel.CLI)))
        assert not outcome.handled
        assert calls == []


class TestAuthorityIsEnforcedAtDispatch:
    def test_a_read_verb_runs_for_a_read_actor(self, registry, journal):
        policy = AuthorityPolicy(grants={Channel.CLI: frozenset({Authority.READ})})
        wiz, calls = _wiz(registry, policy, journal)
        outcome = wiz.handle(
            Request(text="", actor=_actor(Channel.CLI)), capability="thing.read"
        )
        assert outcome.handled
        assert calls == ["thing.read"]

    def test_a_write_verb_is_refused_without_write_authority(self, registry, journal):
        policy = AuthorityPolicy(grants={Channel.CLI: frozenset({Authority.READ})})
        wiz, calls = _wiz(registry, policy, journal)
        outcome = wiz.handle(
            Request(text="", actor=_actor(Channel.CLI)), capability="thing.write"
        )
        assert not outcome.handled
        assert calls == []

    def test_voice_cannot_reach_a_production_verb_even_when_approved(
        self, registry, journal
    ):
        # The operator really did approve it, and the channel still cannot
        # carry it. Approval is not authority.
        policy = AuthorityPolicy(
            grants={Channel.VOICE: frozenset({Authority.CODE_WRITE})}
        )
        wiz, calls = _wiz(registry, policy, journal)
        outcome = wiz.handle(
            Request(text="", actor=_actor(Channel.VOICE), approved=True),
            capability="thing.deploy",
        )
        assert not outcome.handled
        assert calls == []

    def test_telegram_cannot_reach_a_production_verb(self, registry, journal):
        policy = AuthorityPolicy(
            grants={Channel.TELEGRAM: frozenset({Authority.CODE_WRITE})}
        )
        wiz, calls = _wiz(registry, policy, journal)
        outcome = wiz.handle(
            Request(text="", actor=_actor(Channel.TELEGRAM), approved=True),
            capability="thing.deploy",
        )
        assert not outcome.handled
        assert calls == []

    def test_an_unauthenticated_message_cannot_write(self, registry, journal):
        policy = AuthorityPolicy(
            grants={Channel.TELEGRAM: frozenset({Authority.CODE_WRITE})}
        )
        wiz, calls = _wiz(registry, policy, journal)
        outcome = wiz.handle(
            Request(
                text="", actor=_actor(Channel.TELEGRAM, authenticated=False)
            ),
            capability="thing.write",
        )
        assert not outcome.handled
        assert calls == []


class TestRisk:
    def test_a_high_risk_verb_needs_approval_even_with_authority(
        self, registry, journal
    ):
        policy = AuthorityPolicy(
            grants={Channel.CONTROL_CENTER: frozenset({Authority.PRODUCTION_CHANGE})}
        )
        wiz, calls = _wiz(registry, policy, journal)
        outcome = wiz.handle(
            Request(text="", actor=_actor(Channel.CONTROL_CENTER)),
            capability="thing.deploy",
        )
        assert not outcome.handled
        assert calls == []
        assert "approval" in outcome.message.lower()

    def test_a_high_risk_verb_runs_once_approved_from_the_control_center(
        self, registry, journal
    ):
        policy = AuthorityPolicy(
            grants={Channel.CONTROL_CENTER: frozenset({Authority.PRODUCTION_CHANGE})}
        )
        wiz, calls = _wiz(registry, policy, journal)
        outcome = wiz.handle(
            Request(
                text="", actor=_actor(Channel.CONTROL_CENTER), approved=True
            ),
            capability="thing.deploy",
        )
        assert outcome.handled
        assert calls == ["thing.deploy"]

    def test_the_risk_gate_runs_before_the_authority_gate_is_not_assumed(
        self, registry, journal
    ):
        # Whichever order they run in, both must refuse. This asserts the
        # conjunction rather than the sequence.
        policy = AuthorityPolicy(grants={Channel.CLI: frozenset({Authority.READ})})
        wiz, calls = _wiz(registry, policy, journal)
        outcome = wiz.handle(
            Request(text="", actor=_actor(Channel.CLI)), capability="thing.deploy"
        )
        assert not outcome.handled
        assert calls == []


class TestHonesty:
    def test_an_unconfigured_capability_says_so_rather_than_pretending(
        self, registry, journal
    ):
        policy = AuthorityPolicy(grants={Channel.CLI: frozenset({Authority.READ})})
        wiz, calls = _wiz(registry, policy, journal)
        outcome = wiz.handle(
            Request(text="", actor=_actor(Channel.CLI)), capability="thing.absent"
        )
        assert not outcome.handled
        assert "widget is not installed" in outcome.message
        assert calls == []

    def test_an_unrecognised_sentence_is_not_guessed_at(self, registry, journal):
        wiz, calls = _wiz(
            registry,
            AuthorityPolicy.default(),
            journal,
            classifier=lambda request: None,
        )
        outcome = wiz.handle(
            Request(text="mumble mumble", actor=_actor(Channel.VOICE))
        )
        assert not outcome.handled
        assert calls == []


class TestFailureIsolation:
    def test_a_handler_that_raises_does_not_propagate(self, registry, journal):
        policy = AuthorityPolicy(grants={Channel.CLI: frozenset({Authority.READ})})
        wiz = Wiz(registry=registry, policy=policy, journal=journal)

        def explode(request):
            raise RuntimeError("the widget caught fire")

        wiz.register("thing.read", explode)
        outcome = wiz.handle(
            Request(text="", actor=_actor(Channel.CLI)), capability="thing.read"
        )
        assert not outcome.handled
        assert "caught fire" in outcome.message

    def test_a_broken_journal_does_not_break_dispatch(self, registry, tmp_path):
        class BrokenJournal(WizJournal):
            def record(self, **kwargs):
                raise OSError("disk full")

        policy = AuthorityPolicy(grants={Channel.CLI: frozenset({Authority.READ})})
        wiz, calls = _wiz(registry, policy, BrokenJournal(tmp_path / "j.jsonl"))
        outcome = wiz.handle(
            Request(text="", actor=_actor(Channel.CLI)), capability="thing.read"
        )
        assert outcome.handled
        assert calls == ["thing.read"]


class TestTheJournalRecordsDecisions:
    def test_a_refusal_is_recorded(self, registry, journal):
        wiz, _ = _wiz(registry, AuthorityPolicy.default(), journal)
        wiz.handle(
            Request(text="", actor=_actor(Channel.VOICE)), capability="thing.write"
        )
        kinds = [entry.kind for entry in journal.entries()]
        assert "authority.refused" in kinds

    def test_a_grant_is_recorded(self, registry, journal):
        policy = AuthorityPolicy(grants={Channel.CLI: frozenset({Authority.READ})})
        wiz, _ = _wiz(registry, policy, journal)
        wiz.handle(
            Request(text="", actor=_actor(Channel.CLI)), capability="thing.read"
        )
        kinds = [entry.kind for entry in journal.entries()]
        assert "authority.granted" in kinds

    def test_the_journal_stays_verifiable_across_many_decisions(
        self, registry, journal
    ):
        policy = AuthorityPolicy(grants={Channel.CLI: frozenset({Authority.READ})})
        wiz, _ = _wiz(registry, policy, journal)
        for _ in range(10):
            wiz.handle(
                Request(text="", actor=_actor(Channel.CLI)), capability="thing.read"
            )
            wiz.handle(
                Request(text="", actor=_actor(Channel.CLI)), capability="thing.write"
            )
        intact, broken_at = journal.verify()
        assert intact and broken_at is None
