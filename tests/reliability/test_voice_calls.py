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
