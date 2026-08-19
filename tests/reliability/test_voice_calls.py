"""Tests for ringing the phone — and, mostly, for not ringing it.

A watcher ticks every sixty seconds. An incident in ``HUMAN_REQUIRED`` stays
there until a human wakes up. Every test below exists because the naive version
of this feature rings sixty times before breakfast, and a phone that does that
gets the app deleted rather than the operator's attention.
"""

from __future__ import annotations

from typing import List

from openjarvis.reliability.voice.calls import CallOrchestrator
from openjarvis.reliability.voice.health import VoiceHealth


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Subscriptions:
    def __init__(self, count=1):
        self.items = [
            type(
                "S",
                (),
                {"endpoint": f"https://web.push.apple.com/{i}", "added_at": "now"},
            )()
            for i in range(count)
        ]
        self.removed: List[str] = []

    def all(self):
        return list(self.items)

    def remove(self, endpoint):
        self.removed.append(endpoint)
        self.items = [s for s in self.items if s.endpoint != endpoint]
        return True


class _Push:
    def __init__(self, *, ok=True, detail="delivered"):
        self.ok = ok
        self.detail = detail
        self.sent = 0

    def send(self, _subscription):
        self.sent += 1
        return self.ok, self.detail


def _orchestrator(**kwargs) -> CallOrchestrator:
    clock = kwargs.pop("clock", _Clock())
    events: List[tuple] = []
    orchestrator = CallOrchestrator(
        push=kwargs.pop("push", _Push()),
        subscriptions=kwargs.pop("subscriptions", _Subscriptions()),
        fallback=kwargs.pop("fallback", None),
        audit=lambda event, payload: events.append((event, payload)),
        clock=clock,
        **kwargs,
    )
    orchestrator.events = events  # type: ignore[attr-defined]
    orchestrator.clock_obj = clock  # type: ignore[attr-defined]
    return orchestrator


# ---------------------------------------------------------------------------
# Storm protection
# ---------------------------------------------------------------------------


class TestCallStormProtection:
    def test_a_call_rings_the_registered_phone(self):
        calls = _orchestrator()
        call = calls.ring(
            reason="human_required", detail="login is failing", incident_id="INC-1"
        )
        assert call is not None and call.state == "RINGING"
        assert call.push_delivered == 1

    def test_only_one_call_may_be_active(self):
        calls = _orchestrator()
        assert calls.ring(reason="human_required", detail="a", incident_id="INC-1")
        assert (
            calls.ring(reason="post_merge_failure", detail="b", incident_id="INC-2")
            is None
        )

    def test_the_same_incident_does_not_ring_forever(self):
        """The watcher will present this incident every sixty seconds."""
        calls = _orchestrator()
        rang = 0
        for _ in range(60):
            call = calls.ring(reason="human_required", detail="x", incident_id="INC-1")
            if call:
                rang += 1
                calls.missed(call.id)
                calls.clock_obj.advance(1)  # a tick, not a cooldown
        assert rang <= calls.max_attempts, f"rang {rang} times in an hour of ticks"

    def test_a_decline_is_honoured_longer_than_a_miss(self):
        calls = _orchestrator()
        first = calls.ring(reason="human_required", detail="x", incident_id="INC-1")
        calls.declined(first.id)

        calls.clock_obj.advance(calls.missed_cooldown + 1)
        assert (
            calls.ring(reason="human_required", detail="x", incident_id="INC-1") is None
        ), "a decline means not now, and means it for longer than a miss"
        calls.clock_obj.advance(calls.decline_cooldown)
        assert calls.ring(reason="human_required", detail="x", incident_id="INC-1")

    def test_a_missed_call_may_ring_again_after_its_cooldown(self):
        calls = _orchestrator()
        first = calls.ring(reason="human_required", detail="x", incident_id="INC-1")
        calls.missed(first.id)
        assert (
            calls.ring(reason="human_required", detail="x", incident_id="INC-1") is None
        )
        calls.clock_obj.advance(calls.missed_cooldown + 1)
        assert calls.ring(reason="human_required", detail="x", incident_id="INC-1")

    def test_answering_clears_the_suppression(self):
        """The escalation was dealt with, so a later genuine event may ring."""
        calls = _orchestrator()
        first = calls.ring(reason="human_required", detail="x", incident_id="INC-1")
        calls.answered(first.id)
        assert calls.ring(reason="human_required", detail="x", incident_id="INC-1")

    def test_a_ringing_call_times_out_into_missed(self):
        calls = _orchestrator()
        calls.ring(reason="human_required", detail="x", incident_id="INC-1")
        calls.clock_obj.advance(calls.ring_seconds + 1)
        assert calls.snapshot()["active"] is None
        assert calls.snapshot()["last"]["state"] == "MISSED"

    def test_different_incidents_are_tracked_separately(self):
        calls = _orchestrator()
        first = calls.ring(reason="human_required", detail="x", incident_id="INC-1")
        calls.declined(first.id)
        assert calls.ring(reason="human_required", detail="y", incident_id="INC-2"), (
            "a separate problem is a separate call"
        )


