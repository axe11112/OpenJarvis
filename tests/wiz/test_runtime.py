"""The assembled Wiz: what it declares, and what it refuses to claim."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openjarvis.wiz.authority import Actor, Authority, AuthorityPolicy, Channel
from openjarvis.wiz.brain import Request
from openjarvis.wiz.capabilities import Risk
from openjarvis.wiz.journal import WizJournal
from openjarvis.wiz.runtime import (
    _feature_owner_notifier,
    build_wiz,
    default_capabilities,
    operator,
)


class _FakeIncident:
    def __init__(self, incident_id, state, severity="HIGH"):
        self.id = incident_id
        self.title = "Login form renders failed"
        self.component = "authentication"
        self.state = state
        self.severity = severity


class _FakeStore:
    def __init__(self, incidents):
        self._incidents = incidents
        self.closed = False

    def list(self, limit=100, **kwargs):
        return self._incidents[:limit]

    def close(self):
        self.closed = True


@pytest.fixture
def runtime(tmp_path):
    incidents = [
        _FakeIncident("INC-00018", "RESOLVED"),
        _FakeIncident("INC-00019", "REPAIRING"),
    ]
    return build_wiz(
        home=tmp_path,
        policy=AuthorityPolicy(
            grants={Channel.CONTROL_CENTER: frozenset({Authority.READ})}
        ),
        journal=WizJournal(tmp_path / "journal.jsonl"),
        store_factory=lambda: _FakeStore(incidents),
    )


class TestPhaseAIsReadOnly:
    def test_every_declared_capability_needs_only_read(self):
        for spec in default_capabilities():
            assert spec.authority is Authority.READ, spec.name

    def test_every_declared_capability_is_low_risk(self):
        for spec in default_capabilities():
            assert spec.risk is Risk.LOW, spec.name

    def test_every_declared_capability_has_a_handler(self, runtime):
        assert set(runtime.wiz.verbs()) == {s.name for s in default_capabilities()}


class TestHonesty:
    def test_an_unconfigured_reliability_store_is_admitted_not_faked(self, tmp_path):
        # There is no incident database. Wiz must say so rather than answer
        # confidently about a system it cannot see.
        #
        # The factory is explicit rather than left as ``None``: ``None`` means
        # "build the real one", which on the operator's own machine would have
        # this test reading their live incident database and passing for the
        # wrong reason.
        def no_database():
            raise FileNotFoundError("no incident database")

        runtime = build_wiz(
            home=tmp_path,
            policy=AuthorityPolicy(grants={Channel.CLI: frozenset({Authority.READ})}),
            journal=WizJournal(tmp_path / "j.jsonl"),
            store_factory=no_database,
        )
        outcome = runtime.wiz.handle(
            Request(text="", actor=operator(Channel.CLI)),
            capability="reliability.status",
        )
        # Refused at the capability gate, and the refusal says why. The verb is
        # never reached, so there is no path on which an empty answer could be
        # mistaken for a quiet site.
        assert not outcome.handled
        assert "no incident database" in outcome.message

    def test_the_probe_and_the_handler_consult_the_same_store(self, tmp_path):
        # The bug this pins: the probe read a fixed path on disk while the
        # handler used an injected factory, so on a machine that happened to
        # have a database at that path the capability reported itself available
        # and then answered from something else entirely. Availability must be
        # a statement about the store that will actually be read.
        def no_database():
            raise FileNotFoundError("no incident database")

        runtime = build_wiz(
            home=tmp_path,
            policy=AuthorityPolicy(grants={Channel.CLI: frozenset({Authority.READ})}),
            journal=WizJournal(tmp_path / "j.jsonl"),
            store_factory=no_database,
        )
        described = {s["name"]: s for s in runtime.registry.describe()}
        assert described["reliability.status"]["configured"] is False
        assert described["reliability.incidents"]["configured"] is False

    def test_a_store_that_dies_after_the_probe_still_answers_honestly(self, tmp_path):
        # The probe passing is not a promise that the next call succeeds: a
        # database can be moved between the two. The handler keeps its own
        # defence, and reports "I could not see" rather than "nothing wrong".
        opened: list[int] = []

        def flaky():
            opened.append(1)
            if len(opened) > 1:
                raise FileNotFoundError("the database went away")
            return _FakeStore([])

        runtime = build_wiz(
            home=tmp_path,
            policy=AuthorityPolicy(grants={Channel.CLI: frozenset({Authority.READ})}),
            journal=WizJournal(tmp_path / "j.jsonl"),
            store_factory=flaky,
        )
        outcome = runtime.wiz.handle(
            Request(text="", actor=operator(Channel.CLI)),
            capability="reliability.status",
        )
        assert outcome.handled
        assert outcome.result["available"] is False

    def test_capabilities_are_reported_in_two_honest_halves(self, runtime):
        outcome = runtime.wiz.handle(
            Request(text="what can you do", actor=operator(Channel.CONTROL_CENTER))
        )
        assert outcome.handled
        result = outcome.result
        assert "configured" in result and "unavailable" in result
        names = {c["name"] for c in result["configured"]} | {
            c["name"] for c in result["unavailable"]
        }
        # Every read verb, plus the product verbs — which are declared even
        # without an engineering target, and reported in the "unavailable"
        # half when there is none. A capability the operator expected and does
        # not see is a puzzle; one listed as unavailable is an answer.
        assert names >= {s.name for s in default_capabilities()}
        assert "feature.request" in names
        unavailable = {c["name"] for c in result["unavailable"]}
        assert "feature.request" in unavailable


class TestSelfDiagnosis:
    def test_wiz_health_is_separate_from_site_health(self, runtime):
        outcome = runtime.wiz.handle(
            Request(text="how are you", actor=operator(Channel.CONTROL_CENTER))
        )
        assert outcome.handled
        # Wiz's own health says nothing about incidents; that is the point.
        assert "capabilities_declared" in outcome.result
        assert "open" not in outcome.result

    def test_health_reports_journal_integrity(self, runtime):
        outcome = runtime.wiz.handle(
            Request(text="how are you", actor=operator(Channel.CONTROL_CENTER))
        )
        assert outcome.result["journal"]["intact"] is True


class TestReliabilityReadPath:
    def test_status_reports_only_unresolved_incidents(self, runtime):
        outcome = runtime.wiz.handle(
            Request(text="is the site up", actor=operator(Channel.CONTROL_CENTER))
        )
        assert outcome.handled
        assert outcome.result["open"] == 1
        assert outcome.result["incidents"][0]["id"] == "INC-00019"

    def test_incidents_lists_everything_recent(self, runtime):
        outcome = runtime.wiz.handle(
            Request(text="any incidents", actor=operator(Channel.CONTROL_CENTER))
        )
        assert outcome.handled
        assert len(outcome.result["incidents"]) == 2

    def test_the_store_is_closed_after_use(self, tmp_path):
        stores = []

        def factory():
            store = _FakeStore([])
            stores.append(store)
            return store

        runtime = build_wiz(
            home=tmp_path,
            policy=AuthorityPolicy(grants={Channel.CLI: frozenset({Authority.READ})}),
            journal=WizJournal(tmp_path / "j.jsonl"),
            store_factory=factory,
        )
        runtime.wiz.handle(
            Request(text="", actor=operator(Channel.CLI)),
            capability="reliability.incidents",
        )
        assert stores and stores[0].closed


class TestChannelsStillApply:
    def test_an_unauthenticated_actor_can_still_only_read(self, runtime):
        anonymous = Actor(
            actor_id="unknown", channel=Channel.TELEGRAM, authenticated=False
        )
        outcome = runtime.wiz.handle(Request(text="what can you do", actor=anonymous))
        # READ is permitted unauthenticated, but the policy above grants
        # Telegram nothing at all, so this is still refused.
        assert not outcome.handled

    def test_the_default_runtime_grants_no_write_authority(self, tmp_path):
        runtime = build_wiz(home=tmp_path, journal=WizJournal(tmp_path / "j.jsonl"))
        for channel in Channel:
            for authority in (
                Authority.CODE_WRITE,
                Authority.PR_WRITE,
                Authority.PRODUCTION_CHANGE,
                Authority.SECRET_ACCESS,
            ):
                actor = Actor(actor_id="operator", channel=channel, authenticated=True)
                assert not runtime.policy.decide(actor, authority).allowed


def _config(
    *, notify_enabled=True, bot_token="tok", allowed_chat_ids="123", persona=True
):
    return SimpleNamespace(
        reliability=SimpleNamespace(
            notify=SimpleNamespace(enabled=notify_enabled, persona=persona)
        ),
        channel=SimpleNamespace(
            telegram=SimpleNamespace(
                bot_token=bot_token, allowed_chat_ids=allowed_chat_ids
            )
        ),
    )


class TestFeatureOwnerNotifierAssembly:
    """_feature_owner_notifier: absence is silence, not an error — the same
    "declared, not disowned" shape every optional collaborator here takes."""

    def test_notifications_disabled_gives_no_notifier(self, tmp_path):
        config = _config(notify_enabled=False)
        assert _feature_owner_notifier(config, tmp_path) is None

    def test_no_bot_token_anywhere_gives_no_notifier(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        config = _config(bot_token="")
        assert _feature_owner_notifier(config, tmp_path) is None

    def test_a_token_only_in_the_environment_is_still_used(self, tmp_path, monkeypatch):
        # config.channel.telegram.bot_token is legitimately empty in the real
        # deployment — the secret lives only in TELEGRAM_BOT_TOKEN, resolved
        # by TelegramChannel itself — so the same fallback has to apply here.
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-the-environment")
        config = _config(bot_token="")
        assert _feature_owner_notifier(config, tmp_path) is not None

    def test_no_allowed_chat_ids_gives_no_notifier(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        config = _config(allowed_chat_ids="")
        assert _feature_owner_notifier(config, tmp_path) is None

    def test_no_config_at_all_gives_no_notifier(self, tmp_path):
        assert _feature_owner_notifier(None, tmp_path) is None

    def test_configured_telegram_produces_a_working_notifier(self, tmp_path):
        config = _config()
        notifier = _feature_owner_notifier(config, tmp_path)
        assert notifier is not None
        assert notifier.ledger_path == tmp_path / "feature_notify_ledger.json"

    def test_build_wiz_wires_the_notifier_onto_the_pipeline(self, tmp_path):
        pipeline = SimpleNamespace(journal=None, owner_notifier=None, postship=None)
        product = SimpleNamespace(pipeline=pipeline, handlers=dict)
        runtime = build_wiz(
            home=tmp_path,
            journal=WizJournal(tmp_path / "j.jsonl"),
            product=product,
            config=_config(),
        )
        assert runtime.product is product
        assert pipeline.owner_notifier is not None
