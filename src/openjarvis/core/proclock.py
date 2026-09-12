"""A cross-process, crash-safe mutual-exclusion lease.

Several single-flight guards in this codebase — :meth:`FeaturePipeline.ship`'s
``_ship_lock``, :class:`WizJournal`'s append lock, the repair guard in
``reliability/watch.py`` — are a plain ``threading.Lock``. That is correct
*within* one process and worthless *between* two: two independent OpenJarvis
processes each hold their own lock object, so nothing stops both from
believing they alone are merging a feature, or from appending the same
journal sequence number at once. Wiz is explicitly meant to run as more than
one process at a time — a laptop instance and a cloud instance auditing it in
parallel is exactly this system's own deployment shape — so "only one process
ever runs this" is not a safe assumption to build a merge gate on.

:class:`ProcessLease` closes that gap with an OS-level advisory file lock
(``fcntl.flock``), not a hand-rolled PID/TTL scheme. The two are not
equivalent:

* ``flock`` is released by the kernel the instant the holding process exits,
  crashes, or is killed — for any reason, including SIGKILL. Stale-owner
  recovery is therefore automatic and immediate, not a policy decision this
  module has to get right.
* A manual "steal the lock if the recorded PID looks dead or the TTL has
  expired" scheme has to get that decision right, and getting it wrong in
  the unsafe direction — breaking a lock a slow-but-alive holder still needs —
  is exactly the failure this exists to prevent: two processes proceeding to
  merge at once. A live process that is merely slow (a large diff, a loaded
  CI runner) looks identical to a dead one from the outside; only the kernel
  actually knows which it is.

So this module never breaks a lock it cannot prove is abandoned. ``acquire``
blocks up to a bounded timeout and then fails loudly with the current
holder's recorded identity — auditable, but never destructive.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "LeaseTimeout",
    "LeaseInfo",
    "ProcessLease",
    "PRODUCTION_CHANGE_LOCK",
    "production_change_lease",
]

#: Filename of the single production-change lease, at the config root so that
#: both the Wiz feature pipeline and the reliability repair loop resolve the
#: same file. See :func:`production_change_lease`.
PRODUCTION_CHANGE_LOCK = "production-change.lock"


class LeaseTimeout(TimeoutError):
    """Raised when a lease could not be acquired within its timeout.

    Carries the current holder's recorded identity (if any could be read) so
    the caller can log or surface *who* is holding it, not just that
    acquisition failed.
    """

    def __init__(self, name: str, timeout: float, holder: Optional["LeaseInfo"]):
        self.name = name
        self.timeout = timeout
        self.holder = holder
        detail = f" (held by {holder.describe()})" if holder is not None else ""
        super().__init__(
            f"could not acquire lease {name!r} within {timeout:.1f}s{detail}"
        )


@dataclass(frozen=True, slots=True)
class LeaseInfo:
    """Who holds a lease, recorded for audit — not for arbitrating access.

    Mutual exclusion is enforced entirely by the kernel's ``flock``; this is
    metadata written *after* the lock is held, purely so a human (or a
    timed-out caller) can see who has it and since when.
    """

    owner: str
    pid: int
    host: str
    acquired_at: float
    reason: str = ""

    def describe(self) -> str:
        age = max(0.0, time.time() - self.acquired_at)
        return f"{self.owner} (pid={self.pid} host={self.host}, held {age:.0f}s)"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "owner": self.owner,
            "pid": self.pid,
            "host": self.host,
            "acquired_at": self.acquired_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "LeaseInfo":
        return cls(
            owner=str(raw.get("owner", "")),
            pid=int(raw.get("pid", 0) or 0),
            host=str(raw.get("host", "")),
            acquired_at=float(raw.get("acquired_at", 0.0) or 0.0),
            reason=str(raw.get("reason", "")),
        )


class ProcessLease:
    """A named, cross-process exclusive lease backed by a lockfile.

    One :class:`ProcessLease` per lock *file*; safe to construct a fresh one
    per call, since the identity that matters (``flock``'s kernel-side state)
    lives on the file descriptor, not on this object.
    """

    def __init__(self, path: str | Path, *, owner: str = "") -> None:
        self._path = Path(path)
        self._owner = owner or f"pid-{os.getpid()}"

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def acquire(
        self,
        *,
        timeout: float = 30.0,
        poll_interval: float = 0.2,
        reason: str = "",
    ) -> Iterator[None]:
        """Hold the lease for the ``with`` block, or raise :class:`LeaseTimeout`.

        Non-blocking ``flock`` attempts in a poll loop, rather than a single
        blocking ``flock`` call, so a bounded timeout is possible at all —
        ``flock(LOCK_EX)`` alone has no timeout, and a caller that can wait
        forever is exactly how one stuck merge becomes an outage for every
        feature behind it.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, timeout)
        handle = open(self._path, "a+")
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise LeaseTimeout(
                            str(self._path), timeout, self._read_holder(handle)
                        ) from None
                    time.sleep(poll_interval)

            info = LeaseInfo(
                owner=self._owner,
                pid=os.getpid(),
                host=socket.gethostname(),
                acquired_at=time.time(),
                reason=reason,
            )
            self._write_holder(handle, info)
            try:
                yield
            finally:
                # Truncate before releasing: a reader that acquires the lock
                # a moment later must not see a stale holder record and
                # mistake it for a live one. Order matters — release last.
                handle.seek(0)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _write_holder(self, handle, info: LeaseInfo) -> None:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(info.to_dict(), sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())

    def _read_holder(self, handle) -> Optional[LeaseInfo]:
        """Best-effort read of who currently holds the lease, for diagnostics.

        Never authoritative — the file can be stale for an instant between a
        holder releasing ``flock`` and truncating its record, or simply
        absent if this is the lease's first-ever acquisition. Only used to
        make a :class:`LeaseTimeout` message useful, never to decide
        anything.
        """
        try:
            handle.seek(0)
            raw = handle.read()
            if not raw.strip():
                return None
            return LeaseInfo.from_dict(json.loads(raw))
        except (OSError, ValueError):
            return None

    def current_holder(self) -> Optional[LeaseInfo]:
        """Best-effort snapshot of who holds this lease right now, if anyone.

        For status/dashboard display. Racy by nature (the holder can change
        the instant after this returns) and therefore never used to gate a
        decision — only :meth:`acquire` does that, via the kernel.
        """
        if not self._path.exists():
            return None
        try:
            with open(self._path, "r") as handle:
                return self._read_holder(handle)
        except OSError:
            return None


