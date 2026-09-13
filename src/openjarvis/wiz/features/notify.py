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

**One feature, at most one message per outcome in a row.** Deduplicated on
disk so a watcher restart, a retried step, or ``ship`` being called twice for
the same reason does not say it again — the same restraint
:mod:`~openjarvis.reliability.notify_ledger` keeps for incidents, applied to a
different key: not "what does the owner still need to hear", but "have they
already heard this exact thing about this exact feature". A feature does not
flap the way an incident does, so the ledger does not need that module's
correlated-outage machinery — a feature id and a digest of what is being said
about it is enough.

"In a row" is exact, and deliberate. The ledger remembers a feature's *most
recent* outcome, not every outcome it ever had, so a feature that needs the
owner, is fixed, ships, and later needs them again for the identical reason
says so again. Remembering the whole history would suppress that second
message, and silence about a feature that needs a person is the one failure
this module must not have. Duplicates are recoverable; an unsent "I need you"
is not.

**Delivery is at-least-once, and that is a choice, not a limitation.** The
ledger is written only after ``send`` returns, so a crash, a full disk, or a
lost lease between the two costs one repeated message on the next attempt.
The alternative ordering — record first, send second — converts a single
transient Telegram failure into permanent silence about that outcome, because
every later attempt reads its own record and concludes the owner was already
told. Nothing here can make an external send exactly-once: the message is
either sent before it is recorded or recorded before it is sent, and only one
of those two failure modes is safe. This module has no delivery receipt to
close that gap with, and does not pretend otherwise.

**Process-safe, because more than one process sends these.** The read, the
send and the write happen under a cross-process lease
(:class:`~openjarvis.core.proclock.ProcessLease`), not just a
``threading.Lock``: a watcher and a ``jarvis wiz ship`` run in two processes,
each holding its own lock object and its own copy of the ledger, and the
read-modify-write between them both double-sends *and* discards whichever
feature's record lost the race — so the discarded feature is told everything
again as well. The write itself is atomic
(:func:`~openjarvis.reliability.statefile.write_json_atomic`), since the
restarts this ledger exists to survive are exactly the events that can
interrupt a ``write_text`` mid-truncate.

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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict

from openjarvis.core.proclock import LeaseTimeout, ProcessLease
from openjarvis.reliability.statefile import write_json_atomic

logger = logging.getLogger(__name__)

__all__ = ["FeatureOwnerNotifier", "NEEDS_OWNER_KINDS", "SUCCESS_KIND", "SUCCESS_KINDS"]

#: The ordinary journal kind that means "it shipped and production agrees" —
#: see openjarvis.wiz.features.postship.complete: COMPLETE is reserved for
#: exactly this. Kept as the single name existing callers already use.
SUCCESS_KIND = "feature.shipped"

