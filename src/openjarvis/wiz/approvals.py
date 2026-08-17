"""Approvals that mean one specific thing, once.

An approval is the operator saying yes. The failure mode this module exists to
prevent is that yes being reused: yes to merging *this* commit becoming yes to
merging whatever is on the branch an hour later, or yes to a plan becoming yes
to the different plan that replaced it.

So an approval binds to a fingerprint of the exact thing shown to the operator —
the capability, the feature, the plan, the commit SHA. Change any of them and the
fingerprint changes and the approval no longer matches. It is not that Wiz
declines to reuse it; there is nothing to reuse, because the token names
something that no longer exists.

Four properties, all tested:

* **Bound.** Redeeming requires reproducing the fingerprint.
* **Single-use.** Redeeming consumes it.
* **Expiring.** An approval from yesterday is not consent today.
* **Recorded.** Issue and redemption both go to the journal.

Time is injected rather than read from the clock, so the expiry rules are
testable without sleeping and a test cannot pass by accident of timing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "Approval",
    "ApprovalError",
    "ApprovalStore",
    "DEFAULT_TTL_SECONDS",
    "fingerprint",
]

#: How long an approval stays good for. Fifteen minutes is long enough to read
#: a plan and short enough that an approval cannot be left lying around while
#: the thing it described changes underneath it.
DEFAULT_TTL_SECONDS = 900.0


def fingerprint(
    *,
    capability: str,
    subject: str = "",
    parameters: Optional[Dict[str, Any]] = None,
) -> str:
    """A stable digest of exactly what is being approved.

    *subject* is whatever identifies the target: a feature id, a commit SHA, a
    plan hash. *parameters* are the specifics the operator was shown. Anything
    that would change what actually happens must appear in one of them, or the
    approval will survive a change it should not have.
    """
    payload = json.dumps(
        {
            "capability": capability,
            "subject": subject,
            "parameters": parameters or {},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class ApprovalError(RuntimeError):
    """An approval could not be redeemed. The message says why."""


@dataclass(frozen=True, slots=True)
class Approval:
    """One operator yes."""

    token: str
    fingerprint: str
    capability: str
    subject: str
    issued_at: float
    expires_at: float
    actor_id: str = ""
    channel: str = ""

    #: What the operator was shown, kept so the audit trail can answer "what did
    #: they actually agree to?" rather than only "did they agree?".
    summary: str = ""

    redeemed_at: Optional[float] = None

    @property
    def redeemed(self) -> bool:
        return self.redeemed_at is not None

    def expired(self, now: float) -> bool:
        return now >= self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token": self.token,
            "fingerprint": self.fingerprint,
            "capability": self.capability,
            "subject": self.subject,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "actor_id": self.actor_id,
            "channel": self.channel,
            "summary": self.summary,
            "redeemed_at": self.redeemed_at,
        }


class ApprovalStore:
    """Issues and redeems approvals.

    In memory on purpose. An approval that does not survive a restart is a
    small inconvenience — the operator is asked again. An approval that *does*
    survive a restart is consent granted to a process the operator has not seen
    since, which is the more expensive mistake.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        journal: Any = None,
        timestamp: Optional[Callable[[], str]] = None,
    ) -> None:
        self._clock = clock
        self._ttl = float(ttl_seconds)
        self._journal = journal
        self._timestamp = timestamp or (lambda: "")
        self._lock = threading.Lock()
        self._approvals: Dict[str, Approval] = {}

    # -- issuing -----------------------------------------------------------

    def issue(
        self,
        *,
        capability: str,
        subject: str = "",
        parameters: Optional[Dict[str, Any]] = None,
        actor_id: str = "",
        channel: str = "",
        summary: str = "",
    ) -> Approval:
        """Record an operator's yes and return the token that redeems it."""
        now = self._clock()
        approval = Approval(
            token=secrets.token_urlsafe(24),
            fingerprint=fingerprint(
                capability=capability, subject=subject, parameters=parameters
            ),
            capability=capability,
            subject=subject,
            issued_at=now,
            expires_at=now + self._ttl,
            actor_id=actor_id,
            channel=channel,
            summary=summary,
        )
        with self._lock:
            self._approvals[approval.token] = approval
        self._record(
            "approval.issued",
            approval,
            reason=summary or f"approved {capability}",
        )
        return approval

    # -- redeeming ---------------------------------------------------------

    def redeem(
        self,
        token: str,
        *,
        capability: str,
        subject: str = "",
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Approval:
        """Consume *token* for exactly this action, or raise.

        The caller passes what it is *about to do*, not what it was approved
        for. That is the whole mechanism: the two are compared, and a mismatch
        means the action drifted from the one the operator saw.
        """
        expected = fingerprint(
            capability=capability, subject=subject, parameters=parameters
        )
        now = self._clock()

        with self._lock:
            approval = self._approvals.get(token)
            if approval is None:
                raise ApprovalError("that approval does not exist")
            if approval.redeemed:
                raise ApprovalError("that approval has already been used")
            if approval.expired(now):
                raise ApprovalError(
                    "that approval has expired; please approve it again"
                )
            if not secrets.compare_digest(approval.fingerprint, expected):
                raise ApprovalError(
                    "this is not the action that was approved — "
                    "something about it has changed since you agreed to it"
                )

            consumed = Approval(
                token=approval.token,
                fingerprint=approval.fingerprint,
                capability=approval.capability,
                subject=approval.subject,
                issued_at=approval.issued_at,
                expires_at=approval.expires_at,
                actor_id=approval.actor_id,
                channel=approval.channel,
                summary=approval.summary,
                redeemed_at=now,
            )
            self._approvals[token] = consumed

        self._record("approval.redeemed", consumed, reason="approval used")
        return consumed

    # -- inspection --------------------------------------------------------

    def get(self, token: str) -> Optional[Approval]:
        with self._lock:
            return self._approvals.get(token)

    def pending(self) -> list:
        """Approvals still waiting to be used and not yet expired."""
        now = self._clock()
        with self._lock:
            return [
                a
                for a in self._approvals.values()
                if not a.redeemed and not a.expired(now)
            ]

    def purge_expired(self) -> int:
        """Drop expired approvals. Returns how many went."""
        now = self._clock()
        with self._lock:
            stale = [t for t, a in self._approvals.items() if a.expired(now)]
            for token in stale:
                self._approvals.pop(token, None)
        return len(stale)

    # -- audit -------------------------------------------------------------

    def _record(self, kind: str, approval: Approval, *, reason: str) -> None:
        if self._journal is None:
            return
        try:
            self._journal.record(
                at=self._timestamp(),
                kind=kind,
                capability=approval.capability,
                actor_id=approval.actor_id,
                channel=approval.channel,
                reason=reason,
                detail={
                    # The token is deliberately absent: it is a bearer
                    # credential, and an audit log is a file that gets read.
                    "fingerprint": approval.fingerprint,
                    "subject": approval.subject,
                },
            )
        except Exception:
            logger.exception("could not journal an approval event")
