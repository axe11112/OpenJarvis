"""Tests for notification rendering, redaction and routing."""

from __future__ import annotations

import pytest

from openjarvis.reliability.notify import (
    ConsoleNotifier,
    NotificationRouter,
    TelegramNotifier,
    render_alert,
    render_human_required,
    render_progress,
    render_resolved,
    render_rolled_back,
)
from openjarvis.reliability.types import (
    Incident,
    RepairAttempt,
    Severity,
    VerificationResult,
)


def _incident(**overrides) -> Incident:
    defaults = dict(
        fingerprint="fp",
        severity=Severity.CRITICAL,
        component="authentication",
        title="Login broken",
        summary="Users are redirected back to /login.",
        id="INC-00042",
    )
    defaults.update(overrides)
    return Incident(**defaults)


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


class TestTemplates:
    def test_alert(self):
        text = render_alert(_incident())
        assert "JARVIS ALERT" in text
        assert "INC-00042" in text
        assert "CRITICAL" in text
        assert "authentication" in text

    def test_alert_persona(self):
        assert render_alert(_incident(), persona=True).startswith("🔴 JARVIS ALERT")
        assert "Sir, I've detected" in render_alert(_incident(), persona=True)

    def test_alert_without_persona_is_still_precise(self):
        text = render_alert(_incident(), persona=False)
        assert "Sir," not in text
        assert "authentication" in text
        assert "INC-00042" in text

    def test_alert_mentions_repeat_occurrences(self):
        incident = _incident()
        incident.record_occurrence()
        assert "Observed 2 times" in render_alert(incident)

    def test_severity_icons_differ(self):
        critical = render_alert(_incident(severity=Severity.CRITICAL))
        low = render_alert(_incident(severity=Severity.LOW))
        assert critical[0] != low[0]

    def test_progress(self):
        text = render_progress(_incident(), attempt=1, max_attempts=3)
        assert "Attempt: 1/3" in text
        assert "reproduced" in text

    def test_resolved_states_how_it_was_verified(self):
        """ "Resolved" without "verified how" is the claim JARVIS exists not to
        make."""
        text = render_resolved(
            _incident(),
            attempt=RepairAttempt(number=1, diff_stat="1 file changed"),
            verification=VerificationResult(
                passed=True, probe_id="auth-login", target_url="https://preview"
            ),
        )
        assert "passed verification" in text
        assert "auth-login" in text
        assert "https://preview" in text

    def test_resolved_includes_pull_request(self):
        incident = _incident()
        incident.resolution.pr_url = "https://github.com/x/y/pull/3"
        assert "pull/3" in render_resolved(incident)

    def test_human_required(self):
        text = render_human_required(
            _incident(),
            reason="verification failed 3 times",
            attempts=3,
            max_attempts=3,
        )
        assert "Human intervention" in text
        assert "3/3" in text
        assert "verification failed 3 times" in text

    def test_rolled_back(self):
        text = render_rolled_back(_incident(), reason="regression detected")
        assert "rolled it back" in text
        assert "regression detected" in text


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class TestConsoleNotifier:
    def test_records_messages(self):
        notifier = ConsoleNotifier()
        assert notifier.send("hello")
        assert notifier.sent == ["hello"]


