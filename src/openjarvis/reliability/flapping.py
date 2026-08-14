"""Flapping detection.

A check that alternates — pass, fail, pass, fail — is not the same as a check
that is failing. It usually means something intermittent: a slow cold start, a
rate limit, one bad node behind a load balancer, a race. What it almost never
means is "there is a bug in the application that a coding agent can fix."

Left undetected, flapping is expensive in the worst way. Each failure opens or
reopens an incident, each incident may start a repair, and each repair spends a
Claude session on a problem that was already gone by the time verification ran —
which then reports success, closes the incident, and waits for the next flap.

So JARVIS watches the *shape* of recent results, not just the latest one. A
probe that changes verdict too often inside a sliding window is marked flapping,
and a flapping incident goes to a human rather than to the repair loop.

The confirmation tracker in ``probes/executor.py`` answers a narrower question —
"has this failed N times in a row yet?" — and does not see alternation at all: a
strict pass/fail/pass/fail sequence never reaches two consecutive failures, so
it is invisible there. The two mechanisms are complementary.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["FlappingDetector", "FlappingVerdict"]


@dataclass(slots=True)
class FlappingVerdict:
    """Whether a check is flapping, and the evidence for saying so."""

    flapping: bool
    probe_id: str = ""
    transitions: int = 0
    failures: int = 0
    samples: int = 0
    window: int = 0
    threshold: int = 0
    recent: str = ""

    @property
    def reason(self) -> str:
        """Human-readable explanation, for notifications and the audit log."""
        if not self.flapping:
            return ""
        return (
            f"'{self.probe_id}' changed verdict {self.transitions} time(s) in the "
            f"last {self.samples} check(s) (threshold {self.threshold}); "
            f"recent results: {self.recent}"
        )

    def to_dict(self) -> Dict[str, object]:
        """Serialize for the incident record."""
        return {
            "flapping": self.flapping,
            "probe_id": self.probe_id,
            "transitions": self.transitions,
            "failures": self.failures,
            "samples": self.samples,
            "window": self.window,
            "threshold": self.threshold,
            "recent": self.recent,
        }


@dataclass
class FlappingDetector:
    """Tracks recent pass/fail history per probe and spots alternation.

    Parameters
    ----------
    window:
        How many recent results to remember per probe.
    failure_threshold:
        How many pass→fail transitions inside the window make it flapping.
        Counting *transitions* rather than failures is deliberate: ten
        consecutive failures is an outage, not flapping, and should go to the
        repair loop. Ten alternations is flapping.
    min_samples:
        Never call something flapping before there is enough history to say so.
    """

    window: int = 10
    failure_threshold: int = 3
    min_samples: int = 4
    _history: Dict[str, Deque[bool]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, probe_id: str, *, failed: bool) -> FlappingVerdict:
        """Record one result and return the current verdict."""
        with self._lock:
            history = self._history.get(probe_id)
            if history is None or history.maxlen != max(2, self.window):
                history = deque(history or (), maxlen=max(2, self.window))
                self._history[probe_id] = history
            history.append(bool(failed))
            return self._assess(probe_id, list(history))

    def verdict(self, probe_id: str) -> FlappingVerdict:
        """Current verdict without recording a new result."""
        with self._lock:
            return self._assess(probe_id, list(self._history.get(probe_id, ())))

    def reset(self, probe_id: str) -> None:
        """Forget a probe's history.

        Called once an incident has been escalated, so the same window cannot
        immediately re-escalate a second time on the next sample.
        """
        with self._lock:
            self._history.pop(probe_id, None)

    def reset_all(self) -> None:
        """Forget everything. Used on watcher restart."""
        with self._lock:
            self._history.clear()

    # -- internals --------------------------------------------------------

    def _assess(self, probe_id: str, samples: List[bool]) -> FlappingVerdict:
        transitions = sum(
            1 for a, b in zip(samples, samples[1:]) if a is not b and b is True
        )
        failures = sum(1 for s in samples if s)
        flapping = (
            len(samples) >= max(2, self.min_samples)
            and transitions >= max(1, self.failure_threshold)
            # An unbroken run of failures is an outage, not a flap. Requiring at
            # least one pass in the window keeps a genuine sustained failure on
            # the repair path where it belongs.
            and failures < len(samples)
        )
        return FlappingVerdict(
            flapping=flapping,
            probe_id=probe_id,
            transitions=transitions,
            failures=failures,
            samples=len(samples),
            window=self.window,
            threshold=self.failure_threshold,
            recent="".join("F" if s else "P" for s in samples),
        )

    def snapshot(self) -> Dict[str, str]:
        """Recent history per probe, for the dashboard."""
        with self._lock:
            return {
                probe_id: "".join("F" if s else "P" for s in history)
                for probe_id, history in self._history.items()
            }


def describe(verdict: Optional[FlappingVerdict]) -> str:
    """Render a verdict for an owner-facing message."""
    if verdict is None or not verdict.flapping:
        return ""
    return verdict.reason