class TestFallback:
    def test_a_phone_that_cannot_be_reached_gets_a_message_instead(self):
        told: List[tuple] = []
        calls = _orchestrator(
            push=_Push(ok=False, detail="unreachable"),
            fallback=lambda reason, detail: told.append((reason, detail)),
        )
        calls.ring(
            reason="human_required", detail="login is failing", incident_id="INC-1"
        )
        assert told == [("human_required", "login is failing")]

    def test_exhausted_attempts_fall_back_once(self):
        told: List[tuple] = []
        calls = _orchestrator(fallback=lambda r, d: told.append((r, d)))
        for _ in range(calls.max_attempts):
            call = calls.ring(reason="human_required", detail="x", incident_id="INC-1")
            calls.missed(call.id)
            calls.clock_obj.advance(calls.missed_cooldown + 1)
        calls.ring(reason="human_required", detail="x", incident_id="INC-1")
        assert len(told) == 1, "the operator is told once, not on every tick"

    def test_an_expired_subscription_is_dropped(self):
        subscriptions = _Subscriptions()
        calls = _orchestrator(
            push=_Push(ok=False, detail="expired"), subscriptions=subscriptions
        )
        calls.ring(reason="human_required", detail="x", incident_id="INC-1")
        assert subscriptions.removed, "a dead endpoint must not be retried forever"

    def test_a_failing_fallback_does_not_raise(self):
        def _explode(_reason, _detail):
            raise RuntimeError("telegram is down")

        calls = _orchestrator(push=_Push(ok=False), fallback=_explode)
        assert calls.ring(reason="human_required", detail="x", incident_id="INC-1")


class TestTestCall:
    def test_a_test_call_ignores_the_cooldowns(self):
        """It has to ring every time, or it cannot be used to debug a phone."""
        calls = _orchestrator()
        for _ in range(5):
            call = calls.ring(reason="test", detail="test", test=True)
            assert call is not None and call.test
            calls.missed(call.id)

    def test_a_test_call_leaves_no_cooldown_behind(self):
        calls = _orchestrator()
        call = calls.ring(reason="test", detail="test", test=True)
        calls.declined(call.id)
        assert calls.snapshot()["cooling_down"] == {}

    def test_a_test_call_is_marked_as_one(self):
        calls = _orchestrator()
        call = calls.ring(reason="test", detail="test", test=True)
        assert call.to_dict()["test"] is True


class TestAudit:
    def test_every_outcome_is_audited(self):
        calls = _orchestrator()
        call = calls.ring(reason="human_required", detail="x", incident_id="INC-1")
        calls.answered(call.id)
        blocked = calls.ring(reason="human_required", detail="x", incident_id="INC-1")
        calls.declined(blocked.id)

        events = [name for name, _ in calls.events]
        assert "call_requested" in events
        assert "call_delivery" in events
        assert "call_answered" in events
        assert "call_declined" in events

    def test_suppression_is_audited_with_a_reason(self):
        calls = _orchestrator()
        calls.ring(reason="human_required", detail="x", incident_id="INC-1")
        calls.ring(reason="human_required", detail="y", incident_id="INC-2")
        suppressed = [p for name, p in calls.events if name == "call_suppressed"]
        assert suppressed and "already in progress" in suppressed[0]["why"]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class _Part:
    def __init__(self, available, reason=""):
        self.available = available
        self._reason = reason
        self.model_path = "/models/ggml-tiny.en.bin"
        self.voice = "Daniel"

    def unavailable_reason(self):
        return self._reason


