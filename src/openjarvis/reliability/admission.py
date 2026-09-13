"""Cross-process, restart-surviving admission control for repairs.

:class:`~openjarvis.reliability.watch.RepairGate` decides whether an incident
may start a repair right now. Every piece of state that decision rests on --
which repairs are running, which failures are cooling down -- lived in a
``dict`` on that object, behind a ``threading.Lock``. That is correct for one
process that never restarts, and this watcher is neither.

Two concrete failures came out of that:

* **Two processes, two repairs, two pull requests.** The gate's concurrency
  limit and its "a repair for this same failure is already running" check only
  ever saw the repairs *this* process started. A launchd watcher and a manually
  started ``jarvis reliability watch``, or an old watcher that has not finished
  exiting while its replacement comes up, each admit the same incident and run
  a coding agent on the same repository at the same time. The merge itself is
  serialised by the production-change lease, but by then the damage -- two
  agents, two branches, two pull requests for one root cause -- is done.
* **A restart erases every cooldown.** The pending-pull-request cooldown exists
  because one outage once became six pull requests in six ticks. It was held in
  memory, so restarting the watcher put the system straight back into the state
  that cooldown was written to prevent -- and a watcher mid-outage is exactly
  what an operator restarts.

The fix is to keep both in the filesystem, where another process and a later
process can see them:

* A **claim** is an ``flock`` held for as long as the repair runs, one per key
  the gate arbitrates on (the incident id, and the failure fingerprint when it
  differs). The kernel drops it the instant the holder exits for any reason,
  including SIGKILL, so a crashed repair never leaves a claim that blocks its
  successor forever -- and a *live but slow* repair is never mistaken for a
  dead one, which a PID or TTL scheme cannot promise.
* A **cooldown** is a wall-clock deadline in a small JSON file. Wall clock, not
  ``time.monotonic``: a monotonic deadline is meaningless to the next process,
  and surviving a restart is the entire point.

Both are read and written under one registry lease so that two processes cannot
evaluate "is there capacity?" against the same answer and both act on it.

Fail-closed by construction: if this cannot read the registry -- lease timeout,
unwritable directory, corrupt file -- it refuses admission. A repair that does
not start is a delay; two repairs that start together is the failure this
exists to prevent.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import socket
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["RepairAdmission", "RepairClaim"]

#: The registry lease. Held only while evaluating or mutating admission state --
#: never while a repair runs.
_REGISTRY_LOCK = "admission.lock"
_COOLDOWNS = "cooldowns.json"
_SLOTS = "slots"


def _slot_name(key: str) -> str:
    """A filesystem-safe filename for an arbitrary incident id or fingerprint.

    Hashed rather than sanitised: fingerprints are opaque strings that may
    collide once punctuation is stripped, and two different failures sharing a
    slot file would silently serialise them.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".lock"


@dataclass(frozen=True, slots=True)
class RepairClaim:
    """One process's live claim on a repair, for display and diagnostics."""

    claim_id: str
    incident_id: str
    fingerprint: str
    owner: str
    pid: int
    host: str
    started_at: float

    def describe(self) -> str:
        age = max(0.0, time.time() - self.started_at)
        return (
            f"{self.incident_id} (owner={self.owner} pid={self.pid} "
            f"host={self.host}, running {age:.0f}s)"
        )


