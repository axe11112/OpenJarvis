"""Wiz's own health, kept apart from the health of the thing it watches.

The distinction is the one thing this module exists to protect, and it is easy
to lose by accident: most of what Wiz checks about itself — can I reach GitHub,
is there a Claude CLI, is the Telegram bot token valid — reads exactly like a
check about the *product*, and a report that mixes the two eventually says
"Wize is down" because Wiz's own watcher crashed, or says "everything is fine"
because a probe that would have caught the real outage never ran.

So the rule is simple to state and easy to violate without a module enforcing
it: **nothing in here may consult an incident, a probe result, or the site's own
uptime.** Every check is a question about Wiz — is the coding tool on this
machine, is the watcher process alive, can this process still write its own
records — and the one question this module refuses to answer is "is the
website up", because that question belongs to
:class:`~openjarvis.reliability.diagnostic.LiveDiagnostic` and to nowhere else.

Reuses the vocabulary reliability already built for exactly this problem —
:class:`~openjarvis.reliability.health.HealthState`,
:class:`~openjarvis.reliability.health.CheckResult` — rather than inventing a
second one. ``NOT_CONFIGURED`` still outranks ``HEALTHY`` here for the same
reason it does there: an unconfigured integration averaged into a green overall
score is exactly the blind spot both modules exist to prevent.

Every check is a probe, injected rather than reached for at import time, so a
test can prove "the watcher being down is reported, and does not touch the
website" without launchd, a real Claude CLI, or a Telegram bot existing on the
machine running the test.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from openjarvis.reliability.health import CheckResult, HealthState, worst

logger = logging.getLogger(__name__)

__all__ = ["WizHealthReport", "build_wiz_health", "default_checks"]


@dataclass
class WizHealthReport:
    """Every check, and one honest verdict derived from them.

    ``overall`` is never better than the worst check — see
    :func:`~openjarvis.reliability.health.worst` — and a check that was never
    run because nothing was configured for it counts as ``NOT_CONFIGURED``
    rather than being silently excluded from the average.
    """

    checks: List[CheckResult] = field(default_factory=list)

    @property
    def overall(self) -> HealthState:
        return (
            worst([c.state for c in self.checks])
            if self.checks
            else HealthState.NOT_CHECKED
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the Control Center and for `wiz.health`."""
        return {
            "overall": self.overall.value,
            "checks": [
                {
                    "name": c.name,
                    "state": c.state.value,
                    "summary": c.summary,
                    "detail": c.detail,
                    "remediation": c.remediation,
                }
                for c in self.checks
            ],
        }

    def troubles(self) -> List[str]:
        """One line per check that is not simply healthy, for a phone."""
        return [
            f"{c.name}: {c.summary or c.detail or c.state.value.lower()}"
            for c in self.checks
            if not c.state.is_good_news
        ]


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _journal_check(journal: Any) -> CheckResult:
    result = CheckResult(name="audit_trail")
    if journal is None:
        result.state = HealthState.NOT_CONFIGURED
        result.detail = "no journal is attached"
        return result
    try:
        intact, break_at = journal.verify()
    except Exception as exc:  # noqa: BLE001 - a broken verifier is UNKNOWN, not a crash
        result.state = HealthState.UNKNOWN
        result.detail = f"could not verify the journal: {exc}"
        return result
    if intact:
        result.state = HealthState.HEALTHY
        result.summary = "every recorded decision checks out"
    else:
        result.state = HealthState.FAILED
        result.summary = f"the hash chain breaks at entry {break_at}"
        result.remediation = "the journal file has been altered or truncated"
    return result


def _capability_registry_check(registry: Any) -> CheckResult:
    result = CheckResult(name="capability_registry")
    if registry is None:
        result.state = HealthState.NOT_CONFIGURED
        result.detail = "no registry is attached"
        return result
    try:
        declared = len(registry)
        available = sum(1 for s in registry.describe() if s.get("configured"))
    except Exception as exc:  # noqa: BLE001
        result.state = HealthState.UNKNOWN
        result.detail = f"could not read the registry: {exc}"
        return result
    result.state = HealthState.HEALTHY
    result.summary = f"{available}/{declared} declared capabilities are configured"
    return result


def _coding_engine_check(probe: Optional[Callable[[], Any]]) -> CheckResult:
    result = CheckResult(name="coding_engine")
    if probe is None:
        result.state = HealthState.NOT_CONFIGURED
        result.detail = "no probe is attached"
        return result
    try:
        availability = probe()
    except Exception as exc:  # noqa: BLE001
        result.state = HealthState.UNKNOWN
        result.detail = f"could not check the coding engine: {exc}"
        return result
    result.detail = str(getattr(availability, "detail", "") or "")
    if getattr(availability, "configured", False):
        result.state = HealthState.HEALTHY
        result.summary = result.detail or "the coding engine is reachable"
    else:
        result.state = HealthState.NOT_CONFIGURED
        result.summary = result.detail or "the coding engine is not available"
    return result