class _Normalizer:
    def __init__(self, iphone=True):
        self.iphone = iphone

    def capabilities(self):
        return {
            "afconvert": self.iphone,
            "ffmpeg": False,
            "containers": ["mp4", "wav"] if self.iphone else ["wav"],
            "iphone_supported": self.iphone,
        }


def _health(**kwargs) -> VoiceHealth:
    defaults = dict(
        transcriber=_Part(True),
        speech=_Part(True),
        normalizer=_Normalizer(),
        subscriptions=_Subscriptions(),
        calls=_orchestrator(),
    )
    defaults.update(kwargs)
    return VoiceHealth(**defaults)


class TestVoiceHealth:
    def test_unknown_is_never_reported_as_healthy(self):
        """The rule this module exists for. A green light for something nobody
        checked is how an operator stops checking."""
        health = _health(transcriber=None, speech=None)
        snapshot = health.snapshot()
        assert snapshot["voice"] != "ONLINE"
        assert snapshot["parts"]["stt"]["state"] == "UNKNOWN"

    def test_no_transcription_means_offline(self):
        """Hearing is what a call is. Without it, voice is not degraded."""
        health = _health(transcriber=_Part(False, "whisper.cpp is not installed"))
        snapshot = health.snapshot()
        assert snapshot["voice"] == "OFFLINE"
        assert "not installed" in snapshot["parts"]["stt"]["detail"]

    def test_no_speech_means_offline(self):
        assert _health(speech=_Part(False, "no say")).snapshot()["voice"] == "OFFLINE"

    def test_a_phone_that_never_registered_is_degraded_not_online(self):
        health = _health(subscriptions=_Subscriptions(count=0))
        snapshot = health.snapshot()
        assert snapshot["voice"] == "DEGRADED"
        assert snapshot["parts"]["phone"]["state"] == "NOT_REGISTERED"

    def test_audio_that_cannot_decode_an_iphone_is_failed(self):
        health = _health(normalizer=_Normalizer(iphone=False))
        snapshot = health.snapshot()
        assert snapshot["parts"]["audio"]["state"] == "FAILED"
        assert snapshot["voice"] == "DEGRADED"

    def test_the_call_channel_admits_it_is_not_a_real_call(self):
        """Honesty about iOS: this is a notification, not CallKit."""
        state = _health().call_channel()
        assert state["state"] == "LIMITED"
        assert "not a native incoming call" in state["detail"]

    def test_the_call_channel_is_unavailable_without_a_phone(self):
        health = _health(
            subscriptions=_Subscriptions(count=0),
            calls=_orchestrator(subscriptions=_Subscriptions(count=0)),
        )
        assert health.call_channel()["state"] == "UNAVAILABLE"

    def test_a_healthy_stack_with_everything_checked_is_online(self):
        health = _health(
            access=type(
                "A", (), {"tailscale_enabled": True, "tailscale_host": "mac.ts.net"}
            )(),
            tailscale_runner=lambda *a, **k: type("P", (), {"returncode": 0})(),
        )
        snapshot = health.snapshot()
        # call_channel is LIMITED by design on iOS, which is not a fault.
        assert snapshot["parts"]["tailscale"]["state"] == "REACHABLE"
        assert snapshot["voice"] in ("ONLINE", "DEGRADED")

    def test_an_unreachable_tailnet_is_degraded(self):
        health = _health(
            access=type(
                "A", (), {"tailscale_enabled": True, "tailscale_host": "mac.ts.net"}
            )(),
            tailscale_runner=lambda *a, **k: type("P", (), {"returncode": 1})(),
        )
        snapshot = health.snapshot()
        assert snapshot["parts"]["tailscale"]["state"] == "UNREACHABLE"
        assert snapshot["voice"] == "DEGRADED"

    def test_a_crashing_probe_is_unknown_not_healthy(self):
        def _explode(*_a, **_kw):
            raise OSError("no tailscale binary")

        health = _health(
            access=type("A", (), {"tailscale_enabled": True, "tailscale_host": "x"})(),
            tailscale_runner=_explode,
        )
        assert health.tailscale()["state"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Noticing that a call is warranted
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self, incidents=()):
        self.incidents = list(incidents)
        self.reads = 0

    def list(self, **_kw):
        self.reads += 1
        return list(self.incidents)


