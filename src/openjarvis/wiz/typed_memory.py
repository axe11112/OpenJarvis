"""Typed memory with full provenance for Wiz's reasoning and learning.

Wiz must distinguish between:
- Facts: observed truths
- Decisions: what was chosen and why
- Preferences: what the operator prefers
- Inferences: what was deduced (never silently become facts)
- Temporary: short-lived working knowledge
- Incident lessons: what was learned from incidents
- Engineering lessons: what was learned from engineering work

Every entry has full provenance so corrections supersede old information rather
than destroying the audit trail. An inference can only become a fact through
operator correction or external verification.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "MemoryCategory",
    "MemorySource",
    "TypedMemory",
    "TypedMemoryStore",
]


class MemoryCategory(str, Enum):
    """What kind of knowledge this is."""

    FACT = "fact"
    DECISION = "decision"
    PREFERENCE = "preference"
    INFERENCE = "inference"
    TEMPORARY = "temporary"
    INCIDENT_LESSON = "incident_lesson"
    ENGINEERING_LESSON = "engineering_lesson"


class MemorySource(str, Enum):
    """Where this knowledge came from."""

    OPERATOR = "operator"  # directly from the owner
    INFERENCE = "inference"  # deduced by Wiz
    OBSERVATION = "observation"  # learned from running code
    CORRECTION = "correction"  # operator correction of previous entry
    INCIDENT = "incident"  # extracted from incident analysis
    PATTERN = "pattern"  # discovered by pattern detection


@dataclass(slots=True)
class TypedMemory:
    """One piece of Wiz's reasoning or learning.

    Identity is stable (UUID) so corrections can reference what they replace.
    Provenance is full so the audit trail is never lost.
    Confidence allows expression of certainty.
    Supersession means old information is not deleted but marked as replaced.
    """

    id: str
    category: MemoryCategory
    content: str
    source: MemorySource
    source_id: str = ""  # reference: feature ID, incident ID, probe name, etc.
    created_at: str = ""  # ISO format
    updated_at: str = ""
    confidence: float = 1.0  # 0.0-1.0; inferences usually < 1.0
    supersedes: Optional[str] = None  # ID of entry this replaced
    superseded_by: Optional[str] = None  # ID of entry that replaced this
    active: bool = True  # soft-delete flag
    expires_at: Optional[str] = None  # for TEMPORARY category; ISO format
    provenance: Dict[str, Any] = field(default_factory=dict)  # audit trail

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "id": self.id,
            "category": self.category.value,
            "content": self.content,
            "source": self.source.value,
            "source_id": self.source_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "confidence": self.confidence,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "active": self.active,
            "expires_at": self.expires_at,
            "provenance": self.provenance,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> TypedMemory:
        """Deserialize from JSON dict."""
        return TypedMemory(
            id=data.get("id", ""),
            category=MemoryCategory(data.get("category", "fact")),
            content=data.get("content", ""),
            source=MemorySource(data.get("source", "observation")),
            source_id=data.get("source_id", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            confidence=float(data.get("confidence", 1.0)),
            supersedes=data.get("supersedes"),
            superseded_by=data.get("superseded_by"),
            active=bool(data.get("active", True)),
            expires_at=data.get("expires_at"),
            provenance=data.get("provenance", {}),
        )


class TypedMemoryStore:
    """SQLite-backed typed memory store with FTS5 search.

    Stores memories with full provenance. Supports:
    - remember: add or update
    - retrieve: get by ID
    - retrieve_active: get active entries (optionally by category)
    - search: full-text search over content
    - correct: operator correction (supersedes old entry)
    - supersede: mark one entry as replaced by another
    - forget: soft-delete one entry
    - clear_category: soft-delete all in a category
    - inspect_provenance: get audit trail for an entry

    The critical rule: an inference is marked as source=INFERENCE with
    confidence < 1.0, and can only become a fact through operator correction
    (source=CORRECTION) which creates a new entry that supersedes the old one.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(self._schema())
        self._fts = self._try_fts()

    @staticmethod
    def _schema() -> str:
        """Schema for typed memory storage."""
        return """
CREATE TABLE IF NOT EXISTS typed_memory (
    id                TEXT PRIMARY KEY,
    category          TEXT NOT NULL,
    content           TEXT NOT NULL,
    source            TEXT NOT NULL,
    source_id         TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    confidence        REAL NOT NULL DEFAULT 1.0,
    supersedes        TEXT DEFAULT NULL,
    superseded_by     TEXT DEFAULT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    expires_at        TEXT DEFAULT NULL,
    provenance        TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_category ON typed_memory(category);
CREATE INDEX IF NOT EXISTS idx_active ON typed_memory(active);
CREATE INDEX IF NOT EXISTS idx_created_at ON typed_memory(created_at);
CREATE INDEX IF NOT EXISTS idx_supersedes ON typed_memory(supersedes);
CREATE INDEX IF NOT EXISTS idx_superseded_by ON typed_memory(superseded_by);

CREATE VIRTUAL TABLE IF NOT EXISTS typed_memory_fts USING fts5(
    content,
    category,
    content='typed_memory',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS typed_memory_ai AFTER INSERT ON typed_memory BEGIN
    INSERT INTO typed_memory_fts(rowid, content, category)
    VALUES (new.rowid, new.content, new.category);
END;

CREATE TRIGGER IF NOT EXISTS typed_memory_ad AFTER DELETE ON typed_memory BEGIN
    INSERT INTO typed_memory_fts(typed_memory_fts, rowid, content, category)
    VALUES ('delete', old.rowid, old.content, old.category);
END;

CREATE TRIGGER IF NOT EXISTS typed_memory_au AFTER UPDATE ON typed_memory BEGIN
    INSERT INTO typed_memory_fts(typed_memory_fts, rowid, content, category)
    VALUES ('delete', old.rowid, old.content, old.category);
    INSERT INTO typed_memory_fts(rowid, content, category)
    VALUES (new.rowid, new.content, new.category);
END;
"""

    def _try_fts(self) -> bool:
        """Check if FTS5 is available; degrade gracefully if not."""
        try:
            with self._conn:
                self._conn.execute("SELECT * FROM typed_memory_fts LIMIT 1")
            return True
        except sqlite3.OperationalError:
            logger.warning("FTS5 not available; search will use LIKE matching")
            return False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def full_text_search(self) -> bool:
        return self._fts

    # -- Writing ---------------------------------------------------------------

    def remember(self, memory: TypedMemory) -> None:
        """Store or update a memory entry.

        If memory.id exists, it updates. Otherwise, generates a stable UUID.
        """
        with self._lock:
            self._remember_locked(memory)

    def _remember_locked(self, memory: TypedMemory) -> None:
        """The body of :meth:`remember`, for a caller that already holds the
        lock — ``correct`` and ``supersede`` need their own update and this
        insert to commit as one unit, and ``self._lock`` is a plain
        ``threading.Lock``, not reentrant: calling :meth:`remember` from
        inside their own ``with self._lock`` would deadlock forever rather
        than raise, which is the failure mode worth avoiding a second call
        path for.
        """
        if not memory.id:
            memory.id = f"{memory.category.value}:{uuid.uuid4().hex[:8]}"

        now = datetime.now(timezone.utc).isoformat()
        if not memory.created_at:
            memory.created_at = now
        memory.updated_at = now

        self._conn.execute(
            """
INSERT OR REPLACE INTO typed_memory
(id, category, content, source, source_id, created_at, updated_at,
 confidence, supersedes, superseded_by, active, expires_at, provenance)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""",
            (
                memory.id,
                memory.category.value,
                memory.content,
                memory.source.value,
                memory.source_id,
                memory.created_at,
                memory.updated_at,
                memory.confidence,
                memory.supersedes,
                memory.superseded_by,
                1 if memory.active else 0,
                memory.expires_at,
                json.dumps(memory.provenance, sort_keys=True),
            ),
        )
        self._conn.commit()

    def remember_fact(
        self,
        category: MemoryCategory,
        content: str,
        source: MemorySource,
        confidence: float = 1.0,
        source_id: str = "",
    ) -> str:
        """Convenience method: create and store a memory entry.

        Returns the generated memory ID.
        """
        memory = TypedMemory(
            id="",
            category=category,
            content=content,
            source=source,
            source_id=source_id,
            confidence=confidence,
        )
        self.remember(memory)
        return memory.id

    def correct(
        self,
        old_id: str,
        new_content: str,
        reason: str,
        operator_id: str = "operator",
    ) -> Optional[str]:
        """Operator correction: create new FACT, mark old as superseded.

        This is the critical rule enforcement: an inference becomes a fact
        only through explicit operator correction, which creates a new entry
        and preserves the full audit trail.

        Returns the ID of the new entry.
        """
        old = self.retrieve(old_id)
        if not old:
            return None

        now = datetime.now(timezone.utc).isoformat()
        new_id = f"fact:{uuid.uuid4().hex[:8]}"

        new_memory = TypedMemory(
            id=new_id,
            category=MemoryCategory.FACT,
            content=new_content,
            source=MemorySource.CORRECTION,
            source_id=old_id,
            created_at=now,
            updated_at=now,
            confidence=1.0,
            supersedes=old_id,
            active=True,
            provenance={
                "corrected_at": now,
                "operator": operator_id,
                "reason": reason,
                "corrected_from": {
                    "category": old.category.value,
                    "source": old.source.value,
                    "confidence": old.confidence,
                    "content": old.content,
                },
            },
        )

        with self._lock:
            old_up = self._conn.execute(
                "UPDATE typed_memory SET superseded_by = ?, updated_at = ? WHERE id = ?",
                (new_id, now, old_id),
            )
            self._remember_locked(new_memory)

        return new_id

    def supersede(self, old_id: str, new_memory: TypedMemory) -> None:
        """Mark old_id as superseded by new_memory.

        Similar to correct() but for programmatic supersession (not operator
        correction). Both old and new entries are kept in the audit trail.
        """
        now = datetime.now(timezone.utc).isoformat()
        new_memory.supersedes = old_id
        new_memory.updated_at = now

        with self._lock:
            self._conn.execute(
                "UPDATE typed_memory SET superseded_by = ?, updated_at = ? WHERE id = ?",
                (new_memory.id, now, old_id),
            )
            self._remember_locked(new_memory)

    # -- Reading ---------------------------------------------------------------

    def retrieve(self, memory_id: str) -> Optional[TypedMemory]:
        """Get a single memory entry by ID (including inactive ones)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM typed_memory WHERE id = ?", (memory_id,)
            ).fetchone()
        if not row:
            return None
        return self._hydrate(row)

    def retrieve_active(
        self, category: Optional[MemoryCategory] = None
    ) -> List[TypedMemory]:
        """Get all active memories, optionally filtered by category."""
        sql = "SELECT * FROM typed_memory WHERE active = 1"
        params: List[Any] = []
        if category:
            sql += " AND category = ?"
            params.append(category.value)
        sql += " ORDER BY created_at DESC"

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._hydrate(row) for row in rows]

    def search(
        self,
        query: str,
        category: Optional[MemoryCategory] = None,
        limit: int = 50,
    ) -> List[TypedMemory]:
        """Full-text search over content.

        Falls back to LIKE if FTS5 is unavailable.
        """
        query = (query or "").strip()
        if not query:
            return self.retrieve_active(category)

        if self._fts:
            return self._search_fts(query, category, limit)
        return self._search_like(query, category, limit)

    def _search_fts(
        self,
        query: str,
        category: Optional[MemoryCategory],
        limit: int,
    ) -> List[TypedMemory]:
        """FTS5-backed search."""
        terms = [t for t in query.split() if len(t) > 1]
        if not terms:
            return self.retrieve_active(category)

        match = " OR ".join(terms)
        sql = (
            "SELECT m.* FROM typed_memory_fts "
            "JOIN typed_memory m ON m.rowid = typed_memory_fts.rowid "
            "WHERE typed_memory_fts MATCH ? AND m.active = 1"
        )
        params: List[Any] = [match]
        if category:
            sql += " AND m.category = ?"
            params.append(category.value)
        sql += " ORDER BY m.created_at DESC LIMIT ?"
        params.append(int(limit))

        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("FTS search failed, falling back to LIKE: %s", exc)
            return self._search_like(query, category, limit)
        return [self._hydrate(row) for row in rows]

    def _search_like(
        self,
        query: str,
        category: Optional[MemoryCategory],
        limit: int,
    ) -> List[TypedMemory]:
        """LIKE-based search fallback."""
        terms = [t for t in query.split() if len(t) > 2][:6]
        if not terms:
            return self.retrieve_active(category)

        clauses = " OR ".join(["content LIKE ?"] * len(terms))
        params: List[Any] = [f"%{term}%" for term in terms]
        sql = f"SELECT * FROM typed_memory WHERE active = 1 AND ({clauses})"
        if category:
            sql += " AND category = ?"
            params.append(category.value)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._hydrate(row) for row in rows]

    # -- Deletion ---------------------------------------------------------------

    def forget(self, memory_id: str) -> bool:
        """Soft-delete one memory entry."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            result = self._conn.execute(
                "UPDATE typed_memory SET active = 0, updated_at = ? WHERE id = ?",
                (now, memory_id),
            )
            self._conn.commit()
        return result.rowcount > 0

    def clear_category(self, category: MemoryCategory) -> int:
        """Soft-delete all entries in a category."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            result = self._conn.execute(
                "UPDATE typed_memory SET active = 0, updated_at = ? WHERE category = ?",
                (now, category.value),
            )
            self._conn.commit()
        return result.rowcount

    # -- Inspection ---------------------------------------------------------------

    def inspect_provenance(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Get the full audit trail for an entry.

        Includes the entry itself, what it supersedes, and what superseded it.
        """
        entry = self.retrieve(memory_id)
        if not entry:
            return None

        result: Dict[str, Any] = {
            "entry": entry.to_dict(),
            "provenance": entry.provenance,
            "chain": [],
        }

        # Follow supersession chain back (what it replaced)
        current_id: Optional[str] = entry.supersedes
        while current_id:
            prev = self.retrieve(current_id)
            if not prev:
                break
            result["chain"].append(
                {
                    "direction": "supersedes",
                    "entry": prev.to_dict(),
                }
            )
            current_id = prev.supersedes

        # Follow supersession chain forward (what replaced it)
        current_id = entry.superseded_by
        while current_id:
            next_entry = self.retrieve(current_id)
            if not next_entry:
                break
            result["chain"].append(
                {
                    "direction": "superseded_by",
                    "entry": next_entry.to_dict(),
                }
            )
            current_id = next_entry.superseded_by

        return result

    # -- Utilities ---------------------------------------------------------------

    def count(self, active_only: bool = True) -> int:
        """Return count of entries."""
        sql = "SELECT COUNT(*) FROM typed_memory"
        if active_only:
            sql += " WHERE active = 1"
        with self._lock:
            row = self._conn.execute(sql).fetchone()
        return int(row[0]) if row else 0

    def count_by_category(self, active_only: bool = True) -> Dict[str, int]:
        """Return count by category."""
        sql = "SELECT category, COUNT(*) FROM typed_memory"
        if active_only:
            sql += " WHERE active = 1"
        sql += " GROUP BY category ORDER BY category"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return {row[0]: row[1] for row in rows}

    @staticmethod
    def _hydrate(row: sqlite3.Row) -> TypedMemory:
        """Deserialize from database row."""
        try:
            provenance = json.loads(row["provenance"] or "{}")
        except (ValueError, TypeError):
            provenance = {}

        return TypedMemory(
            id=row["id"],
            category=MemoryCategory(row["category"]),
            content=row["content"],
            source=MemorySource(row["source"]),
            source_id=row["source_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            confidence=float(row["confidence"]),
            supersedes=row["supersedes"],
            superseded_by=row["superseded_by"],
            active=bool(row["active"]),
            expires_at=row["expires_at"],
            provenance=provenance,
        )

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()