#: Every kind that is a real "it's live" outcome — not only the ordinary one.
#: Found on FEAT-00030: reconcile_external_merge() deliberately journals
#: "feature.external_bypass_reconciled" rather than "feature.shipped" so the
#: audit trail never implies canonical ship() performed a merge it did not —
#: but that meant notify() below, which only ever recognised the one literal
#: SUCCESS_KIND, silently never told the owner the feature had actually
#: finished. The audit trail's honesty and the owner's "it's live" message
#: are two different concerns; this is what lets both be true at once.
SUCCESS_KINDS = frozenset({SUCCESS_KIND, "feature.external_bypass_reconciled"})

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
    #: How long to wait for the cross-process lease before sending anyway.
    #: Long enough to outlast another process's send, short enough that a
    #: stuck holder does not stall the shipping pipeline behind it.
    lease_timeout: float = 30.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def _lease(self) -> ProcessLease:
        """The machine-wide lease over this ledger file.

        Named after the ledger rather than after this object, because that is
        what two processes have in common. Constructed per call: ``flock``
        state lives on the file descriptor a single ``acquire`` opens, not on
        the Python object, so there is nothing here worth keeping.
        """
        return ProcessLease(
            Path(str(self.ledger_path) + ".lock"), owner="feature-notify"
        )

    def notify(self, feature: Any, *, kind: str, reason: str) -> bool:
        """Notify if this (feature, outcome) has not already been said.

        Returns whether a message was actually sent — mainly useful to a
        caller proving a diagnostic message really went out, and to tests.
        """
        if kind in SUCCESS_KINDS:
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
            try:
                with self._lease.acquire(
                    timeout=self.lease_timeout, reason=f"notify {key}"
                ):
                    return self._notify_locked(key, kind=kind, digest=digest, text=text)
            except LeaseTimeout:
                # Fail *open*, and only here. Everywhere else in this system a
                # guard that cannot answer refuses; this one sends anyway,
                # because the two directions do not cost the same thing. A
                # refusal here is silence about a feature that needs a person,
                # and nothing downstream ever retries a notification that was
                # never attempted. Proceeding costs at most a duplicate
                # message, which this module's contract already allows.
                logger.warning(
                    "could not take the feature notification lease for %s; "
                    "sending without it, which may duplicate a message",
                    key,
                )
                return self._notify_locked(key, kind=kind, digest=digest, text=text)
            except OSError:
                logger.exception(
                    "could not open the feature notification lease for %s; "
                    "sending without it",
                    key,
                )
                return self._notify_locked(key, kind=kind, digest=digest, text=text)

    def _notify_locked(self, key: str, *, kind: str, digest: str, text: str) -> bool:
        """Decide, send, and record — all three inside the lease.

        The ledger is re-read here rather than passed in: the decision must
        rest on what is on disk *now*, under the lease, not on what this
        process last saw. Reading before acquiring is how two processes both
        conclude the owner has not been told.
        """
        entries = self._load()
        previous = entries.get(key)
        if previous is not None and previous.get("digest") == digest:
            return False  # already told them exactly this

        # The ledger is only written *after* send() succeeds. Recording
        # first and sending second would mean a single transient send
        # failure (Telegram down, a network blip) permanently suppresses
        # that outcome — the digest is already on disk, so every retry
        # sees "already told them" and never tries again. For a COMPLETE
        # or HUMAN_REQUIRED message, silently losing it forever is far
        # worse than the alternative failure mode this ordering accepts:
        # send() succeeding but the ledger write failing (a crash between
        # the two, or a full disk) can cause one duplicate resend on the
        # next attempt. See the module docstring: at-least-once, on purpose.
        try:
            self.send(text)
        except Exception:  # noqa: BLE001 - a failed send must not break shipping
            logger.exception("could not notify the owner about %s", key)
            return False

        # Re-read before writing, for the same reason the decision re-read:
        # the file may have gained another feature's record since, and this
        # write replaces the whole object. Losing that record would tell its
        # owner everything about that feature again.
        entries = self._load()
        entries[key] = {"kind": kind, "digest": digest, "at": time.time()}
        self._save(entries)
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

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            loaded = json.loads(self.ledger_path.read_text())
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            # Unreachable through this module's own writes now that they are
            # atomic, so a corrupt file means something else wrote here — or
            # a pre-atomic write was interrupted. Keep it: the cost is one
            # repeated message per feature, and the file is the only evidence
            # of what went wrong.
            self._quarantine()
            return {}
        except OSError:
            logger.exception("could not read the feature notification ledger")
            return {}
        if not isinstance(loaded, dict):
            self._quarantine()
            return {}
        return {
            str(key): value for key, value in loaded.items() if isinstance(value, dict)
        }

    def _quarantine(self) -> None:
        """Move an unreadable ledger aside instead of silently overwriting it."""
        spoiled = Path(str(self.ledger_path) + ".corrupt")
        try:
            self.ledger_path.replace(spoiled)
        except OSError:
            logger.exception("could not set aside the corrupt notification ledger")
            return
        logger.error(
            "the feature notification ledger was unreadable and has been kept at "
            "%s; the owner may hear one repeated message per feature",
            spoiled,
        )

    def _save(self, entries: Dict[str, Dict[str, Any]]) -> None:
        # Atomic: the restarts this ledger exists to survive — a sleeping
        # laptop, a launchd reload, a code update — are exactly the events
        # that can land between a truncate and a write. A half-written ledger
        # reads back as no ledger, and no ledger means the owner is told
        # everything they already know, all over again.
        if not write_json_atomic(self.ledger_path, entries):
            logger.error("could not persist the feature notification ledger")