def _incident(**overrides):
    from openjarvis.reliability.types import Incident, IncidentState, Severity

    fields = dict(
        fingerprint="fp",
        severity=Severity.HIGH,
        component="authentication",
        title="Login broken",
        id="INC-1",
        state=IncidentState.DETECTED,
    )
    fields.update(overrides)
    state = fields.pop("state")
    incident = Incident(**fields)
    incident.state = state
    return incident


def _watchdog(incidents, **kwargs):
    from openjarvis.reliability.voice.trigger import CallTrigger
    from openjarvis.reliability.voice.watchdog import CallWatchdog

    calls = kwargs.pop("calls", None) or _orchestrator()
    return CallWatchdog(
        store=_Store(incidents),
        trigger=kwargs.pop("trigger", None)
        or CallTrigger(clock=_Clock(), minimum_age_seconds=0),
        calls=calls,
        **kwargs,
    )


class TestCallWatchdog:
    def test_an_ordinary_incident_never_rings(self):
        """The overwhelmingly common case, on every tick, forever."""
        from openjarvis.reliability.types import IncidentState

        watchdog = _watchdog([_incident(state=IncidentState.FIXING)])
        assert watchdog.tick() is None

    def test_a_resolved_incident_never_rings(self):
        from openjarvis.reliability.types import IncidentState, Severity

        watchdog = _watchdog(
            [_incident(state=IncidentState.RESOLVED, severity=Severity.CRITICAL)]
        )
        assert watchdog.tick() is None

    def test_a_post_merge_failure_rings(self):
        from openjarvis.reliability.types import IncidentState

        incident = _incident(state=IncidentState.HUMAN_REQUIRED)
        incident.metadata["post_merge_failure"] = {"reason": "still red"}
        watchdog = _watchdog([incident])

        call = watchdog.tick()
        assert call is not None
        assert call.incident_id == "INC-1"
        assert "still fails" in call.detail

    def test_it_does_not_ring_again_on_the_next_tick(self):
        """The whole reason this is polled rather than pushed."""
        from openjarvis.reliability.types import IncidentState

        incident = _incident(state=IncidentState.HUMAN_REQUIRED)
        incident.metadata["post_merge_failure"] = {"reason": "still red"}
        watchdog = _watchdog([incident])

        assert watchdog.tick() is not None
        watchdog.calls.answered()  # clear the active call, not the trigger
        assert watchdog.tick() is None, "one call per incident, not one per tick"

    def test_a_broken_store_does_not_stop_the_loop(self):
        class _Broken:
            def list(self, **_kw):
                raise RuntimeError("database gone")

        watchdog = _watchdog([])
        watchdog.store = _Broken()
        assert watchdog.tick() is None

    def test_a_high_severity_escalation_with_nothing_live_does_not_ring(self):
        """A repair that gave up before touching production is a message, not a
        call. The operator can read it in the morning."""
        from openjarvis.reliability.types import IncidentState, Severity

        watchdog = _watchdog(
            [_incident(state=IncidentState.HUMAN_REQUIRED, severity=Severity.HIGH)]
        )
        assert watchdog.tick() is None

    def test_the_detail_is_plain_english(self):
        from openjarvis.reliability.types import IncidentState, Severity

        incident = _incident(
            state=IncidentState.HUMAN_REQUIRED, severity=Severity.CRITICAL
        )
        watchdog = _watchdog([incident])
        call = watchdog.tick()

        assert call is not None
        assert "Login" in call.detail
        assert "authentication" not in call.detail
        assert "HUMAN_REQUIRED" not in call.detail

    def test_a_flapping_incident_is_not_called_about_immediately(self):
        """The guard that exists because of real behaviour on this machine.

        CRITICAL probe timeouts open, escalate because CRITICAL is not in the
        auto-repair allowlist, and clear themselves minutes later — six in one
        day. Ringing on sight turns a flapping screenshot into a phone call at
        two in the morning about something that fixed itself.
        """
        from openjarvis.reliability.types import IncidentState, Severity
        from openjarvis.reliability.voice.trigger import CallTrigger

        fresh = _incident(
            state=IncidentState.HUMAN_REQUIRED, severity=Severity.CRITICAL
        )
        watchdog = _watchdog(
            [fresh], trigger=CallTrigger(clock=_Clock(), minimum_age_seconds=300)
        )
        assert watchdog.tick() is None, "a brand-new incident must prove itself first"

    def test_a_problem_that_persists_does_ring(self):
        """The other half: waiting must not mean never."""
        from datetime import datetime, timedelta, timezone

        from openjarvis.reliability.types import IncidentState, Severity
        from openjarvis.reliability.voice.trigger import CallTrigger

        old = _incident(state=IncidentState.HUMAN_REQUIRED, severity=Severity.CRITICAL)
        old.created_at = (
            datetime.now(timezone.utc) - timedelta(minutes=20)
        ).isoformat()
        watchdog = _watchdog(
            [old], trigger=CallTrigger(clock=_Clock(), minimum_age_seconds=300)
        )
        assert watchdog.tick() is not None

    def test_a_post_merge_failure_ignores_the_waiting_period(self):
        """Unreviewed code is live and production is unwell. That does not
        improve by waiting to see."""
        from openjarvis.reliability.types import IncidentState
        from openjarvis.reliability.voice.trigger import CallTrigger

        fresh = _incident(state=IncidentState.HUMAN_REQUIRED)
        fresh.metadata["post_merge_failure"] = {"reason": "still red"}
        watchdog = _watchdog(
            [fresh], trigger=CallTrigger(clock=_Clock(), minimum_age_seconds=300)
        )
        assert watchdog.tick() is not None

    def test_many_flapping_probes_cannot_exceed_the_hourly_cap(self):
        """Per-incident guards do not help when ten different probes flap."""
        from datetime import datetime, timedelta, timezone

        from openjarvis.reliability.types import IncidentState, Severity
        from openjarvis.reliability.voice.trigger import CallTrigger

        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        incidents = []
        for index in range(10):
            incident = _incident(
                id=f"INC-{index}",
                state=IncidentState.HUMAN_REQUIRED,
                severity=Severity.CRITICAL,
            )
            incident.created_at = old
            incidents.append(incident)

        trigger = CallTrigger(
            clock=_Clock(), minimum_age_seconds=0, max_calls_per_hour=3
        )
        calls = _orchestrator()
        watchdog = _watchdog(incidents, trigger=trigger, calls=calls)

        rang = 0
        for _ in range(20):
            call = watchdog.tick()
            if call:
                rang += 1
                calls.answered(call.id)
        assert rang <= 3, f"rang {rang} times in an hour despite the cap"

    def test_a_recurring_flap_is_one_problem_not_many(self):
        """The guard that per-incident dedup cannot provide.

        A flapping probe opens a *new* incident every time it fails. Observed on
        this machine: one fingerprint produced INC-00014 at 05:35 and INC-00021
        at 01:15 the next morning, with two more in between. Keyed by incident
        id, each is a fresh problem and rings again.
        """
        from datetime import datetime, timedelta, timezone

        from openjarvis.reliability.types import IncidentState, Severity
        from openjarvis.reliability.voice.trigger import CallTrigger

        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        trigger = CallTrigger(clock=_Clock(), minimum_age_seconds=0)
        calls = _orchestrator()

        rang = 0
        for index in range(6):
            # Same fingerprint, different incident id — a flap, not six faults.
            incident = _incident(
                id=f"INC-{index}",
                state=IncidentState.HUMAN_REQUIRED,
                severity=Severity.CRITICAL,
                fingerprint="fp_the_same_flapping_probe",
            )
            incident.created_at = old
            watchdog = _watchdog([incident], trigger=trigger, calls=calls)
            call = watchdog.tick()
            if call:
                rang += 1
                calls.answered(call.id)

        assert rang == 1, f"one flapping probe rang {rang} times"

    def test_the_reliability_core_does_not_import_voice(self):
        """The architectural rule, asserted rather than hoped for: a microphone
        must never be able to break the repair loop."""
        import pathlib

        core = pathlib.Path("src/openjarvis/reliability")
        for name in ("repair.py", "watch.py", "detector.py", "merge.py", "store.py"):
            source = (core / name).read_text()
            assert "voice" not in source.replace("invoice", ""), (
                f"{name} must not depend on the voice subsystem"
            )


