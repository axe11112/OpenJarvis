"""The owner is told a thing once.

These tests are written from the complaint rather than from the code: the owner
received the same message about INC-00020 and INC-00021 repeatedly, across
watcher cycles and across restarts. Each test below is one of the ways that
happened.
"""

from __future__ import annotations

from openjarvis.reliability.notify import NotificationRouter
from openjarvis.reliability.notify_ledger import NotificationLedger, owner_state
from openjarvis.reliability.types import Incident, IncidentState, Severity


def _incident(**overrides) -> Incident:
    defaults = dict(
        fingerprint="fp-login",
        severity=Severity.HIGH,
        component="authentication",
        title="Login broken",
        summary="Users are redirected back to /login.",
        id="INC-00020",
        state=IncidentState.HUMAN_REQUIRED,
    )
    defaults.update(overrides)
    return Incident(**defaults)


class _Recorder:
    """A notifier that just counts what the owner would have received."""

    def __init__(self):
        self.messages = []

    def send(self, message, *, severity=None):
        self.messages.append(message)
        return True


def _router(ledger, **kwargs):
    notifier = _Recorder()
    router = NotificationRouter(
        notifier=notifier,
        min_severity=Severity.LOW,
        ledger=ledger,
        **kwargs,
    )
    return router, notifier


# ---------------------------------------------------------------------------
# The ledger itself
# ---------------------------------------------------------------------------


def test_owner_state_collapses_internal_states():
    """Two internal states that ask the same thing of the owner are one state."""
    failed = owner_state(_incident(state=IncidentState.FAILED))
    human = owner_state(_incident(state=IncidentState.HUMAN_REQUIRED))
    assert failed == human == "needs-you:HIGH:"


def test_second_look_at_an_unchanged_problem_is_not_news():
    ledger = NotificationLedger()
    incident = _incident()
    assert ledger.should_notify(incident) is True
    ledger.record(incident)
    assert ledger.should_notify(incident) is False


def test_a_worse_problem_is_news():
    ledger = NotificationLedger()
    ledger.record(_incident(severity=Severity.HIGH))
    assert ledger.should_notify(_incident(severity=Severity.CRITICAL)) is True


def test_a_less_bad_problem_is_not_news():
    """Sliding down is the state machine talking, not the situation changing."""
    ledger = NotificationLedger()
    ledger.record(_incident(severity=Severity.CRITICAL))
    assert ledger.should_notify(_incident(severity=Severity.HIGH)) is False


def test_being_fixed_is_always_news():
    ledger = NotificationLedger()
    ledger.record(_incident())
    fixed = _incident(state=IncidentState.RESOLVED)
    assert ledger.should_notify(fixed) is True


def test_breaking_again_after_a_fix_is_news():
    ledger = NotificationLedger()
    ledger.record(_incident(state=IncidentState.RESOLVED))
    assert ledger.should_notify(_incident()) is True


def test_a_fresh_incident_with_the_same_fingerprint_is_not_news():
    """A flapping probe opens a new incident each time. Same problem, though."""
    ledger = NotificationLedger()
    ledger.record(_incident(id="INC-00020"))
    assert ledger.should_notify(_incident(id="INC-00021")) is False


def test_different_channels_do_not_silence_each_other():
    ledger = NotificationLedger()
    incident = _incident()
    ledger.record(incident, kind="problem")
    assert ledger.should_notify(incident, kind="post-merge") is True


def test_forgetting_makes_it_news_again():
    ledger = NotificationLedger()
    incident = _incident()
    ledger.record(incident)
    ledger.forget(incident)
    assert ledger.should_notify(incident) is True


def test_a_corrupt_ledger_costs_a_duplicate_not_a_crash(tmp_path):
    path = tmp_path / "notified.json"
    path.write_text("{not json", encoding="utf-8")
    ledger = NotificationLedger(path=path)
    assert ledger.should_notify(_incident()) is True


def test_an_unwritable_ledger_does_not_stop_the_notification(tmp_path):
    path = tmp_path / "nope"
    path.mkdir()
    ledger = NotificationLedger(path=path / "x" / "notified.json")
    path.chmod(0o500)
    try:
        ledger.record(_incident())  # must not raise
    finally:
        path.chmod(0o700)


# ---------------------------------------------------------------------------
# Across restarts — the thing the in-memory guards could never do
# ---------------------------------------------------------------------------


