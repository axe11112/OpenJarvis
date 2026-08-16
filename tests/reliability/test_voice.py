"""Tests for Sir Voice.

The suite is organised around the one question that matters for a voice
interface: what can a sentence spoken into a room cause to happen? Every class
below is an attempt to make something dangerous happen by saying it, and the
suite passes only when each attempt ends in a refusal, a pending confirmation,
or an honest "I can't do that".

Nothing here records audio, runs whisper, or speaks.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import (
    Incident,
    IncidentState,
    RepairAttempt,
    Severity,
)
from openjarvis.reliability.voice.answers import VoiceFacts
from openjarvis.reliability.voice.commands import (
    NEEDS_CONFIRMATION,
    CommandResult,
    VoiceCommands,
)
from openjarvis.reliability.voice.confirmations import ConfirmationStore
from openjarvis.reliability.voice.intents import INTENTS, match_intent
from openjarvis.reliability.voice.stt import WhisperTranscriber
from openjarvis.reliability.voice.trigger import CallTrigger
from openjarvis.reliability.voice.tts import MacSpeech, speakable


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def store(tmp_path):
    s = IncidentStore(tmp_path / "incidents.db")
    yield s
    s.close()


def _incident(store=None, **overrides) -> Incident:
    fields = dict(
        fingerprint="fp_x",
        severity=Severity.HIGH,
        component="authentication",
        title="Login broken",
        probe_id="auth-login",
    )
    fields.update(overrides)
    incident = Incident(**fields)
    return store.create(incident) if store is not None else incident


def _commands(store=None, **kwargs) -> VoiceCommands:
    facts = kwargs.pop("facts", None) or VoiceFacts(store=store)
    return VoiceCommands(
        facts=facts,
        confirmations=kwargs.pop("confirmations", None)
        or ConfirmationStore(clock=_Clock()),
        store=store,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Intent matching
# ---------------------------------------------------------------------------


class TestIntentMatching:
    @pytest.mark.parametrize(
        "said,expected",
        [
            ("What happened?", "what_happened"),
            ("Is production down?", "production_status"),
            ("What did you try?", "what_did_you_try"),
            ("Did you change production?", "did_you_change_production"),
            ("What's the current status?", "status"),
            ("Run the diagnostic again.", "run_diagnostic"),
            ("Stop automatic repairs.", "stop_repairs"),
            ("Restart the watcher.", "restart_watcher"),
            ("Leave this incident for me.", "hand_over"),
            ("What failed in the deployment?", "deployment_failure"),
        ],
    )
    def test_every_example_from_the_brief_is_understood(self, said, expected):
        assert match_intent(said).name == expected

    def test_an_unknown_sentence_matches_nothing(self):
        """The safe failure. A voice interface that guesses is a liability."""
        assert not match_intent("please rm minus rf the database").understood
        assert not match_intent("").understood
        assert not match_intent("hello there how are you today").understood

    def test_the_longest_phrase_wins(self):
        """ "stop everything" is the emergency stop, not merely stopping repairs."""
        assert match_intent("stop everything").name == "emergency_stop"
        assert match_intent("stop automatic repairs").name == "stop_repairs"

    def test_punctuation_and_case_do_not_matter(self):
        assert match_intent("IS PRODUCTION DOWN?!").name == "production_status"
        assert match_intent("  what happened...  ").name == "what_happened"

    def test_no_intent_can_reach_a_shell(self):
        """Nothing in the allowlist names a program, a path or an argument."""
        for intent in INTENTS:
            for phrase in intent.phrases:
                assert not any(c in phrase for c in ";|&$`><"), phrase


# ---------------------------------------------------------------------------
# The refusal that matters
# ---------------------------------------------------------------------------


class TestHighRiskRequiresTheControlCenter:
    @pytest.mark.parametrize(
        "said",
        [
            "merge it",
            "merge the pull request",
            "override the verification",
            "push to main",
            "enable production deployment",
            "enable supabase writes",
            "change the token",
            "disable boundary guard",
            "disable branch protection",
        ],
    )
    def test_voice_alone_never_suffices(self, said, store):
        commands = _commands(store)
        result = commands.handle(match_intent(said))

        assert result.speech == NEEDS_CONFIRMATION
        assert not result.executed
        assert result.confirmation_id, "the request must be parked for a human"

    def test_the_request_appears_in_the_control_center(self, store):
        confirmations = ConfirmationStore(clock=_Clock())
        commands = _commands(store, confirmations=confirmations)
        commands.handle(match_intent("merge it"))

        pending = confirmations.pending()
        assert len(pending) == 1
        assert pending[0].intent == "merge"
        assert pending[0].state == "PENDING"

    def test_saying_it_again_does_not_promote_it(self, store):
        """There is no path from repetition to authority."""
        confirmations = ConfirmationStore(clock=_Clock())
        commands = _commands(store, confirmations=confirmations)
        for _ in range(5):
            result = commands.handle(match_intent("merge it"))
            assert not result.executed
        assert all(p.state == "PENDING" for p in confirmations.pending())

    def test_approving_does_not_execute_anything(self):
        """Approval marks a decision. Something else, with a human behind it,
        acts on it — this store must never grow the ability to."""
        confirmations = ConfirmationStore(clock=_Clock())
        pending = confirmations.request(
            intent="merge", description="merge a pull request", transcript="merge it"
        )
        assert confirmations.approve(pending.id)
        assert confirmations.get(pending.id).state == "APPROVED"
        assert not hasattr(confirmations, "execute")
        assert not hasattr(confirmations, "run")

    def test_a_request_nobody_answers_expires(self):
        clock = _Clock()
        confirmations = ConfirmationStore(clock=clock, ttl_seconds=600)
        pending = confirmations.request(
            intent="merge", description="merge", transcript="merge it"
        )
        clock.advance(601)
        assert confirmations.pending() == []
        assert confirmations.get(pending.id).state == "EXPIRED"
        assert not confirmations.approve(pending.id), "an expired request is dead"


# ---------------------------------------------------------------------------
# Safe operations
# ---------------------------------------------------------------------------


class _FakeSupervisor:
    def __init__(self, tmp_path, *, restart_ok=True):
        self._flag = Path(tmp_path) / "stop.flag"
        self._restart_ok = restart_ok
        self.restarts = 0

    def stop_flag(self):
        return self._flag

    def restart(self):
        self.restarts += 1
        return (self._restart_ok, "ok" if self._restart_ok else "launchd refused")


class TestSafeOperations:
    def test_stopping_repairs_engages_the_stop_flag(self, store, tmp_path):
        supervisor = _FakeSupervisor(tmp_path)
        commands = _commands(store, supervisor=supervisor)
        result = commands.handle(match_intent("stop automatic repairs"))

        assert result.executed
        assert supervisor.stop_flag().exists()
        assert "stopped automatic repairs" in result.speech

    def test_emergency_stop_engages_the_stop_flag(self, store, tmp_path):
        supervisor = _FakeSupervisor(tmp_path)
        commands = _commands(store, supervisor=supervisor)
        assert commands.handle(match_intent("emergency stop")).executed
        assert supervisor.stop_flag().exists()

    def test_restarting_the_watcher_uses_the_supervisor(self, store, tmp_path):
        supervisor = _FakeSupervisor(tmp_path)
        commands = _commands(store, supervisor=supervisor)
        result = commands.handle(match_intent("restart the watcher"))

        assert result.executed
        assert supervisor.restarts == 1

    def test_a_failed_restart_is_reported_not_claimed(self, store, tmp_path):
        supervisor = _FakeSupervisor(tmp_path, restart_ok=False)
        commands = _commands(store, supervisor=supervisor)
        result = commands.handle(match_intent("restart the watcher"))

        assert not result.executed
        assert "couldn't restart" in result.speech

    def test_handing_over_moves_the_incident_to_the_operator(self, store):
        incident = _incident(store, state=IncidentState.DETECTED)
        commands = _commands(store)
        result = commands.handle(match_intent("leave this incident for me"))

        assert result.executed
        assert store.get(incident.id).state is IncidentState.HUMAN_REQUIRED

    def test_operations_are_unavailable_rather_than_faked(self, store):
        """With no supervisor there is no pretending it worked."""
        commands = _commands(store, supervisor=None)
        result = commands.handle(match_intent("restart the watcher"))
        assert not result.executed
        assert "can't reach the controls" in result.speech

    def test_a_crashing_operation_does_not_crash_the_call(self, store):
        class _Exploding:
            def stop_flag(self):
                raise RuntimeError("disk gone")

        commands = _commands(store, supervisor=_Exploding())
        result = commands.handle(match_intent("emergency stop"))
        assert not result.executed
        assert "didn't work" in result.speech


# ---------------------------------------------------------------------------
# Answers come from recorded state
# ---------------------------------------------------------------------------


class TestAnswers:
    def test_status_with_nothing_open(self, store):
        commands = _commands(store)
        result = commands.handle(match_intent("what's the status"))
        assert "everything is passing" in result.speech.lower()

    def test_status_names_the_thing_in_plain_english(self, store):
        _incident(store, component="authentication", state=IncidentState.FIXING)
        commands = _commands(store)
        result = commands.handle(match_intent("what's the status"))
        assert "login" in result.speech.lower()
        assert "authentication" not in result.speech.lower()

    def test_what_happened_uses_the_recorded_cause(self, store):
        incident = _incident(store)
        incident.resolution.root_cause = "the session cookie was dropped"
        store.save(incident)
        commands = _commands(store)
        result = commands.handle(match_intent("what happened"))
        assert "the session cookie was dropped" in result.speech

    def test_no_recorded_cause_means_no_invented_one(self, store):
        _incident(store)
        commands = _commands(store)
        result = commands.handle(match_intent("what happened"))
        assert "don't have a cause" in result.speech

    def test_what_did_you_try_counts_real_attempts(self, store):
        incident = _incident(store)
        store.add_attempt(
            incident, RepairAttempt(number=1, outcome="verification_failed")
        )
        store.add_attempt(
            incident, RepairAttempt(number=2, outcome="verification_failed")
        )
        commands = _commands(store)
        result = commands.handle(match_intent("what did you try"))
        assert "2 times" in result.speech

    def test_did_you_change_production_is_answered_from_state(self, store):
        _incident(store)
        commands = _commands(store, facts=VoiceFacts(store=store, merge_enabled=False))
        result = commands.handle(match_intent("did you change production"))
        assert "no" in result.speech.lower()
        assert "not allowed to change production" in result.speech

    def test_a_merged_incident_says_the_fix_is_live(self, store):
        _incident(store, state=IncidentState.MERGED)
        commands = _commands(store, facts=VoiceFacts(store=store, merge_enabled=True))
        result = commands.handle(match_intent("did you change production"))
        assert "yes" in result.speech.lower()

    def test_a_broken_store_says_so_rather_than_guessing(self):
        class _Broken:
            def list(self, **_kw):
                raise RuntimeError("database gone")

        commands = _commands(None, facts=VoiceFacts(store=_Broken()))
        result = commands.handle(match_intent("what's the status"))
        assert result.speech.startswith("Sir,")

    def test_an_unknown_request_offers_what_is_possible(self, store):
        commands = _commands(store)
        result = commands.handle(match_intent("make me a sandwich"))
        assert "didn't catch that" in result.speech
        assert not result.executed


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestAudit:
    def test_every_utterance_is_audited(self, store, tmp_path):
        seen: List[CommandResult] = []
        commands = _commands(
            store, supervisor=_FakeSupervisor(tmp_path), audit=seen.append
        )
        commands.handle(match_intent("what's the status"))
        commands.handle(match_intent("emergency stop"))
        commands.handle(match_intent("merge it"))
        commands.handle(match_intent("nonsense words"))

        assert [r.intent for r in seen] == ["status", "emergency_stop", "merge", ""]
        assert [r.executed for r in seen] == [False, True, False, False]
        assert seen[2].confirmation_id

    def test_a_failing_audit_does_not_stop_the_call(self, store):
        def _explode(_result):
            raise RuntimeError("audit sink gone")

        commands = _commands(store, audit=_explode)
        assert commands.handle(match_intent("what's the status")).speech


# ---------------------------------------------------------------------------
# Nothing spoken leaks
# ---------------------------------------------------------------------------


class TestSpokenTextIsSafe:
    def test_identifiers_are_never_read_aloud(self):
        text = speakable(
            "Sir, deployment dpl_aNDR9i1G of 093896b92f3c1aa07ff34b725a8a8e04636ec142 "
            "at https://vercel.com/x failed"
        )
        assert "dpl_" not in text
        assert "093896b" not in text
        assert "https://" not in text

    def test_a_token_is_never_read_aloud(self):
        assert "ghp_" not in speakable("the token is ghp_" + "a" * 36)

    def test_long_answers_are_cut_short(self):
        text = speakable("Sir. " + ("a very long sentence about logs. " * 40))
        assert len(text) < 500
        assert text.endswith("It's on the dashboard.")

    def test_synthesis_of_empty_text_is_silent(self):
        assert MacSpeech().synthesize("") == b""

    def test_a_machine_without_a_voice_is_silent_not_broken(self):
        """An empty ``binary`` means "find it"; a machine with nothing to find
        returns no audio rather than raising into the call."""
        speech = MacSpeech()
        speech.binary = ""
        assert not speech.available
        assert "not available" in speech.unavailable_reason()
        assert speech.synthesize("hello") == b""


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------


class TestTranscription:
    def test_a_missing_piece_is_reported_not_guessed(self):
        """Whichever half is missing, the answer is silence and a named reason —
        never a transcript. Both halves are checked because a machine with
        whisper installed and no model is the likelier misconfiguration."""
        no_binary = WhisperTranscriber(model_path="/tmp/whatever.bin")
        no_binary.binary = ""
        assert not no_binary.available
        assert "not installed" in no_binary.unavailable_reason()
        assert no_binary.transcribe(b"RIFF....") == ""

        no_model = WhisperTranscriber(binary="/bin/echo", model_path="")
        assert not no_model.available
        assert "model" in no_model.unavailable_reason()
        assert no_model.transcribe(b"RIFF....") == ""

    def test_whisper_decorations_are_stripped(self):
        assert WhisperTranscriber._clean("[BLANK_AUDIO]\n Is production down? \n") == (
            "Is production down?"
        )
        assert WhisperTranscriber._clean("(wind blowing)") == ""

    def test_a_failed_run_transcribes_to_nothing(self, tmp_path):
        model = tmp_path / "ggml-base.en.bin"
        model.write_bytes(b"stub")

        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "boom"

        stt = WhisperTranscriber(
            binary="/bin/echo", model_path=str(model), runner=lambda _argv: _Proc()
        )
        assert stt.available
        assert stt.transcribe(b"RIFF") == ""

    def test_audio_is_not_left_on_disk(self, tmp_path):
        model = tmp_path / "ggml-base.en.bin"
        model.write_bytes(b"stub")
        seen = {}

        class _Proc:
            returncode = 0
            stdout = "status"
            stderr = ""

        def _runner(argv):
            wav = Path(argv[argv.index("-f") + 1])
            seen["path"] = wav
            assert wav.exists(), "the audio exists only while whisper reads it"
            return _Proc()

        stt = WhisperTranscriber(
            binary="/bin/echo", model_path=str(model), runner=_runner
        )
        assert stt.transcribe(b"RIFF") == "status"
        assert not seen["path"].exists(), "microphone audio must not survive the call"


# ---------------------------------------------------------------------------
# When the phone rings
# ---------------------------------------------------------------------------


class TestCallTrigger:
    def test_an_ordinary_incident_does_not_ring(self):
        trigger = CallTrigger(clock=_Clock())
        incident = _incident(state=IncidentState.FIXING, severity=Severity.HIGH)
        incident.id = "INC-1"
        assert not trigger.evaluate(incident)

    def test_a_successful_repair_does_not_ring(self):
        trigger = CallTrigger(clock=_Clock())
        incident = _incident(state=IncidentState.RESOLVED, severity=Severity.CRITICAL)
        incident.id = "INC-1"
        assert not trigger.evaluate(incident)

    def test_a_critical_jarvis_is_working_on_does_not_ring(self):
        """It rings if JARVIS stops, not because the fault is severe."""
        trigger = CallTrigger(clock=_Clock())
        incident = _incident(state=IncidentState.FIXING, severity=Severity.CRITICAL)
        incident.id = "INC-1"
        assert not trigger.evaluate(incident)

    def test_a_post_merge_failure_rings(self):
        trigger = CallTrigger(clock=_Clock())
        incident = _incident(state=IncidentState.HUMAN_REQUIRED)
        incident.id = "INC-1"
        incident.metadata["post_merge_failure"] = {"reason": "still red"}
        decision = trigger.evaluate(incident)
        assert decision and decision.reason == "post_merge_failure"

    def test_human_required_on_a_production_change_rings(self):
        trigger = CallTrigger(clock=_Clock())
        incident = _incident(state=IncidentState.HUMAN_REQUIRED)
        incident.id = "INC-1"
        assert trigger.evaluate(incident, production_authority_used=True)

    def test_human_required_with_nothing_live_does_not_ring(self):
        """A repair that gave up before touching production is a message."""
        trigger = CallTrigger(clock=_Clock())
        incident = _incident(state=IncidentState.HUMAN_REQUIRED, severity=Severity.HIGH)
        incident.id = "INC-1"
        assert not trigger.evaluate(incident, production_authority_used=False)

    def test_the_same_incident_does_not_ring_twice(self):
        clock = _Clock()
        trigger = CallTrigger(clock=clock, cooldown_seconds=3600)
        incident = _incident(state=IncidentState.HUMAN_REQUIRED)
        incident.id = "INC-1"
        incident.metadata["post_merge_failure"] = {"reason": "red"}

        assert trigger.evaluate(incident)
        assert not trigger.evaluate(incident), "one call per incident"
        clock.advance(3601)
        assert trigger.evaluate(incident), "after the cooldown it may ring again"

    def test_a_storm_of_events_produces_one_call(self):
        trigger = CallTrigger(clock=_Clock())
        incident = _incident(state=IncidentState.HUMAN_REQUIRED)
        incident.id = "INC-1"
        rang = [
            bool(trigger.evaluate(incident, event=event))
            for event in (
                "production_deployment_failed",
                "post_merge_failure",
                "attempts_exhausted",
                "post_merge_failure",
            )
        ]
        assert rang.count(True) == 1


# ---------------------------------------------------------------------------
# The call itself
# ---------------------------------------------------------------------------


class _EchoTranscriber:
    """Returns whatever bytes it was handed, as text."""

    available = True

    def __init__(self, *, raises=False):
        self._raises = raises

    def transcribe(self, wav_bytes: bytes) -> str:
        if self._raises:
            raise RuntimeError("whisper died")
        return wav_bytes.decode("utf-8", "ignore")


def _session(store, session_id="s1", **kwargs):
    from openjarvis.reliability.voice.session import VoiceSession

    clock = kwargs.pop("clock", _Clock())
    return VoiceSession(
        id=session_id,
        commands=_commands(store, **kwargs.pop("command_kwargs", {})),
        transcriber=kwargs.pop("transcriber", _EchoTranscriber()),
        speech=kwargs.pop("speech", None),
        clock=clock,
        **kwargs,
    )


class TestVoiceSession:
    def test_a_full_conversation_runs_to_goodbye(self, store):
        _incident(store)
        session = _session(store)

        assert session.greeting().startswith("Sir,")
        session.hear(b"what happened")
        session.hear(b"what did you try")
        turn = session.hear(b"goodbye")

        assert turn.said == "Goodbye, Sir."
        assert session.ended and session.end_reason == "goodbye"
        assert len(session.transcript()) == 3

    def test_nothing_is_heard_after_hanging_up(self, store):
        session = _session(store)
        session.hear(b"goodbye")
        turn = session.hear(b"emergency stop")
        assert "call has ended" in turn.said
        assert turn.intent == ""

    def test_unintelligible_audio_is_not_acted_on(self, store):
        session = _session(store)
        turn = session.hear(b"")
        assert "didn't catch that" in turn.said
        assert not turn.executed

    def test_a_crashing_transcriber_does_not_crash_the_call(self, store):
        session = _session(store, transcriber=_EchoTranscriber(raises=True))
        turn = session.hear(b"emergency stop")
        assert "didn't catch that" in turn.said
        assert not turn.executed

    def test_secrets_are_redacted_before_the_transcript_exists(self, store):
        """Whatever the microphone picked up, the stored transcript is clean."""
        from openjarvis.reliability.briefing import redact_secrets

        session = _session(store, redact=redact_secrets)
        secret = "ghp_" + "a" * 36
        session.hear(f"the token is {secret}".encode())

        rendered = str(session.transcript())
        assert secret not in rendered

    def test_the_transcript_is_what_sir_heard_and_said(self, store):
        _incident(store)
        session = _session(store)
        session.hear(b"what happened")
        entry = session.transcript()[0]
        assert entry["heard"] == "what happened"
        assert entry["said"].startswith("Sir,")
        assert entry["intent"] == "what_happened"

    def test_a_quiet_call_expires(self, store):
        clock = _Clock()
        session = _session(store, clock=clock, idle_timeout=60.0)
        session.hear(b"what happened")
        clock.advance(61)
        assert session.expired

    def test_a_call_cannot_run_forever(self, store):
        clock = _Clock()
        session = _session(store, clock=clock, max_duration=100.0)
        clock.advance(101)
        assert session.expired

    def test_no_audio_is_produced_without_a_voice(self, store):
        session = _session(store, speech=None)
        assert session.audio_for("hello") == b""


class TestSessionManager:
    def test_only_so_many_calls_may_be_open(self, store):
        from openjarvis.reliability.voice.session import VoiceSessionManager

        manager = VoiceSessionManager(
            factory=lambda sid: _session(store, sid), max_sessions=2
        )
        assert manager.start() is not None
        assert manager.start() is not None
        assert manager.start() is None, "a reload loop must not exhaust the machine"

    def test_hanging_up_frees_the_slot(self, store):
        from openjarvis.reliability.voice.session import VoiceSessionManager

        manager = VoiceSessionManager(
            factory=lambda sid: _session(store, sid), max_sessions=1
        )
        session = manager.start()
        assert manager.start() is None
        assert manager.end(session.id)
        assert manager.start() is not None

    def test_an_ended_session_is_not_returned(self, store):
        from openjarvis.reliability.voice.session import VoiceSessionManager

        manager = VoiceSessionManager(factory=lambda sid: _session(store, sid))
        session = manager.start()
        session.end("goodbye")
        assert manager.get(session.id) is None


class TestTheWatcherIsUnaffected:
    def test_a_voice_subsystem_that_cannot_start_is_not_fatal(self, store):
        """Requirement: the watcher keeps working if voice breaks. Every part
        of the voice stack reports unavailability instead of raising."""
        from openjarvis.reliability.voice.session import VoiceSession

        session = VoiceSession(
            id="s",
            commands=_commands(store),
            transcriber=None,  # whisper missing
            speech=None,  # say missing
        )
        turn = session.hear(b"what happened")
        assert "didn't catch that" in turn.said
        assert session.audio_for("anything") == b""
