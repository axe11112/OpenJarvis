"""One problem, one message.

Every test here is one of the messages an owner actually received on a morning
when a single failing deployment produced ten of them: one "something serious
happened" and one "the website needs you" per failing probe, across three
probes, twice over as new incident ids opened for the same fault.

The fixtures are shaped like that morning — a homepage, a login and a sign-up
probe failing within minutes of each other on one deployment SHA — because a
notification policy is only worth the history it was tested against.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openjarvis.reliability.notify import NotificationRouter, RecordingNotifier
from openjarvis.reliability.notify_ledger import NotificationLedger
from openjarvis.reliability.outage import OutageRegistry, classify_family
from openjarvis.reliability.owner_commands import (
    OwnerCommandListener,
    OwnerCommands,
    interpret,
)
from openjarvis.reliability.replay import replay
from openjarvis.reliability.types import (
    Correlation,
    Incident,
    IncidentState,
    IncidentTransition,
    Severity,
)

T0 = datetime(2026, 8, 22, 7, 12, tzinfo=timezone.utc)

#: The deployment that was actually broken that morning.
SHA = "9f31c04"

#: The reason the repair loop records when it has run out of attempts. Chosen
#: deliberately: under the new rules this is *not* by itself an owner-facing
#: ask, and several tests below depend on that.
EXHAUSTED = "3 repair attempts did not produce a verified fix"

#: A reason that does name an operator action.
DISABLED = "automatic repair is disabled for this target"


def incident(
    number: int,
    component: str,
    probe: str,
    *,
    offset: int = 0,
    kind: str = "navigation",
    severity: Severity = Severity.CRITICAL,
    state: IncidentState = IncidentState.HUMAN_REQUIRED,
    sha: str = SHA,
    title: str = "",
    status: int = 0,
    **metadata,
) -> Incident:
    """One incident, shaped like the ones the store actually holds."""
    at = (T0 + timedelta(seconds=offset)).isoformat()
    return Incident(
        fingerprint=f"fp_{component}_{kind}",
        severity=severity,
        component=component,
        title=title or f"{component} is unreachable",
        id=f"INC-{number:05d}",
        probe_id=probe,
        source="probe",
        state=state,
        created_at=at,
        last_seen_at=at,
        updated_at=at,
        correlation=Correlation(deployment_id=sha, commit_sha=sha, confidence=0.7),
        metadata={"failure_kind": kind, "http_status": status, **metadata},
    )


def router(**kwargs):
    """A real router with a recording transport. Nothing reaches a phone."""
    notifier = RecordingNotifier()
    kwargs.setdefault("ledger", NotificationLedger())
    kwargs.setdefault("outages", OutageRegistry())
    return (
        NotificationRouter(
            notifier=notifier,
            min_severity=Severity.LOW,
            dedup_window_seconds=0.0,
            critical_grace_seconds=0.0,
            redact=False,
            **kwargs,
        ),
        notifier,
    )


def escalate(route, inc, reason=DISABLED):
    return route.human_required(inc, reason=reason, attempts=3, max_attempts=3)


# ---------------------------------------------------------------------------
# 1. Related probes are one message
# ---------------------------------------------------------------------------


def test_login_and_signup_in_one_outage_send_one_message():
    """The pair the owner named first."""
    route, sent = router()
    login = incident(42, "authentication", "login", offset=0)
    signup = incident(43, "signup", "signup", offset=60)

    assert escalate(route, login) is True
    assert escalate(route, signup) is False
    assert sent.count == 1


def test_homepage_login_and_dashboard_on_one_deployment_send_one_message():
    route, sent = router()
    for inc in (
        incident(41, "website", "homepage", offset=0),
        incident(42, "authentication", "login", offset=95),
        incident(45, "dashboard", "dashboard", offset=150),
    ):
        escalate(route, inc)
    assert sent.count == 1


def test_the_one_message_names_every_subject_known_at_the_time():
    """Section 8 of the brief: one summary, not one message per probe."""
    route, sent = router()
    website = incident(41, "website", "homepage", offset=0)
    login = incident(42, "authentication", "login", offset=95)
    signup = incident(43, "signup", "signup", offset=150)

    # All three are already open when the repair loop gives up, which is what a
    # watcher tick actually looks like.
    route.outages.assign(website)
    route.outages.assign(login)
    route.outages.assign(signup)

    escalate(route, signup)
    body = sent.messages[0]["message"]
    assert "login" in body.lower()
    assert "sign-up" in body.lower()
    assert "website" in body.lower()


def test_every_probe_survives_underneath_the_single_message():
    """Correlation must never cost evidence."""
    route, sent = router()
    incidents = [
        incident(41, "website", "homepage", offset=0),
        incident(42, "authentication", "login", offset=95),
        incident(43, "signup", "signup", offset=150),
    ]
    for inc in incidents:
        escalate(route, inc)

    outages = route.outages.open_outages()
    assert len(outages) == 1
    assert sorted(outages[0].incident_ids) == ["INC-00041", "INC-00042", "INC-00043"]
    assert sorted(outages[0].probes) == ["homepage", "login", "signup"]
    assert len(outages[0].fingerprints) == 3
    assert sent.count == 1


# ---------------------------------------------------------------------------
# 2. Correlation refuses to over-reach
# ---------------------------------------------------------------------------


def test_a_database_failure_is_not_the_website_outage():
    route, sent = router()
    escalate(route, incident(41, "website", "homepage", offset=0))
    database = incident(
        50, "database", "supabase-health", offset=60, kind="http_error", status=500
    )
    database.source = "supabase"
    escalate(route, database)
    assert sent.count == 2


def test_an_auth_security_failure_is_never_folded_into_availability():
    route, sent = router()
    escalate(route, incident(41, "website", "homepage", offset=0))
    breach = incident(
        51,
        "authentication",
        "auth-guard",
        offset=60,
        kind="assertion",
        title="Unauthorized access to /admin was allowed",
    )
    assert classify_family(breach) == "auth_security"
    escalate(route, breach)
    assert sent.count == 2


def test_a_different_deployment_is_a_different_outage():
    route, sent = router()
    escalate(route, incident(41, "website", "homepage", offset=0, sha="9f31c04"))
    escalate(route, incident(60, "signup", "signup", offset=60, sha="ab77e19"))
    assert sent.count == 2


def test_a_failure_a_day_later_is_a_new_outage():
    route, sent = router()
    escalate(route, incident(41, "website", "homepage", offset=0))
    escalate(route, incident(70, "website", "homepage", offset=86400, kind="timeout"))
    assert sent.count == 2


# ---------------------------------------------------------------------------
# 3. Persistence: restarts, new ids, corruption
# ---------------------------------------------------------------------------


def test_the_same_problem_under_a_new_incident_id_says_nothing():
    """A flapping check opens a fresh incident every few minutes."""
    route, sent = router()
    escalate(route, incident(42, "authentication", "login", offset=0))
    escalate(route, incident(44, "authentication", "login", offset=300))
    assert sent.count == 1


def test_a_watcher_restart_does_not_repeat_itself(tmp_path):
    ledger, outages = tmp_path / "notified.json", tmp_path / "outages.json"

    first, sent_first = router(
        ledger=NotificationLedger(path=ledger),
        outages=OutageRegistry(path=outages),
    )
    escalate(first, incident(42, "authentication", "login", offset=0))
    assert sent_first.count == 1

    # launchd restarts the watcher: new process, new objects, same two files.
    second, sent_second = router(
        ledger=NotificationLedger(path=ledger),
        outages=OutageRegistry(path=outages),
    )
    escalate(second, incident(43, "signup", "signup", offset=120))
    escalate(second, incident(44, "authentication", "login", offset=180))
    assert sent_second.count == 0


def test_a_sleeping_mac_does_not_re_announce_the_morning(tmp_path):
    """Ten watcher cycles and two restarts over one unchanged outage."""
    ledger, outages = tmp_path / "notified.json", tmp_path / "outages.json"
    total = 0
    for cycle in range(10):
        route, sent = router(
            ledger=NotificationLedger(path=ledger),
            outages=OutageRegistry(path=outages),
        )
        for offset, (number, component, probe) in enumerate(
            ((41, "website", "homepage"), (42, "authentication", "login"))
        ):
            escalate(
                route,
                incident(number, component, probe, offset=cycle * 60 + offset * 5),
            )
        total += sent.count
    assert total == 1


def test_a_corrupt_ledger_fails_safely(tmp_path):
    """When in doubt, speak. A dedup bug must never become a missed outage."""
    path = tmp_path / "notified.json"
    path.write_text("{ this is not json", encoding="utf-8")
    route, sent = router(ledger=NotificationLedger(path=path))
    assert escalate(route, incident(42, "authentication", "login")) is True
    assert sent.count == 1


def test_a_corrupt_outage_registry_fails_safely(tmp_path):
    path = tmp_path / "outages.json"
    path.write_text("]]]not json[[[", encoding="utf-8")
    route, sent = router(outages=OutageRegistry(path=path))
    assert escalate(route, incident(42, "authentication", "login")) is True
    assert sent.count == 1


def test_a_ledger_that_raises_never_silences_the_owner():
    class Broken:
        def should_notify(self, *args, **kwargs):
            raise RuntimeError("disk on fire")

        def was_told(self, *args, **kwargs):
            raise RuntimeError("disk on fire")

        def record(self, *args, **kwargs):
            raise RuntimeError("disk on fire")

        def record_fixed(self, *args, **kwargs):
            raise RuntimeError("disk on fire")

    route, sent = router(ledger=Broken())
    assert escalate(route, incident(42, "authentication", "login")) is True
    assert sent.count == 1


# ---------------------------------------------------------------------------
# 4. Detection is never news
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "severity", [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
)
def test_an_incident_opening_says_nothing(severity):
    route, sent = router()
    assert route.alert(incident(41, "website", "homepage", severity=severity)) is False
    assert sent.count == 0


def test_critical_becoming_human_required_is_one_message():
    """ "Something serious happened" then "I need your help" — the pair."""
    route, sent = router()
    inc = incident(41, "website", "homepage", state=IncidentState.INVESTIGATING)
    route.alert(inc)
    inc.state = IncidentState.HUMAN_REQUIRED
    escalate(route, inc)
    assert sent.count == 1
    assert sent.messages[0]["message"].startswith("Sir, I need your help.")


def test_repeated_human_required_for_an_unchanged_problem_says_nothing_more():
    route, sent = router()
    inc = incident(42, "authentication", "login")
    for _ in range(12):
        escalate(route, inc)
    assert sent.count == 1


def test_an_internal_state_change_that_asks_the_same_thing_says_nothing():
    route, sent = router()
    inc = incident(42, "authentication", "login", state=IncidentState.HUMAN_REQUIRED)
    escalate(route, inc)
    inc.state = IncidentState.FAILED
    escalate(route, inc)
    inc.state = IncidentState.RECOVERY_REQUIRED
    escalate(route, inc)
    assert sent.count == 1


def test_a_worse_severity_alone_does_not_re_ask():
    """Section 4: only a change in what the owner must do is news."""
    route, sent = router()
    inc = incident(42, "authentication", "login", severity=Severity.HIGH)
    escalate(route, inc)
    inc.severity = Severity.CRITICAL
    escalate(route, inc)
    assert sent.count == 1


def test_a_materially_different_ask_does_reach_the_owner():
    """The other half: when the required action changes, say so."""
    route, sent = router()
    inc = incident(42, "authentication", "login")
    escalate(route, inc, reason=DISABLED)
    escalate(route, inc, reason="the change touched a protected path: src/auth.ts")
    assert sent.count == 2
    assert "protected files" in sent.messages[1]["message"]


# ---------------------------------------------------------------------------
# 5. An escalation must ask for something
# ---------------------------------------------------------------------------


def test_no_specific_action_means_no_telegram():
    """ "I could not fix it" is a status, not a request."""
    route, sent = router()
    inc = incident(
        80, "billing", "invoice-render", kind="assertion", severity=Severity.MEDIUM
    )
    assert escalate(route, inc, reason=EXHAUSTED) is False
    assert sent.count == 0


def test_a_withheld_escalation_is_visible_in_control_center():
    """Silence has to be explainable, or it is indistinguishable from a bug."""
    route, _ = router()
    inc = incident(
        80, "billing", "invoice-render", kind="assertion", severity=Severity.MEDIUM
    )
    escalate(route, inc, reason=EXHAUSTED)
    recorded = inc.metadata["owner_ask"]
    assert recorded["actionable"] is False
    assert recorded["parked_reason"]


def test_a_flapping_check_never_wakes_the_owner():
    route, sent = router()
    inc = incident(41, "website", "homepage")
    assert (
        escalate(route, inc, reason="the check is flapping: 6 changes in 5 min")
        is False
    )
    assert sent.count == 0


def test_a_real_outage_with_no_named_action_still_reaches_the_owner():
    """The line that must not be crossed while removing noise.

    An exhausted repair on a MEDIUM contract failure parks. An exhausted repair
    while users cannot reach the site is a decision only the owner can make,
    and refusing to ask would be hiding an outage rather than quietening one.
    """
    route, sent = router()
    assert (
        escalate(route, incident(41, "website", "homepage"), reason=EXHAUSTED) is True
    )
    assert "roll" in sent.messages[0]["message"].lower()


def test_every_escalation_names_an_action():
    """The property, not an example of it."""
    route, sent = router()
    for reason in (
        DISABLED,
        "the change touched a protected path: src/auth.ts",
        "post-merge: production did not verify",
        "a secret was found in the change",
        "the change exceeded the allowed scope",
        EXHAUSTED,
    ):
        route, sent = router()
        inc = incident(41, "website", "homepage")
        if escalate(route, inc, reason=reason):
            ask = inc.metadata["owner_ask"]
            assert ask["action"].strip(), reason
            assert ask["action"] in sent.messages[0]["message"]


def test_an_ask_carries_the_structured_context_for_control_center():
    route, _ = router()
    inc = incident(41, "website", "homepage", occurrences=4)
    inc.occurrences = 4
    escalate(route, inc, reason=EXHAUSTED)
    ask = inc.metadata["owner_ask"]
    assert ask["what_failed"]
    assert ask["evidence"]
    assert ask["why_blocked"]
    assert ask["action"]


# ---------------------------------------------------------------------------
# 6. Success is told once, and only to somebody who was told
# ---------------------------------------------------------------------------


def test_a_transient_failure_nobody_heard_about_says_nothing():
    route, sent = router()
    inc = incident(90, "website", "homepage", kind="timeout", severity=Severity.MEDIUM)
    assert route.recovered(inc) is False
    assert sent.count == 0


def test_an_outage_the_owner_was_told_about_gets_exactly_one_success():
    route, sent = router()
    inc = incident(41, "website", "homepage")
    escalate(route, inc)
    inc.state = IncidentState.RESOLVED
    assert route.resolved(inc) is True
    assert sent.count == 2
    assert sent.messages[1]["message"].startswith("Sir, I fixed the issue.")


def test_recovered_resolved_and_verified_do_not_become_three_messages():
    """Three internal routes to "it works again", one owner-facing fact."""
    route, sent = router()
    inc = incident(41, "website", "homepage")
    escalate(route, inc)
    inc.state = IncidentState.RESOLVED
    route.resolved(inc)
    route.production_verified(inc, record=object(), result=object())
    route.recovered(inc)
    assert sent.count == 2  # the ask, then one success


def test_a_second_probe_recovering_does_not_send_a_second_success():
    route, sent = router()
    website = incident(41, "website", "homepage", offset=0)
    login = incident(42, "authentication", "login", offset=60)
    escalate(route, website)
    escalate(route, login)
    website.state = IncidentState.RESOLVED
    login.state = IncidentState.RESOLVED
    route.resolved(website)
    route.resolved(login)
    assert sent.count == 2  # one ask, one "it's fixed"


def test_breaking_again_after_a_fix_is_news_again():
    route, sent = router()
    inc = incident(41, "website", "homepage")
    escalate(route, inc)
    inc.state = IncidentState.RESOLVED
    route.resolved(inc)
    inc.state = IncidentState.HUMAN_REQUIRED
    escalate(route, inc)
    assert sent.count == 3


# ---------------------------------------------------------------------------
# 7. Transient and observer-side problems
# ---------------------------------------------------------------------------


def test_a_latency_only_failure_is_never_an_owner_ask():
    route, sent = router()
    inc = incident(91, "website", "homepage", kind="duration", severity=Severity.HIGH)
    assert (
        escalate(route, inc, reason="latency budget overrun on a busy machine") is False
    )
    assert sent.count == 0


def test_an_observer_degraded_failure_is_never_an_owner_ask():
    route, sent = router()
    inc = incident(92, "website", "homepage", kind="duration")
    assert escalate(route, inc, reason="the observer machine looked degraded") is False
    assert sent.count == 0


def test_a_probe_that_fails_and_recovers_sends_nothing_at_all():
    route, sent = router()
    inc = incident(93, "website", "homepage", kind="timeout", severity=Severity.HIGH)
    route.alert(inc)
    inc.state = IncidentState.RESOLVED
    route.recovered(inc)
    assert sent.count == 0


# ---------------------------------------------------------------------------
# 8. Owner commands
# ---------------------------------------------------------------------------


class _Gate:
    def __init__(self):
        self.cleared = []

    def clear_cooldown(self, *keys):
        self.cleared.extend(k for k in keys if k)
        return list(keys)


def _commands(registry, **kwargs):
    kwargs.setdefault("allowed_chat_ids", "555")
    kwargs.setdefault("gate", _Gate())
    return OwnerCommands(outages=registry, **kwargs)


def test_fix_it_from_the_owner_acknowledges_once_and_resumes():
    route, sent = router()
    escalate(route, incident(41, "website", "homepage"))
    commands = _commands(route.outages)

    result = commands.handle(chat_id="555", text="Fix it")
    assert result.executed is True
    assert result.reply == "Sir, I'm working on it."
    assert result.resumed


def test_fix_it_produces_no_notification_storm():
    route, sent = router()
    escalate(route, incident(41, "website", "homepage"))
    before = sent.count
    commands = _commands(route.outages)
    for _ in range(5):
        commands.handle(chat_id="555", text="fix it")
    assert sent.count == before  # the acknowledgement is not a notification


def test_fix_it_between_two_independent_problems_asks_one_question():
    route, _ = router()
    escalate(route, incident(41, "website", "homepage", offset=0))
    database = incident(
        50, "database", "db-health", offset=60, kind="http_error", status=500
    )
    database.source = "supabase"
    escalate(route, database)

    result = _commands(route.outages).handle(chat_id="555", text="fix it")
    assert result.ambiguous is True
    assert result.executed is False
    assert result.reply.count("?") == 1


def test_an_unauthorized_sender_cannot_issue_fix_it():
    route, _ = router()
    escalate(route, incident(41, "website", "homepage"))
    gate = _Gate()
    commands = _commands(route.outages, gate=gate, allowed_chat_ids="555")

    result = commands.handle(chat_id="999", text="fix it")
    assert result.authorized is False
    assert result.executed is False
    assert result.reply == ""  # a stranger is not answered at all
    assert gate.cleared == []


def test_an_empty_allowlist_authorises_nobody():
    route, _ = router()
    escalate(route, incident(41, "website", "homepage"))
    commands = _commands(route.outages, allowed_chat_ids="")
    assert commands.handle(chat_id="555", text="fix it").authorized is False


@pytest.mark.parametrize(
    "text", ["don't fix it", "do not fix it", "stop fixing it", "leave it for now"]
)
def test_a_negated_instruction_is_never_read_as_fix_it(text):
    assert interpret(text) == ""


def test_fix_it_touches_nothing_but_the_cooldown():
    """The whole of what an owner message may change.

    Enforced by a gate that refuses every attribute except the one method:
    reading ``blocked``, calling ``unblock``, raising ``max_concurrent`` or
    reaching for anything else fails the test rather than quietly working.
    """

    class _StrictGate:
        def __init__(self):
            self.cleared = []

        def clear_cooldown(self, *keys):
            self.cleared.extend(k for k in keys if k)
            return list(keys)

        def __getattr__(self, name):  # pragma: no cover - the assertion is the point
            raise AssertionError(f"an owner message must not reach RepairGate.{name}")

    route, _ = router()
    escalate(route, incident(41, "website", "homepage"))
    gate = _StrictGate()
    result = _commands(route.outages, gate=gate).handle(chat_id="555", text="fix it")
    assert result.executed is True
    assert gate.cleared


def test_an_unrecognised_message_is_answered_with_silence():
    route, _ = router()
    result = _commands(route.outages).handle(chat_id="555", text="thanks!")
    assert result.reply == ""
    assert result.executed is False


def test_the_listener_never_raises_into_the_channel_thread():
    class _Channel:
        def __init__(self):
            self.sent = []
            self.handler = None

        def on_message(self, handler):
            self.handler = handler

        def connect(self):
            return None

        def send(self, chat_id, text):
            self.sent.append((chat_id, text))

    class _Transport:
        def __init__(self, channel):
            self.channel = channel

    route, _ = router()
    escalate(route, incident(41, "website", "homepage"))
    channel = _Channel()
    listener = OwnerCommandListener(
        commands=_commands(route.outages), notifier=_Transport(channel)
    )
    assert listener.start() is True

    message = type(
        "M", (), {"conversation_id": "555", "sender": "555", "content": "Fix it"}
    )()
    channel.handler(message)
    assert channel.sent == [("555", "Sir, I'm working on it.")]

    # A message that makes the handler explode must not escape into the
    # channel's polling thread and kill it.
    channel.handler(object())


# ---------------------------------------------------------------------------
# 9. Nothing sensitive reaches a phone
# ---------------------------------------------------------------------------


def test_a_credential_in_the_evidence_never_reaches_the_message():
    notifier = RecordingNotifier()
    route = NotificationRouter(
        notifier=notifier,
        min_severity=Severity.LOW,
        dedup_window_seconds=0.0,
        critical_grace_seconds=0.0,
        ledger=NotificationLedger(),
        outages=OutageRegistry(),
    )
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    inc = incident(41, "website", "homepage")
    inc.metadata["handover"] = {"cause": f"the deploy hook used {token}"}
    escalate(route, inc, reason=EXHAUSTED)
    assert notifier.count == 1
    assert token not in notifier.messages[0]["message"]


def test_the_message_carries_no_internal_vocabulary():
    route, sent = router()
    escalate(route, incident(41, "website", "homepage"), reason=EXHAUSTED)
    body = sent.messages[0]["message"].lower()
    for jargon in ("fingerprint", "outage_key", "otg_", "human_required", "severity"):
        assert jargon not in body


# ---------------------------------------------------------------------------
# 10. The whole morning, replayed
# ---------------------------------------------------------------------------


def _the_morning():
    """The five incidents that produced ten messages."""
    incidents = []
    for number, component, probe, offset, kind in (
        (41, "website", "homepage", 0, "navigation"),
        (42, "authentication", "login", 95, "navigation"),
        (43, "signup", "signup", 150, "navigation"),
        (44, "authentication", "login", 420, "timeout"),
        (45, "dashboard", "dashboard", 480, "navigation"),
    ):
        inc = incident(
            number,
            component,
            probe,
            offset=offset,
            kind=kind,
            state=IncidentState.DETECTED,
        )
        inc.transitions.append(
            IncidentTransition(
                from_state=IncidentState.DETECTED,
                to_state=IncidentState.HUMAN_REQUIRED,
                actor="jarvis",
                reason=EXHAUSTED,
            )
        )
        inc.state = IncidentState.HUMAN_REQUIRED
        incidents.append(inc)
    return incidents


def test_the_morning_that_produced_ten_messages_now_produces_one():
    outcome = replay(_the_morning())
    assert outcome["before"].count == 10
    assert outcome["after"].count == 1
    assert outcome["after"].outages == 1


def test_the_replay_sends_nothing():
    """The tool that measures the policy must not exercise the transport."""
    outcome = replay(_the_morning())
    assert all(entry["message"] for entry in outcome["after"].messages)


def test_the_replay_does_not_mutate_the_incidents_it_reads():
    incidents = _the_morning()
    replay(incidents)
    assert all("outage_key" not in (i.metadata or {}) for i in incidents)
