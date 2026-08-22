"""Wiz's health, and the one line it must never cross.

Every test here is either "a broken Wiz subsystem is reported" or "nothing in
this module can be made to say anything about Wize". The second kind is the
more important one: a health check that quietly starts reading incidents is
how "the watcher crashed" turns into a false "the site is down".
"""

from __future__ import annotations

import pytest

from openjarvis.reliability.health import HealthState
from openjarvis.wiz.health import WizHealthReport, build_wiz_health, default_checks


class Availability:
    def __init__(self, configured, detail=""):
        self.configured = configured
        self.detail = detail


class State:
    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------------------
# The distinction the whole module exists to protect
# ---------------------------------------------------------------------------


def test_nothing_here_accepts_an_incident_store_or_probe_result():
    """The module's public surface has no parameter for either.

    A structural check rather than a behavioural one: the safest way to keep
    a health report from drifting into reporting on incidents is for there to
    be no argument it could read one from.
    """
    import inspect

    names = set(inspect.signature(default_checks).parameters)
    for forbidden in ("incidents", "probes", "site_health", "reliability"):
        assert forbidden not in names


def test_a_healthy_wiz_says_nothing_about_the_website():
    report = build_wiz_health()
    text = str(report.to_dict()).lower()
    for word in ("incident", "probe", "outage", "production", "website"):
        assert word not in text


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def test_a_working_watcher_is_healthy():
    report = build_wiz_health(watcher_status=lambda: State("ONLINE"))
    watcher = next(c for c in report.checks if c.name == "watcher")
    assert watcher.state is HealthState.HEALTHY


def test_a_watcher_status_object_uses_its_value_not_its_repr():
    """The bug this module actually shipped with.

    ``WatcherStatus`` mixes in ``str``, and on this Python ``str(member)``
    returns ``"WatcherStatus.OFFLINE"`` rather than ``"OFFLINE"`` — which
    compared equal to nothing in the mapping and fell through to FAILED. A
    watcher merely unsupported on this platform must never be reported as
    broken.
    """

    class RealisticEnum(str):
        def __new__(cls, value):
            return super().__new__(cls, value)

        @property
        def value(self):
            # The real property: returns the plain string, same as any Enum.
            return str.__str__(self)

        def __str__(self):
            # The real quirk: str(str_enum_member) is "ClassName.NAME" on this
            # Python, not the value — which is exactly what tripped this up.
            return f"WatcherStatus.{str.__str__(self)}"

    status = RealisticEnum("OFFLINE")
    report = build_wiz_health(
        watcher_status=lambda: State(status, "not supported on this platform")
    )
    watcher = next(c for c in report.checks if c.name == "watcher")
    assert watcher.state is HealthState.NOT_CONFIGURED, watcher.summary


def test_a_deliberate_stop_is_not_reported_as_a_fault():
    report = build_wiz_health(
        watcher_status=lambda: State("STOPPED_BY_OPERATOR", "emergency stop engaged")
    )
    watcher = next(c for c in report.checks if c.name == "watcher")
    assert watcher.state is HealthState.NOT_CONFIGURED
    assert "deliberately" in watcher.summary


def test_a_watcher_error_status_is_a_real_failure():
    report = build_wiz_health(watcher_status=lambda: State("ERROR", "crashed"))
    watcher = next(c for c in report.checks if c.name == "watcher")
    assert watcher.state is HealthState.FAILED


def test_no_watcher_probe_is_not_configured_not_failed():
    report = build_wiz_health()
    watcher = next(c for c in report.checks if c.name == "watcher")
    assert watcher.state is HealthState.NOT_CONFIGURED


def test_a_watcher_probe_that_raises_is_unknown_not_a_crash():
    def boom():
        raise RuntimeError("launchctl vanished")

    report = build_wiz_health(watcher_status=boom)
    watcher = next(c for c in report.checks if c.name == "watcher")
    assert watcher.state is HealthState.UNKNOWN


def test_a_tampered_journal_is_a_failure():
    class Journal:
        def verify(self):
            return False, 3

    report = build_wiz_health(journal=Journal())
    audit = next(c for c in report.checks if c.name == "audit_trail")
    assert audit.state is HealthState.FAILED
    assert "3" in audit.summary


def test_an_intact_journal_is_healthy():
    class Journal:
        def verify(self):
            return True, None

    report = build_wiz_health(journal=Journal())
    audit = next(c for c in report.checks if c.name == "audit_trail")
    assert audit.state is HealthState.HEALTHY


def test_the_coding_engine_reports_what_the_probe_says():
    report = build_wiz_health(
        coding_engine_probe=lambda: Availability(False, "claude is not on PATH")
    )
    engine = next(c for c in report.checks if c.name == "coding_engine")
    assert engine.state is HealthState.NOT_CONFIGURED
    assert "not on PATH" in engine.detail


def test_a_broken_registry_is_unknown_not_a_crash():
    class Registry:
        def __len__(self):
            raise RuntimeError("boom")

    report = build_wiz_health(registry=Registry())
    check = next(c for c in report.checks if c.name == "capability_registry")
    assert check.state is HealthState.UNKNOWN


