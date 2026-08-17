"""Is the site slow, or is the machine watching it slow?

A probe cannot tell the difference on its own. It measures one number — how long
a page took, as seen from here — and that number contains both the site's
latency and the observer's. On a four-core laptop that is also running a
browser, a watcher, a dashboard and whatever the operator is doing, the
observer's contribution is not small.

The evidence for building this is specific. Nine of the first twenty-five
incidents opened on a duration overrun, every one of them CRITICAL, every one of
them resolving itself with no repair attempted. Measured on the same machine, at
load average 27, every active probe still ran six to forty times *inside* its
budget: browser probes p95 4.6-5.5s against 30s, HTTP probes p95 0.14-0.46s
against 5-8s. The overruns were 33-140s. Nothing in the normal distribution
reaches that; those were stalls.

One probe being slow is ambiguous. *Several unrelated probes being slow at the
same time, while all of them still serve correct content*, is not: a fault in
the homepage does not also slow down the sitemap and the API, and a fault that
did would not leave every assertion passing. That coincidence is the
corroborating signal, and it points at the observer.

What this module does with it is narrow and deliberately unglamorous: it lets a
caller suppress *latency-only* detections while the monitor looks unwell, and
say so in the reason. It has no opinion about any other kind of failure. A 500, a
missing form, an unreachable host — those are believed on the first sighting
whatever this thinks, because a site that is down does not become healthy
because the laptop is busy.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)

__all__ = ["MonitorHealth", "MonitorVerdict", "latency_only_failure"]

#: How long a latency observation stays relevant. Long enough that two probes on
#: different schedules can corroborate each other, short enough that a stall
#: three minutes ago does not excuse a real regression now.
DEFAULT_WINDOW_SECONDS = 180.0

#: How many *distinct other* probes must be simultaneously slow-but-correct
#: before the monitor rather than the site is the better explanation. Two,
#: because one is the ambiguous case this exists to resolve.
DEFAULT_CORROBORATION = 2


@dataclass(frozen=True)
class MonitorVerdict:
    """Whether the observer looks trustworthy right now."""

    degraded: bool
    reason: str = ""
    probes: Tuple[str, ...] = ()

    def __bool__(self) -> bool:
        """Truthy means *degraded*, so ``if verdict:`` reads as "something is up"."""
        return self.degraded


@dataclass
class MonitorHealth:
    """Tracks which probes were recently slow-but-correct.

    Deliberately in memory and deliberately small. This is a judgement about the
    last few minutes on this host; persisting it across restarts would mean a
    reboot inherited an excuse it had not earned.
    """

    window_seconds: float = DEFAULT_WINDOW_SECONDS
    corroboration: int = DEFAULT_CORROBORATION
    clock: Callable[[], float] = time.monotonic
    #: probe id -> when it was last seen slow-but-correct
    _slow: Dict[str, float] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- recording ---------------------------------------------------------

    def record(self, probe_id: str, *, latency_only: bool) -> None:
        """Note one probe outcome.

        ``latency_only`` means the probe failed on its duration budget and on
        nothing else — the page was correct, it just arrived late. Anything else,
        including success, clears that probe: one now returning the wrong content
        is not evidence about the clock, and one that has recovered is not
        evidence about anything.
        """
        if not probe_id:
            return
        now = self.clock()
        with self._lock:
            if latency_only:
                self._slow[probe_id] = now
            else:
                self._slow.pop(probe_id, None)
            self._expire_locked(now)

    def reset(self) -> None:
        """Forget everything. For tests, and after a deliberate restart."""
        with self._lock:
            self._slow.clear()

    # -- the question ------------------------------------------------------

    def verdict(self, *, exclude: str = "") -> MonitorVerdict:
        """Whether *other* probes corroborate that this host is the problem.

        ``exclude`` is the probe currently being judged. It is left out on
        purpose: a probe cannot corroborate itself, and letting it would turn one
        slow page into its own excuse.
        """
        now = self.clock()
        with self._lock:
            self._expire_locked(now)
            others = tuple(sorted(p for p in self._slow if p != exclude))
        if len(others) < max(1, self.corroboration):
            return MonitorVerdict(False, "no corroborating slow probes", others)
        return MonitorVerdict(
            True,
            (
                f"{len(others)} other probe(s) served correct content over their "
                f"time budget within the last {int(self.window_seconds)}s "
                f"({', '.join(others)}); the monitoring host is a better "
                "explanation than the site"
            ),
            others,
        )

    def snapshot(self) -> Dict[str, Any]:
        """What the Control Center shows about the observer's own health."""
        verdict = self.verdict()
        with self._lock:
            slow = sorted(self._slow)
        return {
            "degraded": verdict.degraded,
            "reason": verdict.reason,
            "slow_probes": slow,
            "window_seconds": self.window_seconds,
            "corroboration_required": self.corroboration,
        }

    # -- internals ---------------------------------------------------------

    def _expire_locked(self, now: float) -> None:
        cutoff = now - self.window_seconds
        stale: List[str] = [p for p, at in self._slow.items() if at < cutoff]
        for probe in stale:
            self._slow.pop(probe, None)


def latency_only_failure(result: Any) -> bool:
    """Whether *result* is a probe that was correct but late.

    One definition, imported by everything that needs it, so the detector, the
    post-merge verifier and this module cannot drift into disagreeing about what
    "slow" means.
    """
    from openjarvis.reliability.detector import LATENCY_FAILURE_KINDS

    if getattr(result, "success", False):
        return False
    return str(getattr(result, "failure_kind", "") or "") in LATENCY_FAILURE_KINDS
