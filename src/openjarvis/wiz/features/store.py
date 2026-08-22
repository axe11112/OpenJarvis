"""Persistence for feature requests.

SQLite, in its own database file rather than sharing the incident store's. The
two are separate systems with separate lifecycles, and a feature-request schema
migration should not be able to take the incident database with it — that
database is what the reliability subsystem uses to know whether the site is
broken.

The whole request is stored as one JSON document with a few columns lifted out
for querying. The document is the record of what happened, and its shape is
allowed to grow as the pipeline learns to record more; the columns exist only so
"show me everything building right now" does not mean deserialising everything.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, List, Optional

from openjarvis.wiz.features.model import FeatureRequest, FeatureState

logger = logging.getLogger(__name__)

__all__ = ["FeatureStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS features (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL,
    priority     TEXT NOT NULL,
    risk         TEXT NOT NULL DEFAULT 'LOW',
    target       TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT '',
    document     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS features_state ON features(state);
CREATE INDEX IF NOT EXISTS features_priority ON features(priority);
CREATE INDEX IF NOT EXISTS features_created ON features(created_at);

-- A high-water mark that survives deletion. Deriving the next identifier from
-- the highest surviving row would hand FEAT-00002 out twice if the first
-- FEAT-00002 were ever deleted, and by then that name may already appear in a
-- branch, a pull request and the journal.
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


class FeatureStore:
    """Feature requests on disk."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)

    @property
    def path(self) -> Path:
        return self._path

    # -- identity ----------------------------------------------------------

    def next_id(self) -> str:
        """Reserve and return the next ``FEAT-00001``-style identifier.

        This *consumes* a number rather than peeking at one. An identifier that
        has ever been handed out may already name a branch, a pull request and a
        row in the journal, so it must never be handed out again — not even if
        the request that held it was deleted, and not even if two callers ask at
        the same moment. Skipping a number when a caller asks and then does
        nothing costs nothing; reusing one corrupts history.
        """
        with self._lock, self._conn:
            # Seed from any pre-existing rows the first time, so a database
            # written before this counter existed does not restart at 1.
            row = self._conn.execute(
                "SELECT value FROM counters WHERE name='feature_id'"
            ).fetchone()
            if row is None:
                highest = 0
                existing = self._conn.execute(
                    "SELECT id FROM features WHERE id LIKE 'FEAT-%'"
                ).fetchall()
                for record in existing:
                    try:
                        highest = max(highest, int(str(record["id"]).split("-", 1)[1]))
                    except (IndexError, ValueError):
                        continue
            else:
                highest = int(row["value"])

            allocated = highest + 1
            self._conn.execute(
                "INSERT INTO counters (name, value) VALUES ('feature_id', ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (allocated,),
            )
        return f"FEAT-{allocated:05d}"

    # -- writing -----------------------------------------------------------

    def create(self, feature: FeatureRequest) -> FeatureRequest:
        if not feature.id:
            feature.id = self.next_id()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO features "
                "(id, title, state, priority, risk, target, source, "
                " created_at, updated_at, document) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._row(feature),
            )
        return feature

    def save(self, feature: FeatureRequest) -> FeatureRequest:
        """Write *feature* back, refusing to invent one that was never created."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE features SET title=?, state=?, priority=?, risk=?, "
                "target=?, source=?, created_at=?, updated_at=?, document=? "
                "WHERE id=?",
                self._row(feature)[1:] + (feature.id,),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"no feature request {feature.id!r} to save")
        return feature

    @staticmethod
    def _row(feature: FeatureRequest) -> tuple:
        return (
            feature.id,
            feature.title,
            feature.state.value,
            feature.priority.value,
            feature.risk,
            feature.target,
            feature.source,
            feature.created_at,
            feature.updated_at,
            json.dumps(feature.to_dict(), sort_keys=True),
        )

    def delete(self, feature_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM features WHERE id=?", (feature_id,))

    # -- reading -----------------------------------------------------------

    def get(self, feature_id: str) -> Optional[FeatureRequest]:
        with self._lock:
            row = self._conn.execute(
                "SELECT document FROM features WHERE id=?", (feature_id,)
            ).fetchone()
        return self._hydrate(row)

    def list(
        self,
        *,
        states: Optional[Iterable[FeatureState]] = None,
        limit: int = 50,
    ) -> List[FeatureRequest]:
        query = "SELECT document FROM features"
        params: List[Any] = []
        if states is not None:
            wanted = [FeatureState.parse(s).value for s in states]
            if not wanted:
                return []
            query += f" WHERE state IN ({','.join('?' * len(wanted))})"
            params.extend(wanted)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [f for f in (self._hydrate(row) for row in rows) if f is not None]

    def active(self, limit: int = 50) -> List[FeatureRequest]:
        """Everything Wiz is still working on."""
        from openjarvis.wiz.features.model import TERMINAL_STATES

        return self.list(
            states=[s for s in FeatureState if s not in TERMINAL_STATES],
            limit=limit,
        )

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM features").fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def _hydrate(row: Optional[sqlite3.Row]) -> Optional[FeatureRequest]:
        if row is None:
            return None
        try:
            return FeatureRequest.from_dict(json.loads(row["document"]))
        except (ValueError, TypeError) as exc:
            logger.error("a stored feature request could not be read: %s", exc)
            return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