@dataclass
class RepairAdmission:
    """The durable, cross-process half of repair admission.

    Not a policy object: it holds no opinion about how long a cooldown should
    be or what counts as a failure. It answers "is this key claimed anywhere
    right now, and is it cooling down", and hands out claims.
    """

    root: Path
    owner: str = ""
    #: Wall clock, deliberately. Deadlines written here are read by other
    #: processes and by this one after a restart; a monotonic reading means
    #: nothing to either.
    clock: Callable[[], float] = time.time
    #: How long to wait for the registry lease. Short: every holder does a few
    #: file operations and releases, so a wait beyond this means something is
    #: wrong, and waiting longer inside a check loop is worse than deferring.
    lease_timeout: float = 5.0
    _handles: Dict[str, List[Any]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.owner = self.owner or f"pid-{os.getpid()}"

    # -- paths ------------------------------------------------------------

    @property
    def _registry_path(self) -> Path:
        return self.root / _REGISTRY_LOCK

    @property
    def _cooldown_path(self) -> Path:
        return self.root / _COOLDOWNS

    @property
    def _slot_dir(self) -> Path:
        return self.root / _SLOTS

    def _keys(self, incident_id: str, fingerprint: str) -> List[str]:
        """The keys a repair claims: its incident, and its failure.

        Both, because they answer different questions. The incident id stops
        the same incident being repaired twice; the fingerprint stops the same
        root cause being repaired twice under two incident ids, which is what
        a recurring failure produces.
        """
        keys = [k for k in (incident_id, fingerprint) if k]
        return list(dict.fromkeys(keys))

    # -- registry ---------------------------------------------------------

    @contextmanager
    def _registry(self) -> Iterator[None]:
        """Hold the registry lease, or raise.

        A plain ``flock`` in a bounded poll loop rather than
        :class:`~openjarvis.core.proclock.ProcessLease`, because this lease
        wants no holder record: it is held for microseconds and read by nobody.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, self.lease_timeout)
        handle = open(self._registry_path, "a+")
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"could not read the repair admission registry at "
                            f"{self.root} within {self.lease_timeout:.1f}s"
                        ) from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    # -- claims -----------------------------------------------------------

    @staticmethod
    def _is_claimed(path: Path) -> bool:
        """Whether some process holds this slot, asked of the kernel.

        Never by reading the record in the file: a process killed with SIGKILL
        leaves its record behind while the kernel drops its lock immediately,
        so a reader trusting the record would treat one crash as a permanent
        block on repairing that failure ever again.
        """
        try:
            handle = open(path, "a+")
        except OSError:
            return False
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return True
                raise
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
        finally:
            handle.close()

    @staticmethod
    def _read_claim(path: Path) -> Optional[RepairClaim]:
        try:
            raw = path.read_text()
        except OSError:
            return None
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        try:
            return RepairClaim(
                claim_id=str(data.get("claim_id", "")),
                incident_id=str(data.get("incident_id", "")),
                fingerprint=str(data.get("fingerprint", "")),
                owner=str(data.get("owner", "")),
                pid=int(data.get("pid", 0) or 0),
                host=str(data.get("host", "")),
                started_at=float(data.get("started_at", 0.0) or 0.0),
            )
        except (TypeError, ValueError):
            return None

    def _live_claims(self) -> List[RepairClaim]:
        """Every claim currently held by any process, this one included."""
        claims: List[RepairClaim] = []
        if not self._slot_dir.is_dir():
            return claims
        for path in sorted(self._slot_dir.glob("*.lock")):
            if not self._is_claimed(path):
                continue
            claim = self._read_claim(path)
            if claim is not None:
                claims.append(claim)
        return claims

    @staticmethod
    def _distinct(claims: List[RepairClaim]) -> int:
        """Repairs running, not slots held.

        One repair takes a slot per key it claims, so counting files would
        report a fingerprinted repair as two and halve the concurrency limit.
        """
        return len({c.claim_id or c.incident_id for c in claims})

    # -- cooldowns --------------------------------------------------------

    def _read_cooldowns(self) -> Dict[str, Tuple[float, str]]:
        try:
            raw = self._cooldown_path.read_text()
        except FileNotFoundError:
            return {}
        except OSError:
            raise
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            # A truncated write from a killed process. Treated as "no
            # cooldowns recorded" rather than as a hard failure: the file is
            # rewritten whole on the next release, and the in-process cooldown
            # still applies for as long as this process lives.
            logger.warning(
                "repair cooldown file at %s is unreadable", self._cooldown_path
            )
            return {}
        out: Dict[str, Tuple[float, str]] = {}
        if not isinstance(data, dict):
            return out
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            try:
                out[str(key)] = (
                    float(value.get("until", 0.0) or 0.0),
                    str(value.get("why", "")),
                )
            except (TypeError, ValueError):
                continue
        return out

    def _write_cooldowns(self, cooldowns: Dict[str, Tuple[float, str]]) -> None:
        now = self.clock()
        payload = {
            key: {"until": until, "why": why}
            for key, (until, why) in sorted(cooldowns.items())
            if until > now
        }
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._cooldown_path.with_suffix(".json.tmp")
        with open(tmp, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._cooldown_path)

    # -- the questions the gate asks --------------------------------------

    def why_not(
        self, incident_id: str, *, fingerprint: str = "", max_concurrent: int = 1
    ) -> str:
        """Why this repair may not start anywhere on this machine, or ``""``.

        Fails closed: an unreadable registry answers "not now", never "go
        ahead". The cost of being wrong in the other direction is two coding
        agents in one repository.
        """
        try:
            with self._registry():
                return self._why_not_locked(
                    incident_id, fingerprint=fingerprint, max_concurrent=max_concurrent
                )
        except Exception as exc:  # noqa: BLE001 - an unreadable registry is a refusal
            logger.warning("refusing repair admission: %s", exc)
            return f"the repair admission registry could not be read ({exc})"

    def _why_not_locked(
        self, incident_id: str, *, fingerprint: str, max_concurrent: int
    ) -> str:
        keys = self._keys(incident_id, fingerprint)
        claims = self._live_claims()
        for claim in claims:
            for key in keys:
                if key and key in (claim.incident_id, claim.fingerprint):
                    if key == incident_id and claim.incident_id == incident_id:
                        return (
                            "a repair for this incident is already running in "
                            f"another process: {claim.describe()}"
                        )
                    return (
                        "a repair for this same failure is already running in "
                        f"another process: {claim.describe()}"
                    )
        running = self._distinct(claims)
        if running >= max(1, max_concurrent):
            who = ", ".join(sorted(c.describe() for c in claims))
            return (
                f"the machine-wide concurrency limit of {max_concurrent} is "
                f"reached (running: {who})"
            )
        now = self.clock()
        cooldowns = self._read_cooldowns()
        for key in keys:
            until, why = cooldowns.get(key, (0.0, ""))
            if now < until:
                return f"{why} for another {until - now:.0f}s"
        return ""

    def claim(
        self, incident_id: str, *, fingerprint: str = "", max_concurrent: int = 1
    ) -> bool:
        """Take the claim, or return ``False``.

        Evaluation and acquisition happen under one registry lease, so two
        processes cannot both read "there is capacity" and both act on it.
        """
        try:
            with self._registry():
                if self._why_not_locked(
                    incident_id, fingerprint=fingerprint, max_concurrent=max_concurrent
                ):
                    return False
                return self._take_locked(incident_id, fingerprint=fingerprint)
        except Exception as exc:  # noqa: BLE001 - an unclaimable slot is a refusal
            logger.warning(
                "could not claim repair admission for %s: %s", incident_id, exc
            )
            self.release(incident_id)
            return False

    def _take_locked(self, incident_id: str, *, fingerprint: str) -> bool:
        claim_id = uuid.uuid4().hex
        record = json.dumps(
            {
                "claim_id": claim_id,
                "incident_id": incident_id,
                "fingerprint": fingerprint,
                "owner": self.owner,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": self.clock(),
            },
            sort_keys=True,
        )
        self._slot_dir.mkdir(parents=True, exist_ok=True)
        handles: List[Any] = []
        for key in self._keys(incident_id, fingerprint):
            path = self._slot_dir / _slot_name(key)
            handle = open(path, "a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                # Partial acquisition is never left behind: some other process
                # took one of our keys between the check and here, so give back
                # everything we did take.
                for taken in handles:
                    self._drop(taken)
                return False
            handle.seek(0)
            handle.truncate()
            handle.write(record)
            handle.flush()
            os.fsync(handle.fileno())
            handles.append(handle)
        self._handles.setdefault(incident_id, []).extend(handles)
        return True

    @staticmethod
    def _drop(handle: Any) -> None:
        """Release one slot, leaving no record a later reader could mistake."""
        try:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                handle.close()
            except OSError:
                pass

    def release(
        self,
        incident_id: str,
        *,
        fingerprint: str = "",
        cooldown_seconds: float = 0.0,
        reason: str = "",
    ) -> None:
        """Give back the claim and, if asked, record a durable cooldown.

        Never fails closed: releasing is not a decision, and refusing to let go
        of a slot because a cooldown could not be written would strand the next
        repair behind a dead one.
        """
        for handle in self._handles.pop(incident_id, []):
            self._drop(handle)
        if cooldown_seconds <= 0:
            return
        try:
            with self._registry():
                cooldowns = self._read_cooldowns()
                until = self.clock() + cooldown_seconds
                for key in self._keys(incident_id, fingerprint):
                    cooldowns[key] = (until, reason)
                self._write_cooldowns(cooldowns)
        except Exception:  # noqa: BLE001 - a lost cooldown must not block release
            logger.exception(
                "could not record a durable repair cooldown for %s; it will only "
                "apply for the life of this process",
                incident_id,
            )

    def clear_cooldown(self, *keys: str) -> List[str]:
        """Drop durable cooldowns on specific incidents or fingerprints."""
        wanted = [k for k in keys if k]
        if not wanted:
            return []
        cleared: List[str] = []
        try:
            with self._registry():
                cooldowns = self._read_cooldowns()
                for key in wanted:
                    if cooldowns.pop(key, None) is not None:
                        cleared.append(key)
                if cleared:
                    self._write_cooldowns(cooldowns)
        except Exception:  # noqa: BLE001 - reported as "nothing cleared"
            logger.exception("could not clear durable repair cooldowns")
            return []
        return cleared

    # -- display ----------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Machine-wide admission state, for the dashboard and the CLI."""
        try:
            with self._registry():
                claims = self._live_claims()
                now = self.clock()
                cooldowns = self._read_cooldowns()
                return {
                    "root": str(self.root),
                    "active": sorted(c.describe() for c in claims),
                    "running": self._distinct(claims),
                    "cooling_down": {
                        key: round(until - now, 1)
                        for key, (until, _why) in cooldowns.items()
                        if until > now
                    },
                }
        except Exception as exc:  # noqa: BLE001 - a snapshot never raises
            return {"root": str(self.root), "error": str(exc)}