def test_a_restart_does_not_repeat_the_message(tmp_path):
    path = tmp_path / "notified.json"
    incident = _incident()

    first, notifier = _router(NotificationLedger(path=path))
    assert (
        first.human_required(
            incident, reason="repair is disabled", attempts=3, max_attempts=3
        )
        is True
    )
    assert len(notifier.messages) == 1

    # The watcher restarts: new process, new router, new ledger object, same disk.
    second, notifier2 = _router(NotificationLedger(path=path))
    assert (
        second.human_required(
            incident, reason="repair is disabled", attempts=3, max_attempts=3
        )
        is False
    )
    assert notifier2.messages == []


def test_repeated_watcher_cycles_send_one_message(tmp_path):
    path = tmp_path / "notified.json"
    router, notifier = _router(NotificationLedger(path=path))
    incident = _incident()
    for _ in range(10):
        router.human_required(
            incident, reason="repair is disabled", attempts=3, max_attempts=3
        )
    assert len(notifier.messages) == 1


def test_critical_then_human_required_is_one_escalation(tmp_path):
    """The duplicate the owner actually reported.

    A CRITICAL incident alerted, then the same incident landed in
    ``HUMAN_REQUIRED`` and alerted again — two messages about one problem that
    had not changed. The grace window holds the first; the ledger holds every
    repeat of the second, across cycles and across a restart.
    """
    path = tmp_path / "notified.json"
    router, notifier = _router(NotificationLedger(path=path))
    incident = _incident(severity=Severity.CRITICAL, state=IncidentState.INVESTIGATING)
    assert router.alert(incident) is False  # detection is never news

    incident.state = IncidentState.HUMAN_REQUIRED
    for _ in range(5):
        router.human_required(
            incident, reason="repair is disabled", attempts=3, max_attempts=3
        )

    restarted, notifier2 = _router(NotificationLedger(path=path))
    restarted.human_required(
        incident, reason="repair is disabled", attempts=3, max_attempts=3
    )

    assert len(notifier.messages) == 1
    assert notifier2.messages == []
    assert "I stopped making changes" in notifier.messages[0]


def test_a_fix_after_silence_still_reaches_the_owner(tmp_path):
    path = tmp_path / "notified.json"
    ledger = NotificationLedger(path=path)
    router, notifier = _router(ledger)
    incident = _incident()
    router.human_required(
        incident, reason="repair is disabled", attempts=3, max_attempts=3
    )
    router.human_required(
        incident, reason="repair is disabled", attempts=3, max_attempts=3
    )
    assert len(notifier.messages) == 1

    incident.state = IncidentState.RESOLVED
    assert router.resolved(incident) is True
    assert len(notifier.messages) == 2

    # ...and the fix clears the record, so the next break is news.
    incident.state = IncidentState.HUMAN_REQUIRED
    assert (
        router.human_required(
            incident,
            reason="protected path: src/auth/session.ts",
            attempts=3,
            max_attempts=3,
        )
        is True
    )


def test_a_deferred_alert_that_is_never_sent_leaves_no_trace(tmp_path):
    """A held CRITICAL must not silence the escalation that supersedes it."""
    path = tmp_path / "notified.json"
    router, notifier = _router(
        NotificationLedger(path=path),
        critical_grace_seconds=60,
        alert_on_critical=True,
    )
    incident = _incident(severity=Severity.CRITICAL, state=IncidentState.INVESTIGATING)
    assert router.alert(incident) is False  # held
    assert notifier.messages == []

    incident.state = IncidentState.HUMAN_REQUIRED
    assert (
        router.human_required(
            incident, reason="repair is disabled", attempts=3, max_attempts=3
        )
        is True
    )
    assert len(notifier.messages) == 1


def test_a_broken_ledger_never_silences_the_owner(tmp_path):
    """When in doubt, speak. A dedup bug must not become a missed outage."""

    class _Broken:
        def should_notify(self, incident, *, kind="owner", ask=None):
            raise RuntimeError("disk on fire")

        def was_told(self, incident, *, kind="owner"):
            raise RuntimeError("disk on fire")

        def record(self, incident, *, kind="owner", ask=None):
            raise RuntimeError("disk on fire")

        def record_fixed(self, incident, *, kind="owner"):
            raise RuntimeError("disk on fire")

        def forget(self, incident, *, kind="owner"):
            raise RuntimeError("disk on fire")

    router, notifier = _router(_Broken())
    assert (
        router.human_required(
            _incident(), reason="repair is disabled", attempts=3, max_attempts=3
        )
        is True
    )
    assert len(notifier.messages) == 1
