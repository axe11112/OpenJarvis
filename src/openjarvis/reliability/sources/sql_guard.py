"""SQL write guard — the last thing standing between JARVIS and your database.

Two separate ideas, deliberately not merged:

* **Never allowed.** Statements that are never a legitimate autonomous repair:
  dropping things, disabling row-level security, granting privileges, touching
  the auth schema, reading vault secrets. These are refused *regardless of
  configuration* — there is no flag that permits them.
* **Gated.** Ordinary writes (``INSERT``/``UPDATE``/``DELETE … WHERE``) that
  might legitimately be part of a fix. These require every gate in
  ``docs/JARVIS_SECURITY.md`` §5 to be open.

The parser is intentionally crude and intentionally paranoid: it does not try to
understand SQL, it looks for dangerous shapes and refuses anything it cannot
confidently classify. A false refusal costs a human five minutes; a false
permit can cost a production database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

__all__ = [
    "SqlVerdict",
    "WriteGateClosedError",
    "check_sql",
    "is_read_only",
]


class WriteGateClosedError(RuntimeError):
    """Raised when a write is attempted with the gate closed."""


@dataclass(slots=True)
class SqlVerdict:
    """Result of inspecting a statement."""

    allowed: bool
    read_only: bool
    reason: str = ""
    matched_rule: str = ""

    def __bool__(self) -> bool:
        return self.allowed


#: Patterns that are refused whatever the configuration says.
#: (regex, rule name, human-readable reason)
#
# Order matters: the most specific rule must come first so the refusal reason
# is the most informative one.  "DROP POLICY" is a security-model change, not
# merely a DROP, and telling the human that is the whole point.
_NEVER_ALLOWED = [
    (
        r"\balter\s+table\b.*\bdisable\s+row\s+level\s+security\b",
        "disable_rls",
        "disabling row-level security weakens the app's security model",
    ),
    (
        r"\bdrop\s+policy\b",
        "drop_policy",
        "dropping an RLS policy weakens the app's security model",
    ),
    (
        r"\bdrop\s+(table|schema|database|view|function|type|index|trigger)\b",
        "drop",
        "DROP is never a legitimate autonomous repair",
    ),
    (r"\btruncate\b", "truncate", "TRUNCATE destroys data irreversibly"),
    (
        r"\bcreate\s+policy\b|\balter\s+policy\b",
        "modify_policy",
        "RLS policy changes need human review",
    ),
    (
        r"\b(grant|revoke)\b",
        "grant",
        "privilege changes need human review",
    ),
    (
        r"\b(create|alter|drop)\s+role\b",
        "role",
        "role changes need human review",
    ),
    (
        r"\bauth\.\w+",
        "auth_schema",
        "the auth schema is off-limits to automated repair",
    ),
    (
        r"\bvault\.|decrypted_secrets\b",
        "vault",
        "reading stored secrets is never permitted",
    ),
    (
        r"\bpg_read_(server_)?file\b|\bcopy\b.*\bfrom\s+program\b|\bpg_sleep\b",
        "dangerous_function",
        "the statement calls a dangerous server-side function",
    ),
    (
        r"\bset\s+role\b|\bsecurity\s+definer\b",
        "privilege_escalation",
        "the statement attempts privilege escalation",
    ),
]

#: Statements that only read.
_READ_PREFIXES = ("select", "with", "explain", "show", "table")

#: Write verbs that are gated rather than forbidden.
_GATED_VERBS = ("insert", "update", "delete", "alter", "create", "merge", "upsert")

_COMMENT_RE = re.compile(r"(--[^\n]*)|(/\*.*?\*/)", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(sql: str) -> str:
    """Strip comments and collapse whitespace for pattern matching.

    Comments are removed first: ``DROP/**/TABLE users`` and
    ``DROP --x\\nTABLE users`` must not slip past a naive word-boundary regex.
    """
    without_comments = _COMMENT_RE.sub(" ", sql)
    return _WHITESPACE_RE.sub(" ", without_comments).strip().lower()


def _split_statements(sql: str) -> List[str]:
    """Split on semicolons.

    Stacked statements are the classic way to smuggle a destructive verb behind
    a harmless-looking one, so every fragment is checked independently.
    """
    return [part.strip() for part in sql.split(";") if part.strip()]


def is_read_only(sql: str) -> bool:
    """Return ``True`` when every statement in *sql* only reads."""
    normalized = _normalize(sql)
    if not normalized:
        return True
    for statement in _split_statements(normalized):
        if not statement.startswith(_READ_PREFIXES):
            return False
        # A CTE can still write: WITH x AS (DELETE ... RETURNING *) ...
        if any(re.search(rf"\b{verb}\b", statement) for verb in _GATED_VERBS):
            return False
    return True


def check_sql(sql: str, *, allow_writes: bool = False) -> SqlVerdict:
    """Decide whether *sql* may run.

    Parameters
    ----------
    sql:
        One or more statements.
    allow_writes:
        Whether the caller has satisfied every write gate.  Even when ``True``,
        the never-allowed rules still apply.

    Returns
    -------
    SqlVerdict
        Truthy when the statement may run.
    """
    normalized = _normalize(sql)
    if not normalized:
        return SqlVerdict(allowed=False, read_only=True, reason="empty statement")

    # Never-allowed rules run first and are not overridable.
    for pattern, rule, reason in _NEVER_ALLOWED:
        if re.search(pattern, normalized):
            return SqlVerdict(
                allowed=False,
                read_only=False,
                reason=reason,
                matched_rule=rule,
            )

    statements = _split_statements(normalized)
    read_only = is_read_only(sql)

    if read_only:
        return SqlVerdict(allowed=True, read_only=True)

    # An unqualified DELETE or UPDATE is a data-loss event waiting to happen.
    for statement in statements:
        if re.match(r"^(delete\s+from|update)\b", statement) and not re.search(
            r"\bwhere\b", statement
        ):
            return SqlVerdict(
                allowed=False,
                read_only=False,
                reason="DELETE/UPDATE without a WHERE clause affects every row",
                matched_rule="unqualified_write",
            )

    if not allow_writes:
        return SqlVerdict(
            allowed=False,
            read_only=False,
            reason=(
                "this statement writes, and production writes are disabled "
                "([reliability.supabase] allow_production_writes = false)"
            ),
            matched_rule="write_gate_closed",
        )

    return SqlVerdict(allowed=True, read_only=False)
