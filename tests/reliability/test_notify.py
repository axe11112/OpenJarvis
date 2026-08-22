"""Tests for notification rendering, redaction and routing."""

from __future__ import annotations

import pytest

from openjarvis.reliability.notify import (
    ConsoleNotifier,
    NotificationRouter,
    TelegramNotifier,
    render_alert,
    render_human_required,
    render_post_merge_failed,
    render_production_verified,
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


#: Internal vocabulary that must never reach the owner's phone. Every one of
#: these is a word this codebase uses correctly and constantly, which is exactly
#: why the guard is a test rather than a style note.
JARGON = (
    "fingerprint",
    "verification gate",
    "state transition",
    "deployment lineage",
    "scope violation",
    "reproduction contract",
    "verifier authority",
    "health vocabulary",
    "probe fleet",
    "incident lifecycle",
    "combined_status",
)

#: Jargon that is only jargon in its own spelling.
#:
#: "resolved" is a perfectly good English word and is the owner's own wording for
#: a finished repair; ``RESOLVED`` is a state name being read aloud. Matching
#: these case-sensitively keeps the check on the property that matters — no
#: identifier from the state machine or the GitHub API reaches a phone — without
#: banning the vocabulary a person would actually use.
JARGON_TOKENS = (
    "HUMAN_REQUIRED",
    "RECOVERY_REQUIRED",
    "ROLLED_BACK",
    "RESOLVED",
    "MERGED",
    "SHA",
)


def _assert_owner_readable(text: str) -> None:
    """Every message the owner receives has to survive this."""
    assert text.startswith("Sir,"), f"must open with 'Sir,': {text[:40]!r}"
    assert not text.startswith("JARVIS"), "must not open with the bot's own name"
    lowered = text.lower()
    for word in JARGON:
        assert word.lower() not in lowered, f"internal jargon leaked: {word!r}"
    for token in JARGON_TOKENS:
        assert token not in text, f"an internal identifier leaked: {token!r}"
    assert len(text.splitlines()) <= 6, f"too long for a phone:\n{text}"


class TestTemplates:
    def test_alert_is_short_and_plain(self):
        text = render_alert(_incident())
        _assert_owner_readable(text)
        assert "something serious happened" in text
        assert "Login is down." in text

    def test_alert_without_persona_drops_the_address(self):
        text = render_alert(_incident(), persona=False)
        assert "Sir," not in text
        assert "Login is down." in text

    def test_alert_names_the_thing_not_the_component(self):
        """The owner knows what login is. "authentication" is our word."""
        assert "Login" in render_alert(_incident(component="authentication"))
        assert "The database" in render_alert(_incident(component="supabase"))

    def test_resolved_is_one_short_success_message(self):
        incident = _incident()
        incident.resolution.root_cause = "the callback dropped the session cookie"
        incident.resolution.pr_url = "https://github.com/x/y/pull/123"
        text = render_resolved(
            incident,
            attempt=RepairAttempt(number=1, diff_stat="1 file changed"),
            verification=VerificationResult(
                passed=True, probe_id="auth-login", target_url="https://preview"
            ),
        )
        _assert_owner_readable(text)
        assert text.splitlines()[0] == "Sir, I fixed the issue."
        assert (
            "Login was failing because the callback dropped the session cookie." in text
        )
        assert "PR #123 is ready" in text
        # None of the machinery that produced the fix belongs in the message.
        assert "auth-login" not in text
        assert "https://preview" not in text
        assert "1 file changed" not in text

    def test_resolved_without_a_known_cause_does_not_invent_one(self):
        text = render_resolved(_incident())
        _assert_owner_readable(text)
        assert "Login was failing." in text
        assert "because" not in text

    def test_resolved_drops_an_over_long_root_cause(self):
        """A paragraph of analysis is not a phone message."""
        incident = _incident()
        incident.resolution.root_cause = "x" * 400
        text = render_resolved(incident)
        _assert_owner_readable(text)
        assert "x" * 20 not in text

    def test_human_required_says_what_to_do(self):
        text = render_human_required(
            _incident(),
            reason="verification failed 3 times; attempts exhausted",
            attempts=3,
            max_attempts=3,
        )
        _assert_owner_readable(text)
        assert text.splitlines()[0] == "Sir, I need your help."
        assert "I stopped making changes." in text
        assert "3/3" not in text, "attempt counts are dashboard detail"

    @pytest.mark.parametrize(
        "reason,expected",
        [
            ("the change touches protected path(s): x", "not allowed to change"),
            ("a secret was found in the diff", "looked like a password"),
            ("attempts exhausted", "could not fix it safely"),
            ("the check is flapping", "keeps coming back"),
            ("something nobody anticipated", "could not fix it safely"),
        ],
    )
    def test_escalation_reasons_are_translated(self, reason, expected):
        text = render_human_required(
            _incident(), reason=reason, attempts=3, max_attempts=3
        )
        _assert_owner_readable(text)
        assert expected in text

    def test_rolled_back(self):
        text = render_rolled_back(_incident(), reason="regression detected")
        _assert_owner_readable(text)
        assert "rolled a change back" in text


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

    def test_a_guard_that_silently_does_nothing_is_not_trusted(self, monkeypatch):
        """The failure that actually happened, and raised nothing.

        ``BoundaryGuard`` disables its scanners and returns the text unchanged
        when the Rust extension is missing. That is not an exception, so the
        fallback above never ran and the token went out verbatim. Both layers
        run now, so a pass-through guard costs nothing.
        """
        import openjarvis.security.boundary as boundary_module

        class _PassThrough:
            def __init__(self, *a, **k):
                pass

            def scan_outbound(self, message, destination=""):
                return message

        monkeypatch.setattr(boundary_module, "BoundaryGuard", _PassThrough)
        notifier = ConsoleNotifier()
        router = NotificationRouter(notifier=notifier, min_severity=Severity.LOW)
        router.notify("token ghp_" + "c" * 36, severity=Severity.HIGH)
        assert "ghp_" + "c" * 36 not in notifier.sent[0]

    def test_the_body_is_withheld_when_nothing_can_redact_it(self, monkeypatch):
        """Silence beats a credential when every layer is unavailable."""
        import openjarvis.security.boundary as boundary_module
        import openjarvis.security.credential_stripper as stripper_module

        class _Exploding:
            def __init__(self, *a, **k):
                raise RuntimeError("unavailable")

        monkeypatch.setattr(boundary_module, "BoundaryGuard", _Exploding)
        monkeypatch.setattr(stripper_module, "CredentialStripper", _Exploding)
        notifier = ConsoleNotifier()
        router = NotificationRouter(notifier=notifier, min_severity=Severity.LOW)
        router.notify("token ghp_" + "d" * 36, severity=Severity.HIGH)
        assert "ghp_" + "d" * 36 not in notifier.sent[0]
        assert "withheld" in notifier.sent[0]


class TestIncidentHelpers:
    def _router(self, **kwargs):
        notifier = ConsoleNotifier()
        kwargs.setdefault("min_severity", Severity.LOW)
        kwargs.setdefault("clock", _FakeClock())
        return NotificationRouter(notifier=notifier, **kwargs), notifier

    def test_a_critical_incident_alerts(self):
        router, notifier = self._router(
            critical_grace_seconds=0, alert_on_critical=True
        )
        assert router.alert(_incident(severity=Severity.CRITICAL))
        _assert_owner_readable(notifier.sent[0])

    def test_resolved(self):
        router, notifier = self._router()
        router.resolved(
            _incident(), verification=VerificationResult(passed=True, probe_id="p")
        )
        assert "Sir, I fixed the issue." in notifier.sent[0]

    def test_human_required_is_always_critical(self):
        """An escalation the owner never sees is the same as no escalation."""
        router, notifier = self._router(
            min_severity=Severity.CRITICAL, max_per_hour=1, dedup_window_seconds=0
        )
        router.notify("filler", severity=Severity.CRITICAL)
        assert router.human_required(
            _incident(severity=Severity.LOW),
            reason="repair is disabled",
            attempts=3,
            max_attempts=3,
        )
        assert "Sir, I need your help." in notifier.sent[-1]

    def test_rolled_back(self):
        router, notifier = self._router()
        router.rolled_back(_incident(), reason="regression")
        assert "rolled a change back" in notifier.sent[0]

    @pytest.mark.parametrize("persona", [True, False])
    def test_persona_flag_is_honoured(self, persona):
        router, notifier = self._router(
            persona=persona, critical_grace_seconds=0, alert_on_critical=True
        )
        router.alert(_incident(severity=Severity.CRITICAL))
        assert ("Sir," in notifier.sent[0]) is persona


# ---------------------------------------------------------------------------
# One event, one message
# ---------------------------------------------------------------------------


class _ManualTimer:
    """A threading.Timer stand-in that only fires when a test says so."""

    created: list = []

    def __init__(self, delay, function):
        self.delay = delay
        self.function = function
        self.cancelled = False
        self.daemon = False
        _ManualTimer.created.append(self)

    def start(self):
        return None

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.function()


class TestCriticalAndEscalationAreOneEvent:
    """A CRITICAL that immediately escalates must not be told twice.

    Both messages describe the same thing seconds apart; the escalation is
    strictly more useful because it says what the owner has to do. So the alert
    is held briefly and dropped when the escalation arrives — but a CRITICAL
    that nothing supersedes must still get through, which is the other half of
    the requirement and the easier half to break.
    """

    def _router(self, **kwargs):
        _ManualTimer.created = []
        notifier = ConsoleNotifier()
        return (
            NotificationRouter(
                notifier=notifier,
                min_severity=Severity.LOW,
                dedup_window_seconds=0,
                clock=_FakeClock(),
                scheduler=_ManualTimer,
                # This class is about the deferral machinery, which only runs
                # when an operator has deliberately turned detection alerts on.
                alert_on_critical=kwargs.pop("alert_on_critical", True),
                **kwargs,
            ),
            notifier,
        )

    def test_an_immediate_escalation_replaces_the_alert(self):
        router, notifier = self._router()
        incident = _incident(severity=Severity.CRITICAL)

        router.alert(incident)
        assert notifier.sent == [], "the alert waits to see what happens next"

        router.human_required(
            incident, reason="repair is disabled", attempts=3, max_attempts=3
        )
        # The held timer must have been cancelled, not merely ignored.
        assert all(t.cancelled for t in _ManualTimer.created)
        for timer in _ManualTimer.created:
            timer.fire()

        assert len(notifier.sent) == 1, f"expected one message, got {notifier.sent}"
        assert "I need your help" in notifier.sent[0]

    def test_a_standalone_critical_still_notifies(self):
        """The half that is easy to break while fixing the duplicate."""
        router, notifier = self._router()
        router.alert(_incident(severity=Severity.CRITICAL))
        assert notifier.sent == []

        for timer in _ManualTimer.created:
            timer.fire()

        assert len(notifier.sent) == 1
        assert "something serious happened" in notifier.sent[0]

    def test_a_post_merge_failure_also_supersedes_the_alert(self):
        router, notifier = self._router()
        incident = _incident(severity=Severity.CRITICAL)
        record = type("M", (), {"pr_number": 210, "merge_commit_sha": "e" * 40})()
        result = _result(verified=False, rule="fleet_failed")

        router.alert(incident)
        router.post_merge_failed(incident, record=record, result=result)
        for timer in _ManualTimer.created:
            timer.fire()

        assert len(notifier.sent) == 1
        assert "I need your help" in notifier.sent[0]

    def test_escalating_a_different_incident_does_not_swallow_the_alert(self):
        """Superseding is per incident. Two genuinely separate problems are two
        messages, and collapsing them would hide one."""
        router, notifier = self._router()
        first = _incident(
            severity=Severity.CRITICAL, id="INC-00001", fingerprint="fp-login"
        )
        second = _incident(
            severity=Severity.CRITICAL,
            id="INC-00002",
            fingerprint="fp-billing",
            component="billing",
        )

        router.alert(first)
        router.human_required(
            second, reason="repair is disabled", attempts=3, max_attempts=3
        )
        for timer in _ManualTimer.created:
            timer.fire()

        assert len(notifier.sent) == 2

    def test_flush_sends_anything_still_held(self):
        """A shutdown must not swallow a CRITICAL that was merely waiting."""
        router, notifier = self._router()
        router.alert(_incident(severity=Severity.CRITICAL))
        router.flush()
        assert len(notifier.sent) == 1


# ---------------------------------------------------------------------------
# What the owner is NOT told
# ---------------------------------------------------------------------------


class TestSilencePolicy:
    """Most of what JARVIS does is not news, and that is the feature.

    Each of these used to send a message. Together they were six notifications
    per incident, which is how an owner learns to swipe JARVIS away — and then
    misses the one that mattered.
    """

    def _router(self, **kwargs):
        notifier = ConsoleNotifier()
        kwargs.setdefault("min_severity", Severity.LOW)
        kwargs.setdefault("clock", _FakeClock())
        return NotificationRouter(notifier=notifier, **kwargs), notifier

    @pytest.mark.parametrize("severity", [Severity.LOW, Severity.MEDIUM, Severity.HIGH])
    def test_an_ordinary_incident_opening_says_nothing(self, severity):
        router, notifier = self._router()
        assert router.alert(_incident(severity=severity)) is False
        assert notifier.sent == []

    def test_a_repair_starting_says_nothing(self):
        router, notifier = self._router()
        assert router.progress(_incident(), attempt=1, max_attempts=3) is False
        assert notifier.sent == []

    def test_a_self_healing_incident_says_nothing(self):
        router, notifier = self._router()
        assert router.recovered(_incident()) is False
        assert notifier.sent == []

    def test_a_merge_starting_and_landing_says_nothing(self):
        router, notifier = self._router()
        record = type("R", (), {"merged": True, "pr_number": 7})()
        assert (
            router.merge_attempt(
                _incident(), pr_number=7, head_sha="a" * 40, method="squash"
            )
            is False
        )
        assert router.merge_outcome(_incident(), record=record) is False
        assert notifier.sent == []

    def test_a_refused_merge_says_nothing(self):
        """The gates declining is the system working, not an incident."""
        router, notifier = self._router()
        record = type("R", (), {"merged": False, "pr_number": 7})()
        assert router.merge_outcome(_incident(), record=record) is False
        assert notifier.sent == []

    def test_production_deployment_and_verification_start_say_nothing(self):
        router, notifier = self._router()
        observation = type("O", (), {"deployment_id": "dpl_1", "state": "READY"})()
        assert (
            router.production_deployment(_incident(), observation=observation) is False
        )
        assert (
            router.production_verification_started(
                _incident(), observation=observation, target_url="https://x"
            )
            is False
        )
        assert notifier.sent == []

    def test_a_whole_quiet_incident_produces_no_messages(self):
        """Detected, repaired, verified, merged, deployed — one message at the
        end, and nothing before it."""
        router, notifier = self._router()
        incident = _incident(severity=Severity.HIGH)
        record = type("R", (), {"merged": True, "pr_number": 7})()
        observation = type("O", (), {"deployment_id": "dpl_1", "state": "READY"})()

        router.alert(incident)
        router.progress(incident, attempt=1, max_attempts=3)
        router.merge_attempt(incident, pr_number=7, head_sha="a" * 40, method="squash")
        router.merge_outcome(incident, record=record)
        router.production_deployment(incident, observation=observation)
        router.production_verification_started(incident, observation=observation)
        assert notifier.sent == [], "nothing before the outcome"

        router.resolved(incident)
        assert len(notifier.sent) == 1, "exactly one message, at the end"
        _assert_owner_readable(notifier.sent[0])


# ---------------------------------------------------------------------------
# The live-mode outcomes
# ---------------------------------------------------------------------------


def _observation(**overrides):
    facts = {"deployment_id": "dpl_aNDR9i1G", "state": "READY", "commit_sha": "e" * 40}
    facts.update(overrides)
    return type("O", (), facts)()


def _result(**overrides):
    facts = {
        "verified": True,
        "reason": "production verified",
        "rule": "verified",
        "deployment": _observation(),
        "reproduction": type("P", (), {"probe_id": "auth-login", "passed": True})(),
        "fleet": [],
        "failures": [],
    }
    facts.update(overrides)
    return type("R", (), facts)()


class TestProductionOutcomeMessages:
    def test_a_live_fix_is_two_lines_in_the_owners_own_words(self):
        """The end of a long sequence, said in the shortest true way.

        Problem found, repaired, four check suites, a preview, a merge, a
        production deployment, the original reproduction and the whole probe
        fleet re-run against production. None of that belongs on a phone, and
        neither does the component or the cause: there is nothing here for the
        owner to decide.
        """
        incident = _incident()
        incident.resolution.root_cause = "the callback dropped the session cookie"
        record = type("M", (), {"pr_number": 210, "merge_commit_sha": "e" * 40})()
        text = render_production_verified(incident, record=record, result=_result())

        _assert_owner_readable(text)
        assert text == (
            "Sir, it's fixed.\n"
            "The issue is resolved and everything is working normally."
        )
        # The identifiers that made this true stay in the dashboard.
        assert "dpl_aNDR9i1G" not in text
        assert "e" * 12 not in text
        assert "210" not in text

    def test_a_bad_deployment_asks_for_help_without_the_forensics(self):
        incident = _incident()
        record = type("M", (), {"pr_number": 210, "merge_commit_sha": "e" * 40})()
        result = _result(
            verified=False,
            rule="fleet_failed",
            reason="production probe(s) failed after the merge: signup",
            failures=[type("P", (), {"probe_id": "signup", "summary": "no form"})()],
        )
        text = render_post_merge_failed(incident, record=record, result=result)

        _assert_owner_readable(text)
        assert text.splitlines()[0] == "Sir, I need your help."
        assert "still fails" in text
        assert "I stopped making changes." in text
        assert "PR #210" in text
        assert "dpl_aNDR9i1G" not in text

    @pytest.mark.parametrize(
        "rule,expected",
        [
            ("deployment_missing", "never went live"),
            ("deployment_not_ready", "the deployment failed"),
            ("reproduction_failed", "still fails"),
        ],
    )
    def test_the_failure_says_which_way_it_went_wrong(self, rule, expected):
        record = type("M", (), {"pr_number": 210, "merge_commit_sha": "e" * 40})()
        text = render_post_merge_failed(
            _incident(), record=record, result=_result(verified=False, rule=rule)
        )
        _assert_owner_readable(text)
        assert expected in text

    def test_a_post_merge_failure_is_critical_and_beats_the_rate_cap(self):
        """The one message that must never be dropped: code is live and wrong."""
        notifier = ConsoleNotifier()
        router = NotificationRouter(
            notifier=notifier,
            min_severity=Severity.CRITICAL,
            max_per_hour=1,
            dedup_window_seconds=0,
            clock=_FakeClock(),
        )
        router.notify("filler", severity=Severity.CRITICAL)
        record = type("M", (), {"pr_number": 210, "merge_commit_sha": "e" * 40})()
        assert router.post_merge_failed(
            _incident(severity=Severity.LOW),
            record=record,
            result=_result(verified=False, rule="fleet_failed"),
        )
        assert "Sir, I need your help." in notifier.sent[-1]

    def test_a_live_success_is_the_only_message_of_its_run(self):
        notifier = ConsoleNotifier()
        router = NotificationRouter(
            notifier=notifier, min_severity=Severity.LOW, clock=_FakeClock()
        )
        incident = _incident(severity=Severity.HIGH)
        record = type("M", (), {"pr_number": 210, "merge_commit_sha": "e" * 40})()

        router.alert(incident)
        router.merge_outcome(incident, record=record)
        router.production_deployment(incident, observation=_observation())
        router.production_verification_started(incident, observation=_observation())
        router.production_verified(incident, record=record, result=_result())

        assert len(notifier.sent) == 1
        assert notifier.sent[0].startswith("Sir, it's fixed.")