def test_the_task_engine_is_not_configured_without_a_product():
    report = build_wiz_health(product=None)
    engine = next(c for c in report.checks if c.name == "task_engine")
    assert engine.state is HealthState.NOT_CONFIGURED


def test_the_task_engine_counts_active_features():
    class Store:
        def active(self, limit=1000):
            return [1, 2, 3]

    class Pipeline:
        store = Store()

    class Product:
        pipeline = Pipeline()

    report = build_wiz_health(product=Product())
    engine = next(c for c in report.checks if c.name == "task_engine")
    assert engine.state is HealthState.HEALTHY
    assert "3" in engine.summary


def test_an_unreadable_feature_store_is_a_failure():
    class Store:
        def active(self, limit=1000):
            raise RuntimeError("disk full")

    class Pipeline:
        store = Store()

    class Product:
        pipeline = Pipeline()

    report = build_wiz_health(product=Product())
    engine = next(c for c in report.checks if c.name == "task_engine")
    assert engine.state is HealthState.FAILED


def test_the_ledger_check_reads_entries():
    class Ledger:
        def entries(self):
            return {"a": {}, "b": {}}

    report = build_wiz_health(ledger=Ledger())
    check = next(c for c in report.checks if c.name == "notification_ledger")
    assert check.state is HealthState.HEALTHY
    assert "2" in check.summary


def test_an_unreadable_ledger_is_a_failure():
    class Ledger:
        def entries(self):
            raise RuntimeError("disk on fire")

    report = build_wiz_health(ledger=Ledger())
    check = next(c for c in report.checks if c.name == "notification_ledger")
    assert check.state is HealthState.FAILED


def test_no_telegram_token_is_not_configured():
    report = build_wiz_health(telegram_bot_token="", telegram_allowed_chat_ids="")
    check = next(c for c in report.checks if c.name == "telegram")
    assert check.state is HealthState.NOT_CONFIGURED


def test_a_token_with_no_allowlist_is_degraded():
    report = build_wiz_health(telegram_bot_token="abc", telegram_allowed_chat_ids="")
    check = next(c for c in report.checks if c.name == "telegram")
    assert check.state is HealthState.DEGRADED


def test_a_configured_telegram_is_healthy():
    report = build_wiz_health(telegram_bot_token="abc", telegram_allowed_chat_ids="1")
    check = next(c for c in report.checks if c.name == "telegram")
    assert check.state is HealthState.HEALTHY


@pytest.mark.parametrize(
    "verdict,expected",
    [
        ("ONLINE", HealthState.HEALTHY),
        ("DEGRADED", HealthState.DEGRADED),
        ("OFFLINE", HealthState.NOT_CONFIGURED),
    ],
)
def test_voice_verdicts_map_onto_wiz_health(verdict, expected):
    report = build_wiz_health(voice_probe=lambda: {"voice": verdict})
    check = next(c for c in report.checks if c.name == "sir_voice")
    assert check.state is expected


def test_no_voice_probe_is_not_configured():
    report = build_wiz_health()
    check = next(c for c in report.checks if c.name == "sir_voice")
    assert check.state is HealthState.NOT_CONFIGURED


def test_a_broken_voice_probe_is_unknown():
    def boom():
        raise RuntimeError("whisper exploded")

    report = build_wiz_health(voice_probe=boom)
    check = next(c for c in report.checks if c.name == "sir_voice")
    assert check.state is HealthState.UNKNOWN


def test_a_running_scheduler_is_healthy():
    class Thread:
        def is_alive(self):
            return True

    class Scheduler:
        _thread = Thread()

        def list_tasks(self):
            return [1, 2]

    report = build_wiz_health(scheduler=Scheduler())
    check = next(c for c in report.checks if c.name == "scheduler")
    assert check.state is HealthState.HEALTHY


def test_a_configured_but_stopped_scheduler_is_a_failure():
    class Scheduler:
        _thread = None

        def list_tasks(self):
            return []

    report = build_wiz_health(scheduler=Scheduler())
    check = next(c for c in report.checks if c.name == "scheduler")
    assert check.state is HealthState.FAILED


# ---------------------------------------------------------------------------
# The overall verdict
# ---------------------------------------------------------------------------


def test_overall_is_the_worst_of_the_checks():
    class Journal:
        def verify(self):
            return False, 1

    report = build_wiz_health(journal=Journal())
    assert report.overall is HealthState.FAILED


def test_all_not_configured_is_not_configured_overall():
    """Nothing configured is not the same as everything broken."""
    report = build_wiz_health()
    assert report.overall in (HealthState.NOT_CONFIGURED, HealthState.HEALTHY)
    assert report.overall is not HealthState.FAILED


def test_troubles_lists_only_what_is_not_good_news():
    class Journal:
        def verify(self):
            return False, 2

    report = build_wiz_health(journal=Journal())
    troubles = report.troubles()
    assert any("audit_trail" in t for t in troubles)
    assert not any("coding_engine" in t for t in troubles)


def test_an_empty_report_is_not_checked():
    assert WizHealthReport(checks=[]).overall is HealthState.NOT_CHECKED
