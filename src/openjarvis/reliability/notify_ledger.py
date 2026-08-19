"""What the owner has already been told, remembered across restarts.

The in-process guards in :mod:`~openjarvis.reliability.notify` — a dedup window
and an hourly cap — are memory. They vanish when the watcher restarts, and the
watcher restarts whenever the machine sleeps, the code updates, or launchd
decides to. The owner does not experience a restart as a fresh start; they
experience it as being told the same thing again.

So the record of what has been said lives on disk, keyed by *what the message
was about* rather than by its text:

    (incident, state-of-the-problem)

The second half is the interesting one. An incident that moves from one
internal failure state to another while still needing exactly the same thing
from the owner has not changed as far as they are concerned — telling them again
is narrating a state machine. What counts as a change is deliberately narrow:

* the problem is fixed;
* it got materially worse (severity rose);
* something genuinely new was learned that changes what the owner must do.

Everything else is silence. That is the difference between an assistant that
handles things and one that reports on itself.
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

__all__ = ["NotificationLedger", "ledger_path", "owner_state"]

#: How many entries to keep. Generous: an entry is a few hundred bytes and the
#: cost of forgetting is telling somebody something twice.
MAX_ENTRIES = 500


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


def owner_state(incident: Incident) -> str:
    """What this incident currently means *to the owner*.

    Deliberately coarse. Two incidents in different internal states that ask the
    same thing of the owner produce the same string here, and therefore only one
    message. The internal lifecycle is rich and the owner-facing one has three
    positions: it is fixed, it needs you, or it is being handled.
    """
    from openjarvis.reliability.types import IncidentState

    if incident.state is IncidentState.RESOLVED:
        return "fixed"
    if incident.state in (
        IncidentState.HUMAN_REQUIRED,
        IncidentState.FAILED,
        IncidentState.RECOVERY_REQUIRED,
        IncidentState.ROLLED_BACK,
    ):
        # Severity is part of the key: a problem that gets materially worse is
        # worth saying again, and only that.
        return f"needs-you:{incident.severity.value}"
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
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self._load()

    # -- the question ------------------------------------------------------

    def should_notify(self, incident: Incident, *, kind: str = "problem") -> bool:
        """Whether this is news to the owner.

        ``kind`` separates channels that legitimately coexist — a success
        message and a problem message about the same incident are different
        things — while still collapsing repeats within each.
        """
        key = self._key(incident, kind)
        state = owner_state(incident)
        with self._lock:
            previous = self._entries.get(key)
            if previous is None:
                return True
            if previous.get("owner_state") == state:
                logger.info(
                    "incident %s: the owner already knows (%s); staying quiet",
                    incident.id,
                    state,
                )
                return False
            # The state changed. Only *upward* severity or resolution is news;
            # sliding between internal failure states is not.
            return self._materially_different(previous.get("owner_state", ""), state)

    def record(self, incident: Incident, *, kind: str = "problem") -> None:
        """Note that the owner has now been told."""
        key = self._key(incident, kind)
        with self._lock:
            self._entries[key] = {
                "incident_id": incident.id,
                "kind": kind,
                "owner_state": owner_state(incident),
                "severity": incident.severity.value,
                "at": now_iso(),
            }
            self._trim_locked()
        self._save()

    def forget(self, incident: Incident, *, kind: str = "problem") -> None:
        """Drop the record, so the next event about this is news again."""
        with self._lock:
            self._entries.pop(self._key(incident, kind), None)
        self._save()

    def last(self, incident: Incident, *, kind: str = "problem") -> Dict[str, Any]:
        """What the owner was last told about this, for the Control Center."""
        with self._lock:
            return dict(self._entries.get(self._key(incident, kind), {}))

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _key(incident: Incident, kind: str) -> str:
        """Keyed on fingerprint, not incident id.

        A flapping check opens a fresh incident every time it fails; keyed on
        id, each recurrence is new and the owner hears about all of them.
        """
        identity = getattr(incident, "fingerprint", "") or incident.id
        return f"{kind}:{identity}"

    @staticmethod
    def _materially_different(before: str, after: str) -> bool:
        """Whether the change is worth a second message."""
        if after == "fixed":
            return True  # "it is working again" is always worth saying
        if before.startswith("working") and after.startswith("needs-you"):
            return True  # JARVIS has stopped; that is new information
        if before.startswith("needs-you") and after.startswith("needs-you"):
            # Still stuck. Only a genuine worsening counts.
            try:
                was = Severity.parse(before.split(":", 1)[1])
                now = Severity.parse(after.split(":", 1)[1])
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
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt ledger costs a duplicate,
            logger.warning("could not read the notification ledger at %s", self.path)
            self._entries = {}  # ...never a crash.

    def _save(self) -> None:
        # Atomic, because the restarts this ledger exists to survive are the
        # same events that can interrupt a write. A half-written ledger reads
        # back as no ledger, and no ledger means the owner is told everything
        # they already know, all over again.
        if self.path is None:
            return
        write_json_atomic(self.path, self._entries)
