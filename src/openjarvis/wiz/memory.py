"""What was built, why, and how to find it again.

§27 and §28. The questions this exists to answer are the ones an operator
actually asks — *what did we build yesterday?*, *why did we change onboarding?*,
*what is being built now?* — and the reason they need their own store is that
the feature database cannot answer the middle one. A feature request records
what happened; it does not record the decision behind it, and the decision is
usually the part worth remembering. Six months later nobody needs to know that
``FEAT-00042`` passed its gates; they need to know that the download button
exists because coaches were emailing screenshots.

Three deliberate limits:

**No embeddings, no model.** SQLite's FTS5 with BM25 ranking, which is built
into the Python that is already installed. §37 rules out a local model on a 2019
laptop with 8 GB, and the honest observation is that BM25 over a few thousand
short records is a better product than a vector index nobody can afford to keep
warm. When FTS5 is unavailable the store degrades to ``LIKE`` matching rather
than failing — a slower answer is a far better outcome than no memory.

**No reasoning is stored.** Entries hold what was decided and what happened,
never a model's chain of thought. §27 says so, and there is a practical reason
too: stored reasoning reads as authoritative six months later, when what it
actually records is one session's guess.

**Everything is attributable.** Every entry names its kind, its subject and when
it happened, so an answer can always be traced back to the thing it came from.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = ["MemoryEntry", "ProductMemory", "KINDS"]

#: What can be remembered. A closed set, because an entry whose kind nobody
#: recognises cannot be filtered, ranked or explained.
KINDS = (
    "feature",  # something the operator asked to be built
    "decision",  # a product decision, and the reason for it
    "release",  # something reaching users
    "incident",  # a reliability event, summarised from the other subsystem
    "research",  # what was learned looking outward
    "proposal",  # maintenance Wiz suggested
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    subject    TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    at         TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    metadata   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS memory_kind ON memory(kind);
CREATE INDEX IF NOT EXISTS memory_at ON memory(at);
CREATE UNIQUE INDEX IF NOT EXISTS memory_identity ON memory(kind, subject, title);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    title, body, subject,
    content='memory', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, title, body, subject)
    VALUES (new.id, new.title, new.body, new.subject);
END;
CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, title, body, subject)
    VALUES ('delete', old.id, old.title, old.body, old.subject);
END;
CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, title, body, subject)
    VALUES ('delete', old.id, old.title, old.body, old.subject);
    INSERT INTO memory_fts(rowid, title, body, subject)
    VALUES (new.id, new.title, new.body, new.subject);
END;
"""

#: Characters FTS5 treats as syntax. Stripped from operator queries, because
#: "what did we ship?" must be a search rather than a syntax error.
_FTS_SYNTAX = re.compile(r'[":^*(){}\[\]]')


@dataclass(slots=True)
class MemoryEntry:
    """One thing worth remembering."""

    kind: str
    title: str
    body: str = ""
    subject: str = ""
    at: str = ""
    url: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    #: Set by the store on read. Lower is a better match.
    rank: float = 0.0

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(
                f"unknown memory kind {self.kind!r}; valid kinds: {', '.join(KINDS)}"
            )
        if not self.title.strip():
            raise ValueError("a memory entry needs a title")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "subject": self.subject,
            "at": self.at,
            "url": self.url,
            "metadata": dict(self.metadata),
        }

    def describe(self) -> str:
        """One line, as the operator hears it."""
        when = self.at[:10] if self.at else ""
        head = f"{when} — " if when else ""
        return f"{head}{self.title}"


