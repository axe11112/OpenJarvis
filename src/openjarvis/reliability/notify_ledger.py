"""What the owner has already been told, remembered across restarts.

The in-process guards in :mod:`~openjarvis.reliability.notify` — a dedup window
and an hourly cap — are memory. They vanish when the watcher restarts, and the
watcher restarts whenever the machine sleeps, the code updates, or launchd
decides to. The owner does not experience a restart as a fresh start; they
experience it as being told the same thing again.

So the record of what has been said lives on disk. The interesting question is
what it is keyed on, and the answer changed after a morning that produced five
messages about one broken deployment.

    (underlying problem, what it currently asks of the owner)

**The underlying problem, not the incident.** Keyed on incident id, a flapping
check re-announces itself every few minutes. Keyed on fingerprint — which is
where this started — a single deployment failure still produces one message per
*probe*, because the homepage, login and sign-up probes each observe their own
failure and each fingerprint is different. The key is now the correlated outage
from :mod:`~openjarvis.reliability.outage`, falling back to the fingerprint and
then the id when correlation has not run. Five probes, one problem, one entry.

**What it asks, not what state it is in.** An incident that moves from one
internal failure state to another while still needing exactly the same thing
from the owner has not changed as far as they are concerned — telling them again
is narrating a state machine. So the owner-facing state carries a digest of the
:class:`~openjarvis.reliability.owner_ask.OwnerAsk`, and what counts as a change
is deliberately narrow:

* the problem is fixed;
* it got materially worse *and* that changes what the owner must do;
* the specific ask itself changed.

Everything else is silence. That is the difference between an assistant that
handles things and one that reports on itself.

Problem and success share one slot, on purpose. "It needs you" and "it is
fixed" are two positions of the same conversation about one outage, and giving
them separate channels is how an owner ends up with a "needs you" and a "fixed"
that do not refer to each other.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from openjarvis.reliability.statefile import write_json_atomic
from openjarvis.reliability.types import Incident, Severity, now_iso

logger = logging.getLogger(__name__)

__all__ = [
    "NotificationLedger",
    "ledger_path",
    "owner_identity",
    "owner_state",
]

#: How many entries to keep. Generous: an entry is a few hundred bytes and the
#: cost of forgetting is telling somebody something twice.
MAX_ENTRIES = 500

#: The single owner-facing channel. Problem and success are positions in one
#: conversation, not two subscriptions.
OWNER = "owner"


def ledger_path(config: Any) -> Path:
    """Where the ledger lives.

    Resolved the same way as the emergency stop flag — next to the incident
    database — so the watcher and the Control Center cannot disagree about what
    the owner has already been told.
    """
    from openjarvis.core.paths import get_config_dir

    configured = getattr(getattr(config, "reliability", None), "db_path", "")
    if configured:
        return Path(configured).expanduser().parent / "notified.json"
    return get_config_dir() / "reliability" / "notified.json"


def owner_identity(incident: Incident) -> str:
    """Which *problem* this incident is evidence of.

    The correlated outage when one has been assigned, the fingerprint when it
    has not, the incident id as a last resort. Every fallback is narrower than
    the one before it, so a missing correlation costs an extra message and
    never a missed outage.
    """
    metadata = getattr(incident, "metadata", None) or {}
    outage = str(metadata.get("outage_key") or "").strip()
    if outage:
        return outage
    return str(getattr(incident, "fingerprint", "") or "") or str(incident.id or "")


def owner_state(incident: Incident, *, ask: Any = None) -> str:
    """What this incident currently means *to the owner*.

    Deliberately coarse. Two incidents in different internal states that ask the
    same thing of the owner produce the same string here, and therefore only one
    message. The internal lifecycle is rich and the owner-facing one has three
    positions: it is fixed, it needs you, or it is being handled.

    When an ask is supplied its digest is part of the string, so a *different*
    ask about the same problem is a new message and the same ask never is.
    """
    from openjarvis.reliability.types import IncidentState

    if incident.state is IncidentState.RESOLVED:
        return "fixed"
    if (
        incident.state
        in (
            IncidentState.HUMAN_REQUIRED,
            IncidentState.FAILED,
            IncidentState.RECOVERY_REQUIRED,
            IncidentState.ROLLED_BACK,
        )
        or ask is not None
    ):
        digest = ""
        try:
            digest = str(ask.digest()) if ask is not None else ""
        except Exception:  # noqa: BLE001 - a malformed ask must not crash a send
            digest = ""
        # Severity is part of the key: a problem that gets materially worse is
        # worth saying again — but only when the ask changed with it.
        return f"needs-you:{incident.severity.value}:{digest}"
    return f"working:{incident.severity.value}"


@dataclass
class NotificationLedger:
    """Remembers what the owner has been told, across restarts.

    Parameters
    ----------
    path:
        Where to persist. ``None`` keeps it in memory, which is only correct in
        tests — in production the whole point is surviving a restart.
    """

    path: Optional[Path] = None
    _entries: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _stamp: tuple = field(default=(0, 0.0), repr=False)

    def __post_init__(self) -> None:
        self._load()

    def _refresh(self) -> None:
        """Re-read the file when another process has written to it.

        The watcher, the Control Center and a CLI invocation are three
        processes sharing one ledger. Without this, whichever loaded first
        answers from a snapshot and re-announces what another has already said
        — the cross-process version of the bug the file itself was added to
        fix.
        """
        if self.path is None:
            return
        try:
            stat = self.path.stat()
            stamp = (stat.st_size, stat.st_mtime)
        except OSError:
            return
        if stamp != self._stamp:
            self._load()

    # -- the question ------------------------------------------------------

    def should_notify(
        self, incident: Incident, *, kind: str = OWNER, ask: Any = None
    ) -> bool:
        """Whether this is news to the owner."""
        key = self._key(incident, kind)
        state = owner_state(incident, ask=ask)
        with self._lock:
            self._refresh()
            previous = self._entries.get(key)
            if previous is None:
                return True
            if previous.get("owner_state") == state:
                logger.info(
                    "%s: the owner already knows (%s); staying quiet",
                    key,
                    state,
                )
                return False
            return self._materially_different(previous.get("owner_state", ""), state)

    def was_told(self, incident: Incident, *, kind: str = OWNER) -> bool:
        """Whether the owner is currently carrying an unresolved escalation.

        The gate on success messages. An outage the owner never heard about
        does not need a "it's fixed" — that message is only meaningful as the
        end of a conversation they were part of.
        """
        with self._lock:
            self._refresh()
            previous = self._entries.get(self._key(incident, kind))
        state = str((previous or {}).get("owner_state") or "")
        return state.startswith("needs-you")

    def record(self, incident: Incident, *, kind: str = OWNER, ask: Any = None) -> None:
        """Note that the owner has now been told."""
        key = self._key(incident, kind)
        with self._lock:
            self._refresh()
            self._entries[key] = {
                "incident_id": incident.id,
                "identity": owner_identity(incident),
                "kind": kind,
                "owner_state": owner_state(incident, ask=ask),
                "severity": incident.severity.value,
                "action": str(getattr(ask, "action", "") or ""),
                "at": now_iso(),
            }
            self._trim_locked()
        self._save()

    def record_fixed(self, incident: Incident, *, kind: str = OWNER) -> None:
        """Note that the owner has been told it is over.

        Recorded rather than forgotten. A forgotten entry cannot stop a second
        success message arriving from a different part of the pipeline — the
        repair loop, the post-merge verifier and the detector all have a claim
        on "it works again" — and the owner reads those as three fixes for one
        problem.
        """
        key = self._key(incident, kind)
        with self._lock:
            self._refresh()
            self._entries[key] = {
                "incident_id": incident.id,
                "identity": owner_identity(incident),
                "kind": kind,
                "owner_state": "fixed",
                "severity": incident.severity.value,
                "at": now_iso(),
            }
            self._trim_locked()
        self._save()

    def forget(self, incident: Incident, *, kind: str = OWNER) -> None:
        """Drop the record, so the next event about this is news again."""
        with self._lock:
            self._entries.pop(self._key(incident, kind), None)
        self._save()

    def last(self, incident: Incident, *, kind: str = OWNER) -> Dict[str, Any]:
        """What the owner was last told about this, for the Control Center."""
        with self._lock:
            return dict(self._entries.get(self._key(incident, kind), {}))

    def entries(self) -> Dict[str, Dict[str, Any]]:
        """Everything on record, for the Control Center and diagnostics."""
        with self._lock:
            return {k: dict(v) for k, v in self._entries.items()}

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _key(incident: Incident, kind: str) -> str:
        """Keyed on the underlying problem, not on the incident.

        A flapping check opens a fresh incident every time it fails; keyed on
        id, each recurrence is new. Five probes observe one broken deployment;
        keyed on fingerprint, each probe is new. Keyed on the outage, it is one
        problem and one entry — see :func:`owner_identity`.
        """
        return f"{kind}:{owner_identity(incident)}"

    @staticmethod
    def _materially_different(before: str, after: str) -> bool:
        """Whether the change is worth a second message."""
        if after == "fixed":
            return True  # "it is working again" is always worth saying
        if before.startswith("working") and after.startswith("needs-you"):
            return True  # JARVIS has stopped; that is new information
        if before.startswith("needs-you") and after.startswith("needs-you"):
            before_severity, before_ask = _split(before)
            after_severity, after_ask = _split(after)
            if before_ask and after_ask and before_ask != after_ask:
                # A different thing is being asked of the owner. That is the
                # definition of news, whatever the severity did.
                return True
            if before_ask and after_ask and before_ask == after_ask:
                # Same ask. A severity change that does not change what the
                # owner must do is a state machine talking about itself.
                return False
            try:
                was = Severity.parse(before_severity)
                now = Severity.parse(after_severity)
            except (ValueError, IndexError):
                return False
            return now.rank > was.rank
        # fixed -> working/needs-you: it broke again, which is news.
        return before == "fixed"

    def _trim_locked(self) -> None:
        if len(self._entries) <= MAX_ENTRIES:
            return
        oldest = sorted(self._entries.items(), key=lambda kv: kv[1].get("at", ""))
        for key, _ in oldest[: len(self._entries) - MAX_ENTRIES]:
            self._entries.pop(key, None)

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("the ledger is not an object")
            self._entries = {
                str(key): value
                for key, value in loaded.items()
                if isinstance(value, dict)
            }
            self._stamp = self._current_stamp()
        except Exception:  # noqa: BLE001 - a corrupt ledger costs a duplicate,
            logger.warning("could not read the notification ledger at %s", self.path)
            self._entries = {}  # ...never a crash.
            self._stamp = self._current_stamp()

    def _save(self) -> None:
        # Atomic, because the restarts this ledger exists to survive are the
        # same events that can interrupt a write. A half-written ledger reads
        # back as no ledger, and no ledger means the owner is told everything
        # they already know, all over again.
        if self.path is None:
            return
        try:
            write_json_atomic(self.path, self._entries)
            self._stamp = self._current_stamp()
        except Exception:  # noqa: BLE001 - failing to persist costs a duplicate
            logger.exception("could not write the notification ledger at %s", self.path)

    def _current_stamp(self) -> tuple:
        if self.path is None:
            return (0, 0.0)
        try:
            stat = self.path.stat()
        except OSError:
            return (0, 0.0)
        return (stat.st_size, stat.st_mtime)


def _split(state: str) -> tuple[str, str]:
    """``"needs-you:CRITICAL:abc123"`` into ``("CRITICAL", "abc123")``."""
    parts = state.split(":")
    severity = parts[1] if len(parts) > 1 else ""
    digest = parts[2] if len(parts) > 2 else ""
    return severity, digest
