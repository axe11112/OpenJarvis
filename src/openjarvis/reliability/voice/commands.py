"""Carrying out what a voice asked for — and refusing what it may not.

The dispatch table here is the whole security surface of Sir Voice. Three rules
hold it together:

**No shell.** Nothing in this module builds a command string. Every operation is
a call to an existing JARVIS primitive that was already narrow before a voice
could reach it — the supervisor's four allowlisted ``launchctl`` subcommands,
the diagnostic's read-only checks, a file touch for the emergency stop. A voice
cannot name a program, an argument or a path.

**Direction of travel.** Every ``SAFE`` operation makes JARVIS *less* capable:
stop repairing, stop everything, hand this incident to a human. Restarting the
watcher is the one that adds function, and it only returns the system to the
state an operator already configured. Nothing here grants authority, and that is
why a microphone is allowed to trigger it at all.

**Confirmation is not persuasion.** ``CONFIRM`` intents return a refusal and a
pending row in the Control Center. There is no argument, no repetition and no
phrasing that promotes one, because the promotion path does not exist in this
file — the approval lives on a screen, behind a session, in a different process.

Every execution is audited, whether it ran, was refused, or failed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Dict, Optional

from openjarvis.reliability.types import now_iso
from openjarvis.reliability.voice.answers import VoiceFacts, answer_for
from openjarvis.reliability.voice.confirmations import ConfirmationStore
from openjarvis.reliability.voice.intents import Intent, IntentMatch, Risk

logger = logging.getLogger(__name__)

__all__ = ["CommandResult", "VoiceCommands"]

#: What Sir says when asked for something only the Control Center may grant.
NEEDS_CONFIRMATION = "Sir, that action needs confirmation in the Control Center."


@dataclass
class CommandResult:
    """What happened when an utterance was acted on."""

    #: What Sir says back. Always populated.
    speech: str
    intent: str = ""
    risk: str = ""
    #: Whether an operation actually ran. False for reads, refusals and errors.
    executed: bool = False
    #: Set when the request was parked for Control Center approval.
    confirmation_id: str = ""
    #: True when this ends the call.
    ends_call: bool = False
    at: str = field(default_factory=now_iso)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the audit record."""
        return {
            "at": self.at,
            "intent": self.intent,
            "risk": self.risk,
            "executed": self.executed,
            "confirmation_id": self.confirmation_id,
            "ends_call": self.ends_call,
            "speech": self.speech,
        }