class ProductMemory:
    """The product-development record, and the search over it."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)
        self._fts = self._try_fts()

    def _try_fts(self) -> bool:
        """Build the full-text index, or note that this SQLite cannot.

        Degrading to ``LIKE`` matching rather than failing is deliberate: a
        slower answer is a far better outcome than an assistant that has lost
        its memory because a build of SQLite was compiled without an extension.
        """
        try:
            with self._conn:
                self._conn.executescript(_FTS_SCHEMA)
        except sqlite3.OperationalError as exc:
            logger.warning(
                "this SQLite has no FTS5, so search will be slower and simpler: %s",
                exc,
            )
            return False
        return True

    @property
    def path(self) -> Path:
        return self._path

    @property
    def full_text_search(self) -> bool:
        """Whether ranked search is available, or only substring matching."""
        return self._fts

    # -- writing -----------------------------------------------------------

    def remember(self, entry: MemoryEntry) -> None:
        """Record *entry*, replacing any earlier version of the same thing.

        Identity is ``(kind, subject, title)``. A feature that is remembered
        again as it progresses updates its own entry rather than accumulating
        one row per state change — the memory is meant to answer "what did we
        build", not to be a second copy of the audit log, which already exists
        and is hash-chained.
        """
        import json

        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO memory (kind, subject, title, body, at, url, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(kind, subject, title) DO UPDATE SET "
                "body=excluded.body, at=excluded.at, url=excluded.url, "
                "metadata=excluded.metadata",
                (
                    entry.kind,
                    entry.subject,
                    entry.title,
                    entry.body,
                    entry.at,
                    entry.url,
                    json.dumps(entry.metadata, sort_keys=True),
                ),
            )

    def remember_feature(self, feature: Any) -> None:
        """Record what a feature request was, and what became of it.

        Note what is not recorded: the plan, the attempts, the agent's account
        of its own work. Those live on the feature and in the journal. What
        belongs in memory is the sentence somebody would want in six months.
        """
        verification = (getattr(feature, "metadata", {}) or {}).get(
            "verification"
        ) or {}
        state = getattr(feature.state, "value", feature.state)
        body_lines = [
            f"Asked for: {feature.operator_request}",
            f"Outcome: {state}",
            f"Risk: {feature.risk}",
        ]
        if verification.get("summary"):
            body_lines.append(f"Verified: {verification['summary']}")
        if feature.pr_url:
            body_lines.append(f"Pull request: {feature.pr_url}")
        if feature.preview_url:
            body_lines.append(f"Preview: {feature.preview_url}")
        changed = feature.attempts[-1].changed_files if feature.attempts else []
        if changed:
            body_lines.append("Touched: " + ", ".join(changed[:12]))

        self.remember(
            MemoryEntry(
                kind="feature",
                subject=feature.id,
                title=feature.title or feature.operator_request[:120],
                body="\n".join(body_lines),
                at=feature.updated_at or feature.created_at,
                url=feature.pr_url or feature.preview_url,
                metadata={
                    "state": state,
                    "risk": feature.risk,
                    "source": feature.source,
                },
            )
        )

    def record_decision(
        self, *, subject: str, title: str, because: str, at: str = "", url: str = ""
    ) -> None:
        """Record a product decision and the reason behind it.

        The reason is the whole point. "We changed onboarding" is in the git
        log; "we changed onboarding because three people in a row could not
        find the club code" is not, and it is the only one of the two that helps
        anybody decide what to do next.
        """
        self.remember(
            MemoryEntry(
                kind="decision",
                subject=subject,
                title=title,
                body=f"Because: {because}",
                at=at,
                url=url,
            )
        )

    # -- reading -----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        kinds: Optional[Sequence[str]] = None,
        limit: int = 10,
    ) -> List[MemoryEntry]:
        """Find entries matching *query*, best first."""
        cleaned = _FTS_SYNTAX.sub(" ", (query or "")).strip()
        if not cleaned:
            return self.recent(kinds=kinds, limit=limit)
        if self._fts:
            found = self._search_fts(cleaned, kinds, limit)
            if found:
                return found
            # An FTS query that matches nothing is often a query whose terms are
            # all stopwords or all punctuation. Falling through to substring
            # matching costs one extra query and answers questions that would
            # otherwise return silence.
        return self._search_like(cleaned, kinds, limit)

    def _search_fts(
        self, query: str, kinds: Optional[Sequence[str]], limit: int
    ) -> List[MemoryEntry]:
        # Every term ORed rather than ANDed: an operator asking "why did we
        # change onboarding" should not get nothing because "why" appears in no
        # entry. BM25 puts the entries matching more terms first anyway.
        terms = [t for t in query.split() if len(t) > 1]
        if not terms:
            return []
        match = " OR ".join(terms)
        sql = (
            "SELECT m.*, bm25(memory_fts) AS rank FROM memory_fts "
            "JOIN memory m ON m.id = memory_fts.rowid "
            "WHERE memory_fts MATCH ?"
        )
        params: List[Any] = [match]
        if kinds:
            sql += f" AND m.kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        sql += " ORDER BY rank LIMIT ?"
        params.append(int(limit))
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("full-text search failed, falling back: %s", exc)
            return []
        return [self._hydrate(row) for row in rows]

    def _search_like(
        self, query: str, kinds: Optional[Sequence[str]], limit: int
    ) -> List[MemoryEntry]:
        terms = [t for t in query.split() if len(t) > 2][:6]
        if not terms:
            return []
        clauses = " OR ".join(
            ["(title LIKE ? OR body LIKE ? OR subject LIKE ?)"] * len(terms)
        )
        params: List[Any] = []
        for term in terms:
            params.extend([f"%{term}%"] * 3)
        sql = f"SELECT * FROM memory WHERE ({clauses})"
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        sql += " ORDER BY at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._hydrate(row) for row in rows]

    def recent(
        self,
        *,
        kinds: Optional[Sequence[str]] = None,
        limit: int = 10,
        since: str = "",
    ) -> List[MemoryEntry]:
        """The most recent entries, newest first."""
        sql = "SELECT * FROM memory"
        params: List[Any] = []
        conditions: List[str] = []
        if kinds:
            conditions.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if since:
            conditions.append("at >= ?")
            params.append(since)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._hydrate(row) for row in rows]

    def on_day(
        self, day: str, *, kinds: Optional[Sequence[str]] = None
    ) -> List[MemoryEntry]:
        """Everything from one calendar day, ``YYYY-MM-DD``.

        Answers "what did we build yesterday" without the caller having to know
        that timestamps are ISO strings.
        """
        sql = "SELECT * FROM memory WHERE at LIKE ?"
        params: List[Any] = [f"{day}%"]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        sql += " ORDER BY at ASC, id ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._hydrate(row) for row in rows]

    def yesterday(self, *, today: Optional[str] = None) -> List[MemoryEntry]:
        """Everything from the day before *today* (default: now, UTC)."""
        if today:
            reference = datetime.fromisoformat(today[:10]).replace(tzinfo=timezone.utc)
        else:
            reference = datetime.now(timezone.utc)
        return self.on_day((reference - timedelta(days=1)).date().isoformat())

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM memory").fetchone()
        return int(row["n"]) if row else 0

    # -- plumbing ----------------------------------------------------------

    @staticmethod
    def _hydrate(row: sqlite3.Row) -> MemoryEntry:
        import json

        try:
            metadata = json.loads(row["metadata"])
        except (ValueError, TypeError, IndexError):
            metadata = {}
        rank = 0.0
        try:
            rank = float(row["rank"])
        except (IndexError, TypeError, ValueError, KeyError):
            rank = 0.0
        return MemoryEntry(
            kind=row["kind"],
            title=row["title"],
            body=row["body"],
            subject=row["subject"],
            at=row["at"],
            url=row["url"],
            metadata=metadata,
            rank=rank,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def summarise(entries: Iterable[MemoryEntry], *, limit: int = 8) -> str:
    """Render entries as the operator hears them.

    Factual and short. §29 asks for a summary that is factual, and the failure
    to avoid is the one where a daily summary becomes prose nobody reads because
    it says the same three sentences every morning.
    """
    listed = list(entries)[:limit]
    if not listed:
        return "Nothing to report."
    return "\n".join(f"- {entry.describe()}" for entry in listed)
