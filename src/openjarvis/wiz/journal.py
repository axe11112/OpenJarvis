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
        with (
            self._lock,
            self._lease.acquire(
                timeout=self._lease_timeout, reason=f"journal.record: {kind}"
            ),
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
            # A process killed mid-write leaves a line with no newline. Without
            # this, the next append lands on the end of that partial line and
            # welds the two together -- destroying a second, perfectly good
            # record as collateral, and turning one lost entry into two. The
            # newline keeps the damage to the line that was actually torn,
            # where _scan can see it and verify() can report it.
            if handle.tell() > 0 and not self._ends_with_newline():
                handle.write("\n")
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _ends_with_newline(self) -> bool:
        """Whether the journal's last byte is a newline. True for an empty file."""
        try:
            with open(self._path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    return True
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) == b"\n"
        except OSError:
            return True

    def _tail_locked(self) -> Tuple[int, str]:
        """The sequence to continue from, and the hash to chain onto.

        The sequence comes from the highest one anywhere in the file, not from
        the last *readable* entry. Those differ exactly when a line is corrupt,
        and the difference matters: :meth:`_read` stops at a corrupt line, so
        continuing from the last readable entry re-issued a sequence number
        that entries further down the file already used. A journal with one
        torn line and six entries ended up holding sequences 1, 2, <corrupt>,
        4, 5, 3 -- the chain forked, and forking it destroys the only property
        an audit log has.

        The chain is still anchored to the last readable entry's hash, so the
        break at the corrupt line stays visible to :meth:`verify` rather than
        being papered over. Appending is allowed to continue: refusing every
        further record would mean one torn line silences the audit trail
        permanently, which loses more than it protects now that the damage is
        reported honestly and no longer compounds.
        """
        entries, _corrupt_at, highest = self._scan()
        if not entries:
            # Still respect a highest sequence read from a file whose very
            # first line is corrupt, so even then nothing is re-issued.
            return highest, GENESIS
        return max(entries[-1].sequence, highest), entries[-1].entry_hash

    # -- reading -----------------------------------------------------------

    def _scan(self) -> Tuple[List[JournalEntry], Optional[int], int]:
        """Read the file once, returning what it holds and how it is damaged.

        Returns the readable entries *before* the first corrupt line, the
        1-based number of that line (``None`` when there is none), and the
        highest sequence found anywhere in the file -- including in entries
        after the corruption, which the entry list deliberately stops short of.

        The corruption has to be returned rather than merely logged. The
        previous code stopped the iteration at a bad line and left a comment
        saying "the chain check below will fail on it, which is the point" --
        but stopping the iteration is exactly what stops the chain check from
        ever seeing it. :meth:`verify` walked the clean prefix, found it
        perfectly valid and returned "intact" for a journal with a torn line in
        the middle of it. An audit log that reports itself intact when it is
        not is worse than having none.
        """
        entries: List[JournalEntry] = []
        corrupt_at: Optional[int] = None
        highest = 0
        if not self._path.exists():
            return entries, None, 0
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                for number, raw_line in enumerate(handle, start=1):
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                        entry = JournalEntry.from_dict(parsed)
                    except (ValueError, TypeError):
                        if corrupt_at is None:
                            corrupt_at = number
                            logger.error(
                                "unreadable journal line %d in %s; the chain is "
                                "broken from here and verify() will say so",
                                number,
                                self._path,
                            )
                        continue
                    highest = max(highest, entry.sequence)
                    if corrupt_at is None:
                        entries.append(entry)
        except OSError as exc:
            logger.error("journal at %s could not be read: %s", self._path, exc)
        return entries, corrupt_at, highest

    def _read(self) -> Iterator[JournalEntry]:
        """The readable entries, stopping at the first corrupt line.

        Lenient on purpose: this feeds display (``tail``, the dashboard), where
        showing what can be read beats showing nothing. Integrity questions go
        through :meth:`verify`, which uses :meth:`_scan` and does not forgive.
        """
        entries, _corrupt_at, _highest = self._scan()
        yield from entries

    def entries(self) -> List[JournalEntry]:
        return list(self._read())

    def tail(self, limit: int = 50) -> List[JournalEntry]:
        entries = self.entries()
        return entries[-limit:] if limit > 0 else entries

    # -- integrity ---------------------------------------------------------

    def verify(self) -> Tuple[bool, Optional[int]]:
        """Whether the chain is intact, and the sequence where it first is not."""
        entries, corrupt_at, _highest = self._scan()
        previous = GENESIS
        expected_sequence = 1
        for entry in entries:
            if entry.sequence != expected_sequence:
                return False, entry.sequence
            if entry.previous_hash != previous:
                return False, entry.sequence
            recomputed = _digest(entry.previous_hash, entry.payload())
            if recomputed != entry.entry_hash:
                return False, entry.sequence
            previous = entry.entry_hash
            expected_sequence += 1
        if corrupt_at is not None:
            # A line nothing can parse is a break in the chain, whatever the
            # entries before it look like. Reported as the sequence that would
            # have come next, which is where a reader should start looking.
            return False, expected_sequence
        return True, None