@dataclass
class VoiceCommands:
    """Executes the allowlist, and only the allowlist.

    Parameters
    ----------
    facts:
        Read-only view of JARVIS state, for answering questions.
    confirmations:
        Where high-risk requests are parked.
    supervisor:
        Optional ``LaunchdSupervisor``, for restarting the watcher and engaging
        the emergency stop. Absent means those operations report that they are
        unavailable rather than pretending to succeed.
    diagnostic_factory:
        Builds a ``LiveDiagnostic`` on demand. Called only for the read-only
        diagnostic, and only when the operator asks for one by voice.
    store:
        Incident store, for handing an incident to the operator.
    audit:
        ``(CommandResult) -> None``, called for every outcome.
    """

    facts: VoiceFacts
    confirmations: ConfirmationStore
    supervisor: Any = None
    diagnostic_factory: Optional[Callable[[], Any]] = None
    store: Any = None
    audit: Optional[Callable[[CommandResult], None]] = None
    session_id: str = ""

    def handle(self, match: IntentMatch) -> CommandResult:
        """Act on one interpreted utterance."""
        if not match.understood:
            return self._record(
                CommandResult(
                    speech=(
                        "Sir, I didn't catch that. You can ask me for the status, "
                        "what happened, or to run a diagnostic."
                    ),
                    intent="",
                    risk="",
                )
            )

        intent = match.intent
        assert intent is not None  # narrowed by `understood`

        if intent.risk is Risk.CONFIRM:
            return self._record(self._refuse(intent, match))
        if intent.risk is Risk.SAFE:
            return self._record(self._operate(intent, match))
        return self._record(self._read(intent, match))

    # -- READ -------------------------------------------------------------

    def _read(self, intent: Intent, _match: IntentMatch) -> CommandResult:
        return CommandResult(
            speech=answer_for(intent.name, self.facts),
            intent=intent.name,
            risk=intent.risk.value,
            ends_call=intent.name == "goodbye",
        )

    # -- CONFIRM ----------------------------------------------------------

    def _refuse(self, intent: Intent, match: IntentMatch) -> CommandResult:
        """Park the request, say so, change nothing."""
        pending = self.confirmations.request(
            intent=intent.name,
            description=intent.description or intent.name,
            transcript=match.transcript,
            session_id=self.session_id,
        )
        return CommandResult(
            speech=NEEDS_CONFIRMATION,
            intent=intent.name,
            risk=intent.risk.value,
            executed=False,
            confirmation_id=pending.id,
        )

    # -- SAFE -------------------------------------------------------------

    def _operate(self, intent: Intent, match: IntentMatch) -> CommandResult:
        handler = self._OPERATIONS.get(intent.name)
        if handler is None:  # pragma: no cover - table and enum kept in step
            return CommandResult(
                speech="Sir, I can't do that.",
                intent=intent.name,
                risk=intent.risk.value,
            )
        try:
            speech, executed = handler(self, match)
        except Exception:  # noqa: BLE001 - a failed operation is reported
            logger.exception("voice: operation %s failed", intent.name)
            return CommandResult(
                speech="Sir, I tried and it didn't work. It's on the dashboard.",
                intent=intent.name,
                risk=intent.risk.value,
                executed=False,
            )
        return CommandResult(
            speech=speech,
            intent=intent.name,
            risk=intent.risk.value,
            executed=executed,
        )

    def _op_run_diagnostic(self, _match: IntentMatch):
        """Run the read-only diagnostic and summarise it in one sentence."""
        if self.diagnostic_factory is None:
            return "Sir, I can't run a diagnostic from here.", False
        diagnostic = self.diagnostic_factory()
        # Deliberately the individual read-only checks, never `run()`: `run()`
        # can open incidents, and a question asked by voice must not create work.
        results = []
        for name in ("check_github", "check_vercel", "check_website"):
            check = getattr(diagnostic, name, None)
            if check is None:
                continue
            try:
                results.append(check())
            except Exception:  # noqa: BLE001
                logger.exception("voice: diagnostic %s failed", name)
        if not results:
            return "Sir, the diagnostic didn't return anything.", True

        bad = [r for r in results if getattr(r.state, "value", "") == "FAILED"]
        summary = (
            f"Sir, I checked {len(results)} things and they all look fine."
            if not bad
            else (
                f"Sir, I checked {len(results)} things and "
                f"{len(bad)} came back bad. It's on the dashboard."
            )
        )
        # Cached on the facts so "what did the diagnostic say" can repeat it
        # without running it again.
        self.facts.last_diagnostic = summary
        return summary, True

    def _op_rerun_probe(self, _match: IntentMatch):
        """Re-running one check is the diagnostic's website check, scoped."""
        if self.diagnostic_factory is None:
            return "Sir, I can't run a check from here.", False
        diagnostic = self.diagnostic_factory()
        check = getattr(diagnostic, "check_website", None)
        if check is None:
            return "Sir, I can't run a check from here.", False
        result = check()
        ok = getattr(result.state, "value", "") not in ("FAILED", "DEGRADED")
        return (
            "Sir, I checked the site again and it's passing."
            if ok
            else "Sir, I checked the site again and it's still failing."
        ), True

    def _op_stop_repairs(self, _match: IntentMatch):
        """Stop automatic repair by engaging the stop flag the watcher reads."""
        return self._engage_stop(
            "Sir, I've stopped automatic repairs. I'm still watching."
        )

    def _op_emergency_stop(self, _match: IntentMatch):
        return self._engage_stop("Sir, emergency stop is on. I've stopped.")

    def _engage_stop(self, speech: str):
        if self.supervisor is None:
            return "Sir, I can't reach the controls from here.", False
        flag = self.supervisor.stop_flag()
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(f"engaged by voice at {now_iso()}\n", encoding="utf-8")
        logger.warning("voice: emergency stop engaged")
        return speech, True

    def _op_restart_watcher(self, _match: IntentMatch):
        if self.supervisor is None:
            return "Sir, I can't reach the controls from here.", False
        ok, _message = self.supervisor.restart()
        return (
            "Sir, I've restarted."
            if ok
            else "Sir, I couldn't restart. It's on the dashboard."
        ), bool(ok)

    def _op_hand_over(self, _match: IntentMatch):
        """Leave the current incident to the operator."""
        from openjarvis.reliability.types import IncidentState

        incident = self.facts.latest()
        if incident is None:
            return "Sir, there's nothing open to hand over.", False
        if self.store is None:
            return "Sir, I can't change that from here.", False
        if incident.state is IncidentState.HUMAN_REQUIRED:
            return "Sir, it's already waiting for you.", False
        if not incident.can_transition_to(IncidentState.HUMAN_REQUIRED):
            return "Sir, I can't hand that one over right now.", False
        self.store.transition(
            incident,
            IncidentState.HUMAN_REQUIRED,
            actor="voice",
            reason="handed to the operator by voice",
        )
        return "Sir, it's yours. I won't touch it.", True

    #: Name -> bound operation. A table rather than ``getattr`` on a string:
    #: there is then no utterance that can reach a method this table does not
    #: name, however the intent name is spelled. ``ClassVar`` so the dataclass
    #: treats it as the constant it is, not as per-instance state something
    #: could rebind.
    _OPERATIONS: ClassVar[Dict[str, Callable[..., Any]]] = {
        "run_diagnostic": _op_run_diagnostic,
        "rerun_probe": _op_rerun_probe,
        "stop_repairs": _op_stop_repairs,
        "emergency_stop": _op_emergency_stop,
        "restart_watcher": _op_restart_watcher,
        "hand_over": _op_hand_over,
    }

    # -- audit ------------------------------------------------------------

    def _record(self, result: CommandResult) -> CommandResult:
        logger.info(
            "voice: intent=%s risk=%s executed=%s",
            result.intent or "-",
            result.risk or "-",
            result.executed,
        )
        if self.audit is not None:
            try:
                self.audit(result)
            except Exception:  # noqa: BLE001 - an audit gap must not stop a call
                logger.exception("voice: could not audit a command")
        return result
