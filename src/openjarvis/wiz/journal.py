"""An append-only, hash-chained record of what Wiz decided and why.

Appending is guarded two ways: a ``threading.Lock`` for ordering within this
process, and a :class:`~openjarvis.wiz.proclock.ProcessLease` for ordering
*between* processes. Both matter here specifically, more than in most of this
codebase's other single-flight guards: the in-process lock alone would let
two OpenJarvis processes each read the same tail, both compute the next
sequence number and previous-hash from it, and both append — producing two
entries claiming the same sequence, which :meth:`WizJournal.verify` cannot
tell apart from tampering. An audit trail that cannot survive its own writer
running twice is not much of an audit trail. See
:mod:`openjarvis.wiz.proclock` for why a kernel ``flock`` lease rather than a
hand-rolled scheme.

This is the third hash chain in the system, and that is a deliberate choice
rather than an oversight. The other two record different subjects and cannot
absorb this one without being distorted: ``security/audit.py`` records scanner
findings and is shaped around matched text and threat levels;
``reliability/store.py`` records incident state transitions and is shaped around
an incident's lifecycle. An authority decision is neither. Forcing it into
either schema would mean either lying about its shape or widening a schema that
a production subsystem depends on.

What the chain buys is narrow and worth stating plainly: entries cannot be
edited or removed after the fact without the break being detectable. It is not
tamper-*proof* — anything that can write the file can rewrite the whole chain
from the point of interest onward. It is tamper-*evident* for everything short
of that, which is what an audit trail on a single-operator machine needs to be.

Entries never contain credentials. The journal records that a capability was
exercised, by whom, and what the deterministic gates said — not the contents of
what was read or written.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from openjarvis.wiz.proclock import ProcessLease

logger = logging.getLogger(__name__)

__all__ = ["JournalEntry", "WizJournal"]

#: The hash a chain starts from, so the first entry is chained to something
#: rather than to nothing.
GENESIS = "0" * 64


def _digest(previous: str, payload: str) -> str:
    return hashlib.sha256(f"{previous}{payload}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One recorded decision."""

    #: Monotonic within a file, starting at 1.
    sequence: int

    #: ISO-8601. Supplied by the caller so the journal has no clock of its own
    #: and stays trivially testable.
    at: str

    #: What happened: ``authority.granted``, ``authority.refused``,
    #: ``capability.unavailable``, ``feature.state``, ...
    kind: str

    #: The capability the entry is about, when there is one.
    capability: str = ""

    actor_id: str = ""
    channel: str = ""

    #: Human-readable justification. This is the field an operator actually
    #: reads six weeks later.
    reason: str = ""

    #: Structured extras. Kept small and free of anything secret.
    detail: Dict[str, Any] = field(default_factory=dict)

    previous_hash: str = GENESIS
    entry_hash: str = ""

    def payload(self) -> str:
        """The canonical serialisation that gets hashed.

        Sorted keys and no whitespace, so the same entry always produces the
        same digest regardless of how the dict happened to be built.
        """
        return json.dumps(
            {
                "sequence": self.sequence,
                "at": self.at,
                "kind": self.kind,
                "capability": self.capability,
                "actor_id": self.actor_id,
                "channel": self.channel,
                "reason": self.reason,
                "detail": self.detail,
                "previous_hash": self.previous_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def sealed(self) -> "JournalEntry":
        """This entry with its hash computed."""
        return JournalEntry(
            sequence=self.sequence,
            at=self.at,
            kind=self.kind,
            capability=self.capability,
            actor_id=self.actor_id,
            channel=self.channel,
            reason=self.reason,
            detail=self.detail,
            previous_hash=self.previous_hash,
            entry_hash=_digest(self.previous_hash, self.payload()),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sequence": self.sequence,
            "at": self.at,
            "kind": self.kind,
            "capability": self.capability,
            "actor_id": self.actor_id,
            "channel": self.channel,
            "reason": self.reason,
            "detail": self.detail,
            "previous_hash": self.previous_hash,
            "entry_hash": self.entry_hash,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "JournalEntry":
        return cls(
            sequence=int(raw.get("sequence", 0)),
            at=str(raw.get("at", "")),
            kind=str(raw.get("kind", "")),
            capability=str(raw.get("capability", "")),
            actor_id=str(raw.get("actor_id", "")),
            channel=str(raw.get("channel", "")),
            reason=str(raw.get("reason", "")),
            detail=dict(raw.get("detail") or {}),
            previous_hash=str(raw.get("previous_hash", GENESIS)),
            entry_hash=str(raw.get("entry_hash", "")),
        )


#: How long record() waits for another process's append before giving up.
#: Short: a journal write happens on nearly every pipeline step, so a wedged
#: holder should surface as a fast, loud failure (caught and logged by
#: callers such as FeaturePipeline._record) rather than stalling the whole
#: pipeline behind it.
DEFAULT_LEASE_TIMEOUT = 10.0


class WizJournal:
    """A JSONL file of :class:`JournalEntry`, chained and append-only."""

    def __init__(
        self,
        path: str | Path,
        *,
        lease_timeout: float = DEFAULT_LEASE_TIMEOUT,
    ) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        # Cross-process append ordering — see the module docstring. The
        # lockfile sits next to the journal it protects, named distinctly so
        # nothing that globs *.jsonl picks it up.
        self._lease = ProcessLease(
            self._path.with_name(self._path.name + ".lock"), owner="wiz-journal"
        )
        self._lease_timeout = lease_timeout

    @property
    def path(self) -> Path:
        return self._path

    # -- writing -----------------------------------------------------------

    def record(
        self,
        *,
        at: str,
        kind: str,
        capability: str = "",
        actor_id: str = "",
        channel: str = "",
        reason: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> JournalEntry:
        """Append one entry and return it, sealed."""
        with self._lock, self._lease.acquire(
            timeout=self._lease_timeout, reason=f"journal.record: {kind}"
        ):
            sequence, previous = self._tail_locked()
            entry = JournalEntry(
                sequence=sequence + 1,
                at=at,
                kind=kind,
                capability=capability,
                actor_id=actor_id,
                channel=channel,
                reason=reason,
                detail=dict(detail or {}),
                previous_hash=previous,
            ).sealed()
            self._append_locked(entry)
            return entry

    def _append_locked(self, entry: JournalEntry) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_dict(), sort_keys=True, separators=(",", ":"))
        # Append and flush to disk before returning: an entry the caller has
        # been told was recorded must survive the process dying immediately
        # afterwards, which is exactly when an audit trail matters most.
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _tail_locked(self) -> Tuple[int, str]:
        last: Optional[JournalEntry] = None
        for entry in self._read():
            last = entry
        if last is None:
            return 0, GENESIS
        return last.sequence, last.entry_hash

    # -- reading -----------------------------------------------------------

    def _read(self) -> Iterator[JournalEntry]:
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield JournalEntry.from_dict(json.loads(line))
                    except (ValueError, TypeError):
                        # A corrupt line is reported, not skipped silently: the
                        # chain check below will fail on it, which is the point.
                        logger.error("unreadable journal line in %s", self._path)
                        return
        except OSError as exc:
            logger.error("journal at %s could not be read: %s", self._path, exc)

    def entries(self) -> List[JournalEntry]:
        return list(self._read())

    def tail(self, limit: int = 50) -> List[JournalEntry]:
        entries = self.entries()
        return entries[-limit:] if limit > 0 else entries

    # -- integrity ---------------------------------------------------------

    def verify(self) -> Tuple[bool, Optional[int]]:
        """Whether the chain is intact, and the sequence where it first is not."""
        previous = GENESIS
        expected_sequence = 1
        for entry in self._read():
            if entry.sequence != expected_sequence:
                return False, entry.sequence
            if entry.previous_hash != previous:
                return False, entry.sequence
            recomputed = _digest(entry.previous_hash, entry.payload())
            if recomputed != entry.entry_hash:
                return False, entry.sequence
            previous = entry.entry_hash
            expected_sequence += 1
        return True, None