def _watcher_check(status_probe: Optional[Callable[[], Any]]) -> CheckResult:
    """Whether the reliability watcher process is alive.

    A statement about the *process*, never about the site it watches. A
    watcher that is down is Wiz's problem — nothing is being observed — and a
    watcher that is up says nothing about what it is currently seeing.
    """
    result = CheckResult(name="watcher")
    if status_probe is None:
        result.state = HealthState.NOT_CONFIGURED
        result.detail = "no supervisor is attached"
        return result
    try:
        state = status_probe()
    except Exception as exc:  # noqa: BLE001
        result.state = HealthState.UNKNOWN
        result.detail = f"could not query the watcher: {exc}"
        return result

    raw_status = getattr(state, "status", "")
    # ``WatcherStatus`` mixes in ``str``, and ``str(member)`` on that
    # combination returns ``"WatcherStatus.OFFLINE"`` rather than ``"OFFLINE"``
    # on this Python — using ``.value`` when it exists is what actually asks
    # for the plain string, and falling back to ``str()`` still handles a
    # caller that passes a bare string in tests.
    status = str(getattr(raw_status, "value", raw_status) or "").upper()
    detail = str(getattr(state, "detail", "") or "")
    result.detail = detail
    if status == "STOPPED_BY_OPERATOR":
        # A deliberate stop is not a fault. Reporting it as one is exactly the
        # failure the reliability watcher's own emergency-stop handling refuses
        # to make, and this must not reintroduce it one layer up.
        result.state = HealthState.NOT_CONFIGURED
        result.summary = "stopped deliberately (emergency stop engaged)"
    elif status == "ONLINE":
        result.state = HealthState.HEALTHY
        result.summary = "running"
    elif status in ("STARTING",):
        result.state = HealthState.DEGRADED
        result.summary = "starting"
    elif status == "OFFLINE":
        result.state = HealthState.NOT_CONFIGURED
        result.summary = detail or "not installed or not running on this platform"
    else:  # ERROR and anything unrecognised
        result.state = HealthState.FAILED
        result.summary = detail or f"unhealthy ({status or 'unknown'})"
    return result


def _task_engine_check(product: Any) -> CheckResult:
    """Whether the feature pipeline and queue are reachable."""
    result = CheckResult(name="task_engine")
    if product is None or getattr(product, "pipeline", None) is None:
        result.state = HealthState.NOT_CONFIGURED
        result.detail = "no engineering target is configured"
        return result
    pipeline = product.pipeline
    try:
        active = len(pipeline.store.active(limit=1000))
    except Exception as exc:  # noqa: BLE001
        result.state = HealthState.FAILED
        result.detail = f"the feature store could not be read: {exc}"
        return result
    result.state = HealthState.HEALTHY
    result.summary = f"{active} active request(s)"
    return result


def _ledger_check(ledger: Any) -> CheckResult:
    """Whether the owner-notification ledger can be read and written.

    A broken ledger costs a duplicate message, never a missed one — see
    :mod:`openjarvis.reliability.notify_ledger` — but a duplicate Sir starts
    sending is exactly the noise this whole subsystem exists to remove, so it
    is worth Wiz being able to say "my memory of what I told you is broken"
    before the owner notices for themselves.
    """
    result = CheckResult(name="notification_ledger")
    if ledger is None:
        result.state = HealthState.NOT_CONFIGURED
        result.detail = "no ledger is attached"
        return result
    try:
        entries = ledger.entries()
    except Exception as exc:  # noqa: BLE001
        result.state = HealthState.FAILED
        result.detail = f"could not read the ledger: {exc}"
        return result
    result.state = HealthState.HEALTHY
    result.summary = f"{len(entries)} entries on record"
    return result


def _telegram_check(bot_token: str, allowed_chat_ids: str) -> CheckResult:
    """Whether Wiz has anything to speak with — not whether it is speaking well.

    Deliberately does not dial out: a live connectivity check belongs to a
    notifier's own send path, and duplicating it here would slow every health
    read down by a network round trip for a fact that changes rarely.
    """
    result = CheckResult(name="telegram")
    if not bot_token:
        result.state = HealthState.NOT_CONFIGURED
        result.detail = "no bot token is configured"
        return result
    if not allowed_chat_ids:
        result.state = HealthState.DEGRADED
        result.summary = "a bot token is set, but no owner chat is allow-listed"
        result.remediation = "set [channel.telegram] allowed_chat_ids"
        return result
    result.state = HealthState.HEALTHY
    result.summary = "configured"
    return result