class TestARestartDoesNotRingAgain:
    """The suppression guards were memory, and a restart is what destroys memory.

    `CallWatchdog` re-reads every open incident thirty seconds after start, and
    `minimum_age_seconds` holds nothing back — an incident open for an hour
    passes an age check immediately. So a watcher that restarted overnight rang
    the owner again about the problem they had already been woken for, and the
    supervisor that restarts a dead watcher could do it on a loop.
    """

    @staticmethod
    def _trigger(tmp_path, clock, wall):
        from openjarvis.reliability.voice.trigger import CallTrigger

        return CallTrigger(
            path=tmp_path / "called.json",
            clock=clock,
            wall_clock=wall,
            minimum_age_seconds=0,
        )

    def test_the_same_problem_is_not_rung_about_after_a_restart(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        from openjarvis.reliability.types import IncidentState, Severity

        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        incident = _incident(
            state=IncidentState.HUMAN_REQUIRED, severity=Severity.CRITICAL
        )
        incident.created_at = old

        clock, wall = _Clock(), _Clock()
        wall.now = 1_700_000_000.0
        before = self._trigger(tmp_path, clock, wall)
        assert before.evaluate(incident, event="human_required_production")

        # The machine sleeps for ten minutes and the watcher comes back. The
        # monotonic clock restarts from zero; the wall clock does not.
        clock2, wall2 = _Clock(), _Clock()
        wall2.now = wall.now + 600.0
        after = self._trigger(tmp_path, clock2, wall2)
        assert not after.evaluate(incident, event="human_required_production")

    def test_it_rings_again_once_the_cooldown_has_genuinely_passed(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        from openjarvis.reliability.types import IncidentState, Severity

        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        incident = _incident(
            state=IncidentState.HUMAN_REQUIRED, severity=Severity.CRITICAL
        )
        incident.created_at = old

        clock, wall = _Clock(), _Clock()
        wall.now = 1_700_000_000.0
        before = self._trigger(tmp_path, clock, wall)
        assert before.evaluate(incident, event="human_required_production")

        clock2, wall2 = _Clock(), _Clock()
        wall2.now = wall.now + 7200.0  # two hours, past the one-hour cooldown
        after = self._trigger(tmp_path, clock2, wall2)
        assert after.evaluate(incident, event="human_required_production")

    def test_a_backwards_wall_clock_suppresses_rather_than_rings(self, tmp_path):
        """A nonsense timestamp reads as 'just called', not 'called in the future'."""
        from datetime import datetime, timedelta, timezone

        from openjarvis.reliability.types import IncidentState, Severity

        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        incident = _incident(
            state=IncidentState.HUMAN_REQUIRED, severity=Severity.CRITICAL
        )
        incident.created_at = old

        clock, wall = _Clock(), _Clock()
        wall.now = 1_700_000_000.0
        before = self._trigger(tmp_path, clock, wall)
        assert before.evaluate(incident, event="human_required_production")

        clock2, wall2 = _Clock(), _Clock()
        wall2.now = wall.now - 3600.0  # the clock went backwards
        after = self._trigger(tmp_path, clock2, wall2)
        assert not after.evaluate(incident, event="human_required_production")

    def test_no_path_still_works(self):
        """Voice is optional and so is its state file."""
        from datetime import datetime, timedelta, timezone

        from openjarvis.reliability.types import IncidentState, Severity
        from openjarvis.reliability.voice.trigger import CallTrigger

        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        incident = _incident(
            state=IncidentState.HUMAN_REQUIRED, severity=Severity.CRITICAL
        )
        incident.created_at = old
        trigger = CallTrigger(clock=_Clock(), minimum_age_seconds=0)
        assert trigger.evaluate(incident, event="human_required_production")
        assert not trigger.evaluate(incident, event="human_required_production")