def production_change_lease(*, owner: str = "", root: Optional[Path] = None) -> "ProcessLease":
    """The one lease every production-changing operation must hold.

    Two subsystems can put a commit on the production default branch:
    :meth:`openjarvis.wiz.features.pipeline.FeaturePipeline.ship` merges a
    feature's pull request, and
    :meth:`openjarvis.reliability.merge.AutoMerger.merge_for` merges a repair's.
    They were serialised against *themselves* and against nothing else — ship
    took a lease named ``ship.lock``, the repair side took a process-local
    ``threading.RLock`` and no lease at all — so a watcher merging a repair and
    a shipper merging a feature raced each other by construction, in the one
    place in this system where a race is least recoverable.

    "Which of two merges won" is not the damage. The damage is that each side
    then *observes production* to decide whether its own change is good: the
    repair loop re-runs the incident's reproduction, and post-ship
    verification checks the deployment. Both of those read one shared
    production, so an interleaved pair can each attribute the other's
    deployment to itself — a feature declared verified on a deploy that
    actually carries a repair, a repair declared to have fixed an incident
    that a feature fixed, or either declared broken by the other's regression.
    A wrong COMPLETE is worse than a refused merge, because nothing downstream
    re-checks it.

    So the lock covers *production change*, not *shipping*: one lease, one
    file, both callers, held across merge **and** the production observation
    that interprets it. It deliberately lives at the config root rather than
    under ``wiz/``, because it is not Wiz's lock — it is the production's.

    Ordering, to keep this deadlock-free: a caller takes this lease **first**
    and any narrower component lease inside it. Nothing may acquire a
    component lease and then reach for this one. ``ProcessLease`` is backed by
    ``flock`` and is therefore *not* reentrant (a second ``acquire`` in the
    same process opens a second file description and blocks against itself
    until the timeout), so a caller must also never nest this lease inside
    itself — see :meth:`ProcessLease.acquire`.
    """
    if root is None:
        from openjarvis.core.paths import get_config_dir

        root = Path(get_config_dir())
    return ProcessLease(Path(root) / PRODUCTION_CHANGE_LOCK, owner=owner or "production-change")