def _voice_check(voice_probe: Optional[Callable[[], Any]]) -> CheckResult:
    """Folds :class:`~openjarvis.reliability.voice.health.VoiceHealth` in as one line.

    Sir Voice already has its own rich, part-by-part panel — hearing, speaking,
    the phone, the tailnet — and this does not re-derive any of it. It reads the
    one verdict that panel already produces, because a second opinion about the
    same five parts is how two health checks quietly disagree.
    """
    result = CheckResult(name="sir_voice")
    if voice_probe is None:
        result.state = HealthState.NOT_CONFIGURED
        result.detail = "voice is not configured"
        return result
    try:
        snapshot = voice_probe()
    except Exception as exc:  # noqa: BLE001
        result.state = HealthState.UNKNOWN
        result.detail = f"could not check voice: {exc}"
        return result
    verdict = str(snapshot.get("voice", "")).upper()
    mapping = {
        "ONLINE": HealthState.HEALTHY,
        "DEGRADED": HealthState.DEGRADED,
        "OFFLINE": HealthState.NOT_CONFIGURED,
    }
    result.state = mapping.get(verdict, HealthState.UNKNOWN)
    result.summary = verdict.lower() or "unknown"
    return result


def _scheduler_check(scheduler: Any) -> CheckResult:
    result = CheckResult(name="scheduler")
    if scheduler is None:
        result.state = HealthState.NOT_CONFIGURED
        result.detail = "no scheduler is attached"
        return result
    try:
        alive = bool(
            getattr(scheduler, "_thread", None) and scheduler._thread.is_alive()
        )
        tasks = len(scheduler.list_tasks())
    except Exception as exc:  # noqa: BLE001
        result.state = HealthState.UNKNOWN
        result.detail = f"could not read the scheduler: {exc}"
        return result
    if alive:
        result.state = HealthState.HEALTHY
        result.summary = f"running, {tasks} task(s)"
    else:
        result.state = HealthState.FAILED
        result.summary = "configured but not running"
    return result


def _claude_probe() -> Any:
    from openjarvis.wiz.runtime import claude_cli_available

    return claude_cli_available()


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def default_checks(
    *,
    journal: Any = None,
    registry: Any = None,
    coding_engine_probe: Optional[Callable[[], Any]] = None,
    watcher_status: Optional[Callable[[], Any]] = None,
    product: Any = None,
    ledger: Any = None,
    telegram_bot_token: str = "",
    telegram_allowed_chat_ids: str = "",
    voice_probe: Optional[Callable[[], Any]] = None,
    scheduler: Any = None,
) -> List[CheckResult]:
    """Every check Wiz runs about itself. Nothing here reads an incident."""
    return [
        _journal_check(journal),
        _capability_registry_check(registry),
        _coding_engine_check(coding_engine_probe or _claude_probe),
        _watcher_check(watcher_status),
        _task_engine_check(product),
        _ledger_check(ledger),
        _telegram_check(telegram_bot_token, telegram_allowed_chat_ids),
        _voice_check(voice_probe),
        _scheduler_check(scheduler),
    ]


def build_wiz_health(
    *,
    journal: Any = None,
    registry: Any = None,
    coding_engine_probe: Optional[Callable[[], Any]] = None,
    watcher_status: Optional[Callable[[], Any]] = None,
    product: Any = None,
    ledger: Any = None,
    telegram_bot_token: str = "",
    telegram_allowed_chat_ids: str = "",
    voice_probe: Optional[Callable[[], Any]] = None,
    scheduler: Any = None,
) -> WizHealthReport:
    """Assemble a report from live collaborators.

    Every parameter is optional and every absence is honestly
    ``NOT_CONFIGURED`` rather than silently skipped — a Wiz with no scheduler
    attached says so, and does not simply omit the row.
    """
    return WizHealthReport(
        checks=default_checks(
            journal=journal,
            registry=registry,
            coding_engine_probe=coding_engine_probe,
            watcher_status=watcher_status,
            product=product,
            ledger=ledger,
            telegram_bot_token=telegram_bot_token,
            telegram_allowed_chat_ids=telegram_allowed_chat_ids,
            voice_probe=voice_probe,
            scheduler=scheduler,
        )
    )


def which(executable: str) -> bool:
    """Whether *executable* is on ``PATH``. Exposed for check authors."""
    return shutil.which(executable) is not None
