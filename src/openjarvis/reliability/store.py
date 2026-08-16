"""SQLite persistence for incidents, evidence, repair attempts and transitions.

Follows the store conventions in :mod:`openjarvis.scheduler.store`, with one
addition: the transition log is **append-only and hash-chained**, mirroring the
Merkle approach in :class:`openjarvis.security.audit.AuditLogger`.

The security ``AuditLogger`` is not reused directly here because its schema is
shaped for scan findings (``findings_json``, ``content_preview``,
``action_taken``) and its ``SecurityEventType`` taxonomy is deliberately narrow.
Rather than widen a security-specific enum with reliability concerns, the
transition log carries its own chain and answers "why did JARVIS do that?" with
the same tamper-evidence guarantee.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openjarvis.reliability.events import (
    RELIABILITY_INCIDENT_OPENED,
    RELIABILITY_INCIDENT_TRANSITION,
)
from openjarvis.reliability.types import (
    Evidence,
    Incident,
    IncidentState,
    IncidentTransition,
    RepairAttempt,
    Severity,
    now_iso,
)

logger = logging.getLogger(__name__)

__all__ = ["IncidentStore"]

_CREATE_INCIDENTS = """\
CREATE TABLE IF NOT EXISTS incidents (
    id            TEXT PRIMARY KEY,
    fingerprint   TEXT    NOT NULL,
    severity      TEXT    NOT NULL,
    component     TEXT    NOT NULL DEFAULT '',
    title         TEXT    NOT NULL DEFAULT '',
    summary       TEXT    NOT NULL DEFAULT '',
    environment   TEXT    NOT NULL DEFAULT 'production',
    source        TEXT    NOT NULL DEFAULT 'probe',
    probe_id      TEXT    NOT NULL DEFAULT '',
    state         TEXT    NOT NULL DEFAULT 'DETECTED',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    occurrences   INTEGER NOT NULL DEFAULT 1,
    last_seen_at  TEXT    NOT NULL,
    repro_steps   TEXT    NOT NULL DEFAULT '[]',
    correlation   TEXT    NOT NULL DEFAULT '{}',
    resolution    TEXT    NOT NULL DEFAULT '{}',
    metadata      TEXT    NOT NULL DEFAULT '{}'
);
"""

_CREATE_EVIDENCE = """\
CREATE TABLE IF NOT EXISTS incident_evidence (
    id            TEXT PRIMARY KEY,
    incident_id   TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    summary       TEXT    NOT NULL DEFAULT '',
    content       TEXT    NOT NULL DEFAULT '',
    artifact_path TEXT    NOT NULL DEFAULT '',
    source        TEXT    NOT NULL DEFAULT '',
    trust         TEXT    NOT NULL DEFAULT 'external',
    created_at    TEXT    NOT NULL,
    metadata      TEXT    NOT NULL DEFAULT '{}'
);
"""

_CREATE_ATTEMPTS = """\
CREATE TABLE IF NOT EXISTS incident_attempts (
    incident_id   TEXT    NOT NULL,
    number        INTEGER NOT NULL,
    payload       TEXT    NOT NULL DEFAULT '{}',
    PRIMARY KEY (incident_id, number)
);
"""

_CREATE_TRANSITIONS = """\
CREATE TABLE IF NOT EXISTS incident_transitions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id   TEXT    NOT NULL,
    from_state    TEXT    NOT NULL,
    to_state      TEXT    NOT NULL,
    actor         TEXT    NOT NULL DEFAULT 'jarvis',
    reason        TEXT    NOT NULL DEFAULT '',
    at            TEXT    NOT NULL,
    row_hash      TEXT    NOT NULL DEFAULT '',
    prev_hash     TEXT    NOT NULL DEFAULT ''
);
"""

_CREATE_SEQUENCE = """\
CREATE TABLE IF NOT EXISTS incident_sequence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    allocated_at TEXT NOT NULL
);
"""

_CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint ON incidents (fingerprint);",
    "CREATE INDEX IF NOT EXISTS idx_incidents_state ON incidents (state);",
    "CREATE INDEX IF NOT EXISTS idx_evidence_incident"
    " ON incident_evidence (incident_id);",
    "CREATE INDEX IF NOT EXISTS idx_transitions_incident"
    " ON incident_transitions (incident_id);",
)

_INSERT_INCIDENT = """\
INSERT OR REPLACE INTO incidents
    (id, fingerprint, severity, component, title, summary, environment,
     source, probe_id, state, created_at, updated_at, occurrences,
     last_seen_at, repro_steps, correlation, resolution, metadata)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_EVIDENCE = """\
INSERT OR REPLACE INTO incident_evidence
    (id, incident_id, kind, summary, content, artifact_path, source,
     trust, created_at, metadata)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

#: Incident IDs are zero-padded to this width: ``INC-00042``.
_ID_WIDTH = 5


class IncidentStore:
    """CRUD store for incidents, with an append-only hash-chained history.

    Parameters
    ----------
    db_path:
        Path to the SQLite database.  Parent directories are created.
    bus:
        Optional :class:`~openjarvis.core.events.EventBus`.  When supplied,
        incident lifecycle events are published (see
        :mod:`openjarvis.reliability.events`).
    """

    def __init__(self, db_path: str | Path, *, bus: Any = None) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._bus = bus
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        for statement in (
            _CREATE_INCIDENTS,
            _CREATE_EVIDENCE,
            _CREATE_ATTEMPTS,
            _CREATE_TRANSITIONS,
            _CREATE_SEQUENCE,
            *_CREATE_INDEXES,
        ):
            self._conn.execute(statement)
        self._conn.commit()

    # -- ID allocation ----------------------------------------------------

    def next_id(self) -> str:
        """Allocate the next monotonic incident ID, e.g. ``INC-00042``.

        Uses an ``AUTOINCREMENT`` sequence table so IDs are never reused even
        after an incident is deleted.
        """
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO incident_sequence (allocated_at) VALUES (?)",
                (now_iso(),),
            )
            self._conn.commit()
            seq = int(cursor.lastrowid or 1)
        return f"INC-{seq:0{_ID_WIDTH}d}"

    # -- Create / read ----------------------------------------------------

    def create(self, incident: Incident) -> Incident:
        """Persist a new incident, assigning an ID when it has none.

        The incident's ``DETECTED`` origin is written to the transition log so
        every incident's history starts from a recorded entry.
        """
        with self._lock:
            if not incident.id:
                incident.id = self.next_id()
            self._write_incident(incident)
            for evidence in incident.evidence:
                self._write_evidence(incident.id, evidence)
            for attempt in incident.attempts:
                self._write_attempt(incident.id, attempt)
            if incident.transitions:
                for transition in incident.transitions:
                    self._append_transition(incident.id, transition)
            else:
                self._append_transition(
                    incident.id,
                    IncidentTransition(
                        from_state=incident.state,
                        to_state=incident.state,
                        actor="jarvis",
                        reason="incident opened",
                    ),
                )
            self._conn.commit()

        self._publish(RELIABILITY_INCIDENT_OPENED, incident)
        logger.info(
            "Incident %s opened: [%s] %s",
            incident.id,
            incident.severity.value,
            incident.title,
        )
        return incident

    def get(self, incident_id: str) -> Optional[Incident]:
        """Load a full incident by ID, or ``None`` when it does not exist."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                return None
            return self._hydrate(row)

    def list(
        self,
        *,
        state: Optional[IncidentState] = None,
        severity: Optional[Severity] = None,
        open_only: bool = False,
        limit: int = 100,
    ) -> List[Incident]:
        """List incidents, newest first, with optional filters."""
        clauses: List[str] = []
        params: List[Any] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(IncidentState.parse(state).value)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(Severity.parse(severity).value)
        if open_only:
            clauses.append("state != ?")
            params.append(IncidentState.RESOLVED.value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM incidents{where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [self._hydrate(row) for row in rows]

    def list_by_fingerprint(
        self,
        fingerprint: str,
        *,
        include_resolved: bool = False,
        limit: int = 50,
    ) -> List[Incident]:
        """Every incident matching *fingerprint*, newest first.

        Distinct from :meth:`find_by_fingerprint`, which answers "is this
        recurrence already open" and so returns only the newest. Callers that
        need the *history* of a fingerprint — has a repair for this ever gone
        wrong? — must not be handed the newest one, because the newest is
        usually the fresh incident asking the question.
        """
        query = "SELECT * FROM incidents WHERE fingerprint = ?"
        params: List[Any] = [fingerprint]
        if not include_resolved:
            query += " AND state != ?"
            params.append(IncidentState.RESOLVED.value)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
            return [self._hydrate(row) for row in rows]

    def find_by_fingerprint(
        self,
        fingerprint: str,
        *,
        include_resolved: bool = False,
    ) -> Optional[Incident]:
        """Return the most recent incident matching *fingerprint*.

        Resolved incidents are excluded by default so a recurrence after a fix
        opens a fresh incident rather than reviving a closed one.
        """
        query = "SELECT * FROM incidents WHERE fingerprint = ?"
        params: List[Any] = [fingerprint]
        if not include_resolved:
            query += " AND state != ?"
            params.append(IncidentState.RESOLVED.value)
        query += " ORDER BY created_at DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
            return self._hydrate(row) if row is not None else None

    # -- Mutation ---------------------------------------------------------

    def save(self, incident: Incident) -> Incident:
        """Upsert an incident's scalar fields (not its child collections)."""
        with self._lock:
            self._write_incident(incident)
            self._conn.commit()
        return incident

    def transition(
        self,
        incident: Incident,
        state: IncidentState,
        *,
        actor: str = "jarvis",
        reason: str = "",
    ) -> Incident:
        """Move *incident* to *state*, persisting and chaining the transition.

        Raises :class:`~openjarvis.reliability.types.InvalidTransitionError`
        when the move is illegal; nothing is written in that case.
        """
        transition = incident.transition_to(state, actor=actor, reason=reason)
        with self._lock:
            self._write_incident(incident)
            self._append_transition(incident.id, transition)
            self._conn.commit()

        self._publish(RELIABILITY_INCIDENT_TRANSITION, incident, reason=reason)
        logger.info(
            "Incident %s: %s -> %s (%s)",
            incident.id,
            transition.from_state.value,
            transition.to_state.value,
            reason or actor,
        )
        return incident

    def record_audit(
        self, incident: Incident, *, actor: str, reason: str
    ) -> IncidentTransition:
        """Append a hash-chained audit entry without moving the incident.

        Some decisions must be tamper-evident but are not lifecycle changes. A
        refused merge is the motivating case: nothing about the incident
        changed, and yet "JARVIS considered merging and would not" is exactly
        the kind of fact an audit log exists to preserve — arguably more so than
        the merges that went ahead.

        The entry is written into the same append-only chain as every
        transition, with ``from_state == to_state``, so ``verify-audit`` covers
        it and ``incident show`` displays it in order alongside everything else.
        It deliberately does not go through :meth:`Incident.transition_to`: that
        method enforces the lifecycle graph, which has no self-edges, and
        loosening the graph to allow bookkeeping would also allow a real
        state machine bug to go unnoticed.
        """
        transition = IncidentTransition(
            from_state=incident.state,
            to_state=incident.state,
            actor=actor,
            reason=reason,
        )
        incident.transitions.append(transition)
        with self._lock:
            self._append_transition(incident.id, transition)
            self._conn.commit()
        logger.info("Incident %s audit (%s): %s", incident.id, actor, reason)
        return transition

    def add_evidence(self, incident: Incident, evidence: Evidence) -> Evidence:
        """Attach evidence to *incident* and persist it."""
        incident.add_evidence(evidence)
        with self._lock:
            self._write_evidence(incident.id, evidence)
            self._write_incident(incident)
            self._conn.commit()
        return evidence

    def add_attempt(self, incident: Incident, attempt: RepairAttempt) -> RepairAttempt:
        """Record a repair attempt on *incident* and persist it."""
        incident.add_attempt(attempt)
        with self._lock:
            self._write_attempt(incident.id, attempt)
            self._write_incident(incident)
            self._conn.commit()
        return attempt

    def update_attempt(
        self, incident: Incident, attempt: RepairAttempt
    ) -> RepairAttempt:
        """Persist changes to an already-recorded attempt (same ``number``)."""
        with self._lock:
            self._write_attempt(incident.id, attempt)
            self._write_incident(incident)
            self._conn.commit()
        return attempt

    def record_occurrence(self, incident: Incident, at: Optional[str] = None) -> int:
        """Note a repeat observation of the same failure."""
        count = incident.record_occurrence(at)
        with self._lock:
            self._write_incident(incident)
            self._conn.commit()
        return count

    def delete(self, incident_id: str) -> None:
        """Delete an incident and its children.

        Transition history is deliberately **not** deleted — it is the audit
        trail, and removing it would defeat the purpose of chaining it.
        """
        with self._lock:
            self._conn.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))
            self._conn.execute(
                "DELETE FROM incident_evidence WHERE incident_id = ?", (incident_id,)
            )
            self._conn.execute(
                "DELETE FROM incident_attempts WHERE incident_id = ?", (incident_id,)
            )
            self._conn.commit()

    # -- History / audit --------------------------------------------------

    def transitions_for(self, incident_id: str) -> List[IncidentTransition]:
        """Return an incident's transition history, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT from_state, to_state, actor, reason, at"
                " FROM incident_transitions WHERE incident_id = ? ORDER BY id",
                (incident_id,),
            ).fetchall()
        return [
            IncidentTransition(
                from_state=IncidentState.parse(row["from_state"]),
                to_state=IncidentState.parse(row["to_state"]),
                actor=row["actor"],
                reason=row["reason"],
                at=row["at"],
            )
            for row in rows
        ]

    def tail_hash(self) -> str:
        """Return the hash of the newest transition row, or an empty string."""
        row = self._conn.execute(
            "SELECT row_hash FROM incident_transitions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["row_hash"] if row and row["row_hash"] else ""

    def verify_chain(self) -> Tuple[bool, Optional[int]]:
        """Verify the transition log's hash chain.

        Returns ``(True, None)`` when intact, or ``(False, row_id)`` naming the
        first broken link.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, incident_id, from_state, to_state, actor, reason,"
                " at, row_hash, prev_hash FROM incident_transitions ORDER BY id"
            ).fetchall()

        expected_prev = ""
        for row in rows:
            if not row["row_hash"]:
                continue
            if row["prev_hash"] != expected_prev:
                return False, int(row["id"])
            computed = self._row_hash(
                row["prev_hash"],
                row["incident_id"],
                row["from_state"],
                row["to_state"],
                row["actor"],
                row["reason"],
                row["at"],
            )
            if computed != row["row_hash"]:
                return False, int(row["id"])
            expected_prev = row["row_hash"]
        return True, None

    def count(self) -> int:
        """Return the total number of stored incidents."""
        row = self._conn.execute("SELECT COUNT(*) AS n FROM incidents").fetchone()
        return int(row["n"]) if row else 0

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    # -- Internals --------------------------------------------------------

    def _write_incident(self, incident: Incident) -> None:
        self._conn.execute(
            _INSERT_INCIDENT,
            (
                incident.id,
                incident.fingerprint,
                incident.severity.value,
                incident.component,
                incident.title,
                incident.summary,
                incident.environment,
                incident.source,
                incident.probe_id,
                incident.state.value,
                incident.created_at,
                incident.updated_at,
                incident.occurrences,
                incident.last_seen_at,
                json.dumps(incident.repro_steps),
                json.dumps(incident.correlation.to_dict()),
                json.dumps(incident.resolution.to_dict()),
                json.dumps(incident.metadata),
            ),
        )

    def _write_evidence(self, incident_id: str, evidence: Evidence) -> None:
        self._conn.execute(
            _INSERT_EVIDENCE,
            (
                evidence.id,
                incident_id,
                evidence.kind.value,
                evidence.summary,
                evidence.content,
                evidence.artifact_path,
                evidence.source,
                evidence.trust.value,
                evidence.created_at,
                json.dumps(evidence.metadata),
            ),
        )

    def _write_attempt(self, incident_id: str, attempt: RepairAttempt) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO incident_attempts"
            " (incident_id, number, payload) VALUES (?, ?, ?)",
            (incident_id, attempt.number, json.dumps(attempt.to_dict())),
        )

    def _append_transition(
        self, incident_id: str, transition: IncidentTransition
    ) -> None:
        prev_hash = self.tail_hash()
        row_hash = self._row_hash(
            prev_hash,
            incident_id,
            transition.from_state.value,
            transition.to_state.value,
            transition.actor,
            transition.reason,
            transition.at,
        )
        self._conn.execute(
            "INSERT INTO incident_transitions"
            " (incident_id, from_state, to_state, actor, reason, at,"
            "  row_hash, prev_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                incident_id,
                transition.from_state.value,
                transition.to_state.value,
                transition.actor,
                transition.reason,
                transition.at,
                row_hash,
                prev_hash,
            ),
        )

    @staticmethod
    def _row_hash(prev_hash: str, *fields: str) -> str:
        payload = "|".join([prev_hash, *fields])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _hydrate(self, row: sqlite3.Row) -> Incident:
        """Rebuild a full ``Incident`` from a row plus its child tables."""
        data: Dict[str, Any] = dict(row)
        incident_id = data["id"]
        for key in ("repro_steps", "correlation", "resolution", "metadata"):
            data[key] = _loads(
                data.get(key), default=[] if key == "repro_steps" else {}
            )

        evidence_rows = self._conn.execute(
            "SELECT * FROM incident_evidence WHERE incident_id = ? ORDER BY created_at",
            (incident_id,),
        ).fetchall()
        data["evidence"] = [
            {**dict(er), "metadata": _loads(er["metadata"], default={})}
            for er in evidence_rows
        ]

        attempt_rows = self._conn.execute(
            "SELECT payload FROM incident_attempts WHERE incident_id = ?"
            " ORDER BY number",
            (incident_id,),
        ).fetchall()
        data["attempts"] = [_loads(ar["payload"], default={}) for ar in attempt_rows]

        data["transitions"] = [t.to_dict() for t in self.transitions_for(incident_id)]
        return Incident.from_dict(data)

    def _publish(self, event_name: str, incident: Incident, **extra: Any) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publish(
                event_name,
                {
                    "incident_id": incident.id,
                    "state": incident.state.value,
                    "severity": incident.severity.value,
                    "component": incident.component,
                    **extra,
                },
            )
        except Exception:  # pragma: no cover - a bad subscriber must not break us
            logger.exception("Failed to publish %s for %s", event_name, incident.id)


def _loads(raw: Any, *, default: Any) -> Any:
    """Parse a JSON column, falling back to *default* on malformed data."""
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default
