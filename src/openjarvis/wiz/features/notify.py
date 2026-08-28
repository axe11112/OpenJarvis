"""The owner hears about a feature exactly twice: it shipped, or it needs them.

Mirrors :mod:`openjarvis.reliability.notify`'s restraint for a different
domain, deliberately — not a second notification system, the same one
philosophy applied where it did not reach before. That module's own docstring
says it best: most of what Wiz does is not news. A feature request passes
through nine or ten states, several of them retried a few times each; the
owner does not need a sentence for any of that, only for the two moments that
actually change what is true — the thing they asked for exists and works
where users are, or Wiz has hit something it genuinely cannot resolve alone
and needs a decision.

**One feature, at most one message per outcome, ever.** Deduplicated on disk
so a watcher restart, a retried step, or ``ship`` being called twice for the
same reason does not say it again — the same restraint
:mod:`~openjarvis.reliability.notify_ledger` keeps for incidents, applied to a
different key: not "what does the owner still need to hear", but "have they
already heard this exact thing about this exact feature". A feature does not
flap the way an incident does, so the ledger does not need that module's
correlated-outage machinery — a feature id and a digest of what is being said
about it is enough.

**No model writes the message.** Deterministic copy, assembled from the
feature's own title and the reason its own gates already recorded — the same
principle reliability's notifier holds to, for the same reason: a sentence
sent to someone's phone should be one something checked.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

__all__ = ["FeatureOwnerNotifier", "NEEDS_OWNER_KINDS", "SUCCESS_KIND"]

#: The one journal kind that means "it shipped and production agrees" — see
#: openjarvis.wiz.features.postship.complete: COMPLETE is reserved for
#: exactly this, so this is the only kind that maps to it.
SUCCESS_KIND = "feature.shipped"

#: Every journal kind that represents a genuine "I need you" — the feature's
#: own attempt loop is exhausted, evidence changed too much to proceed
#: safely, or a merge landed in a state nothing here can safely continue
#: from on its own. Everything else — retries, previews, PR creation, merge
#: progress, deployment progress — is a step, not an outcome, and is left out
#: on purpose; see the module docstring.
NEEDS_OWNER_KINDS = frozenset(
    {
        "feature.attempts_exhausted",
        "feature.disk_exhausted",
        "feature.engine_unavailable",
        "feature.failed",
        "feature.plan_failed",
        "feature.no_contract",
        "feature.authority_refused",
        "feature.risk_raised_by_diff",
        "feature.no_preview_provider",
        "feature.push_failed",
        "feature.no_verifier",
        "feature.needs_a_person",
        "feature.needs_approval",
        "feature.merge_already_done",
        "feature.merged_unverified",
        "feature.production_unverified",
    }
)


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


@dataclass
class FeatureOwnerNotifier:
    """Sends at most one message per feature per distinct outcome.

    ``send`` is a plain one-argument callable rather than a specific
    transport class, so this can drive Telegram, a console, or a test double
    without knowing which.
    """

    send: Callable[[str], None]
    ledger_path: Path
    persona: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def notify(self, feature: Any, *, kind: str, reason: str) -> bool:
        """Notify if this (feature, outcome) has not already been said.

        Returns whether a message was actually sent — mainly useful to a
        caller proving a diagnostic message really went out, and to tests.
        """
        if kind == SUCCESS_KIND:
            text = self._success_text(feature)
        elif kind in NEEDS_OWNER_KINDS:
            text = self._needs_owner_text(feature, reason)
        else:
            return False

        key = str(getattr(feature, "id", "") or "")
        if not key:
            return False
        digest = _digest(f"{kind}:{reason}")
        with self._lock:
            entries = self._load()
            previous = entries.get(key)
            if previous is not None and previous.get("digest") == digest:
                return False  # already told them exactly this
            entries[key] = {"kind": kind, "digest": digest}
            self._save(entries)

        try:
            self.send(text)
        except Exception:  # noqa: BLE001 - a failed send must not break shipping
            logger.exception("could not notify the owner about %s", key)
            return False
        return True

    def _success_text(self, feature: Any) -> str:
        title = str(getattr(feature, "title", "") or getattr(feature, "id", ""))
        return self._say(
            f"it's live.\nI finished {title} and verified it in production."
        )

    def _needs_owner_text(self, feature: Any, reason: str) -> str:
        title = str(getattr(feature, "title", "") or getattr(feature, "id", ""))
        return self._say(f"I need your help.\n{title}: {reason}")

    def _say(self, body: str) -> str:
        return f"Sir, {body}" if self.persona else body[:1].upper() + body[1:]

    # -- persistence, survives a restart -------------------------------------

    def _load(self) -> Dict[str, Dict[str, str]]:
        try:
            return json.loads(self.ledger_path.read_text())
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError):
            logger.exception("could not read the feature notification ledger")
            return {}

    def _save(self, entries: Dict[str, Dict[str, str]]) -> None:
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            self.ledger_path.write_text(json.dumps(entries, sort_keys=True))
        except OSError:
            logger.exception("could not persist the feature notification ledger")