class TestTelegramNotifier:
    def test_sends_via_the_channel(self):
        class _Channel:
            def __init__(self):
                self.sent = []

            def send(self, chat, content, **kwargs):
                self.sent.append((chat, content))
                return True

        channel = _Channel()
        notifier = TelegramNotifier(chat_id="123", channel=channel)
        assert notifier.send("hi")
        assert channel.sent == [("123", "hi")]

    def test_no_chat_id_fails_cleanly(self):
        assert not TelegramNotifier(chat_id="", channel=object()).send("hi")

    def test_channel_failure_is_reported_not_raised(self):
        class _Channel:
            def send(self, *a, **k):
                raise RuntimeError("network down")

        assert not TelegramNotifier(chat_id="1", channel=_Channel()).send("hi")

    def test_channel_returning_false_is_a_failure(self):
        class _Channel:
            def send(self, *a, **k):
                return False

        assert not TelegramNotifier(chat_id="1", channel=_Channel()).send("hi")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class TestRouting:
    def _router(self, **kwargs):
        notifier = ConsoleNotifier()
        clock = _FakeClock()
        kwargs.setdefault("clock", clock)
        router = NotificationRouter(notifier=notifier, **kwargs)
        return router, notifier, clock

    def test_sends_at_or_above_min_severity(self):
        router, notifier, _ = self._router(min_severity=Severity.MEDIUM)
        assert router.notify("a", severity=Severity.HIGH)
        assert router.notify("b", severity=Severity.MEDIUM)
        assert len(notifier.sent) == 2

    def test_drops_below_min_severity(self):
        router, notifier, _ = self._router(min_severity=Severity.MEDIUM)
        assert not router.notify("quiet", severity=Severity.LOW)
        assert notifier.sent == []

    def test_deduplicates_identical_messages(self):
        router, notifier, _ = self._router(dedup_window_seconds=300)
        assert router.notify("same", severity=Severity.HIGH)
        assert not router.notify("same", severity=Severity.HIGH)
        assert len(notifier.sent) == 1

    def test_dedup_window_expires(self):
        router, notifier, clock = self._router(dedup_window_seconds=60)
        router.notify("same", severity=Severity.HIGH)
        clock.advance(61)
        assert router.notify("same", severity=Severity.HIGH)
        assert len(notifier.sent) == 2

    def test_different_messages_are_not_deduplicated(self):
        router, notifier, _ = self._router()
        router.notify("a", severity=Severity.HIGH)
        router.notify("b", severity=Severity.HIGH)
        assert len(notifier.sent) == 2

    def test_rate_cap_bounds_a_storm(self):
        """50 incidents must not become 50 messages."""
        router, notifier, _ = self._router(max_per_hour=5, dedup_window_seconds=0)
        for index in range(50):
            router.notify(f"incident {index}", severity=Severity.HIGH)
        assert len(notifier.sent) == 5

    def test_critical_always_gets_through(self):
        """A cap that silences the one message the owner needs is worse than
        no cap at all."""
        router, notifier, _ = self._router(max_per_hour=2, dedup_window_seconds=0)
        for index in range(5):
            router.notify(f"noise {index}", severity=Severity.HIGH)
        assert router.notify("the big one", severity=Severity.CRITICAL)
        assert "the big one" in notifier.sent[-1]

    def test_rate_cap_window_rolls_over(self):
        router, notifier, clock = self._router(max_per_hour=2, dedup_window_seconds=0)
        router.notify("a", severity=Severity.HIGH)
        router.notify("b", severity=Severity.HIGH)
        assert not router.notify("c", severity=Severity.HIGH)
        clock.advance(3601)
        assert router.notify("d", severity=Severity.HIGH)

    def test_suppression_is_reported_once_the_window_reopens(self):
        router, notifier, clock = self._router(max_per_hour=1, dedup_window_seconds=0)
        router.notify("first", severity=Severity.HIGH)
        router.notify("dropped 1", severity=Severity.HIGH)
        router.notify("dropped 2", severity=Severity.HIGH)
        clock.advance(3601)
        router.notify("later", severity=Severity.HIGH)
        assert "2 further notification(s) were suppressed" in notifier.sent[-1]


class TestOutboundRedaction:
    def test_secret_is_redacted_before_sending(self):
        notifier = ConsoleNotifier()
        router = NotificationRouter(notifier=notifier, min_severity=Severity.LOW)
        router.notify("token ghp_" + "a" * 36, severity=Severity.HIGH)
        assert "ghp_" + "a" * 36 not in notifier.sent[0]

    def test_redaction_can_be_disabled(self):
        notifier = ConsoleNotifier()
        router = NotificationRouter(
            notifier=notifier, min_severity=Severity.LOW, redact=False
        )
        router.notify("plain text", severity=Severity.HIGH)
        assert notifier.sent[0] == "plain text"

    def test_redaction_failure_falls_back_to_the_stripper(self, monkeypatch):
        """A redaction failure must never mean 'send it raw'."""
        import openjarvis.security.boundary as boundary_module

        class _Exploding:
            def __init__(self, *a, **k):
                raise RuntimeError("guard is broken")

        monkeypatch.setattr(boundary_module, "BoundaryGuard", _Exploding)
        notifier = ConsoleNotifier()
        router = NotificationRouter(notifier=notifier, min_severity=Severity.LOW)
        router.notify("token ghp_" + "b" * 36, severity=Severity.HIGH)
        assert "ghp_" + "b" * 36 not in notifier.sent[0]


class TestIncidentHelpers:
    def _router(self, **kwargs):
        notifier = ConsoleNotifier()
        kwargs.setdefault("min_severity", Severity.LOW)
        kwargs.setdefault("clock", _FakeClock())
        return NotificationRouter(notifier=notifier, **kwargs), notifier

    def test_alert(self):
        router, notifier = self._router()
        assert router.alert(_incident())
        assert "JARVIS ALERT" in notifier.sent[0]

    def test_progress(self):
        router, notifier = self._router()
        router.progress(_incident(), attempt=2, max_attempts=3)
        assert "2/3" in notifier.sent[0]

    def test_resolved(self):
        router, notifier = self._router()
        router.resolved(
            _incident(), verification=VerificationResult(passed=True, probe_id="p")
        )
        assert "passed verification" in notifier.sent[0]

    def test_human_required_is_always_critical(self):
        """An escalation the owner never sees is the same as no escalation."""
        router, notifier = self._router(
            min_severity=Severity.CRITICAL, max_per_hour=1, dedup_window_seconds=0
        )
        router.notify("filler", severity=Severity.CRITICAL)
        assert router.human_required(
            _incident(severity=Severity.LOW),
            reason="attempts exhausted",
            attempts=3,
            max_attempts=3,
        )
        assert "Human intervention" in notifier.sent[-1]

    def test_rolled_back(self):
        router, notifier = self._router()
        router.rolled_back(_incident(), reason="regression")
        assert "rolled it back" in notifier.sent[0]

    @pytest.mark.parametrize("persona", [True, False])
    def test_persona_flag_is_honoured(self, persona):
        router, notifier = self._router(persona=persona)
        router.alert(_incident())
        assert ("Sir," in notifier.sent[0]) is persona
