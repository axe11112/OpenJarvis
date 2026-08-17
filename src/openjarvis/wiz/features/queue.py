"""The development queue — and the rule that production always wins.

On a dual-core machine with 8 GB of memory, one Claude Code session with a Node
build under it is roughly what fits. So the queue's real job is not scheduling;
it is refusing. Exactly one code task runs at a time by default, and the answer
to "can this start?" is usually no.

The part that matters for safety is :meth:`DevelopmentQueue.yield_to_production`.
If the site breaks while a feature is building, the feature stops. Not "is
deprioritised for the next admission" — stops, now, releasing the machine to the
thing that keeps the site up. An operator waiting longer for a dashboard is an
inconvenience; an outage continuing because the laptop was busy compiling a
dashboard is a failure of the whole design.

The queue does not kill anything itself. It records that a feature must yield
and exposes it; the pipeline that owns the subprocess does the stopping, because
only it knows how to leave a worktree in a state somebody can recover.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from openjarvis.wiz.features.model import FeatureRequest, Priority

logger = logging.getLogger(__name__)

__all__ = ["DevelopmentQueue", "QueueDecision", "QueuedTask"]

#: Priorities that belong to reliability rather than to product work. A feature
#: request may never claim one: it would let "urgent" wording in a chat message
#: outrank an actual outage.
RELIABILITY_PRIORITIES = frozenset({Priority.P0, Priority.P1})


@dataclass(frozen=True, slots=True)
class QueuedTask:
    """One unit of work waiting for the machine."""

    feature_id: str
    priority: Priority

    #: Monotonic sequence, so equal priorities run in the order they arrived
    #: rather than in whatever order a dict happened to iterate.
    sequence: int

    title: str = ""

    def sort_key(self) -> Tuple[int, int]:
        return (self.priority.rank, self.sequence)


@dataclass(frozen=True, slots=True)
class QueueDecision:
    """Whether something may start, and why not."""

    admitted: bool
    reason: str
    task: Optional[QueuedTask] = None

    def __bool__(self) -> bool:
        return self.admitted


class DevelopmentQueue:
    """Admission control for code-writing work."""

    def __init__(
        self,
        *,
        max_concurrent: int = 1,
        production_busy: Optional[Callable[[], bool]] = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._max_concurrent = max_concurrent
        self._production_busy = production_busy or (lambda: False)
        self._lock = threading.Lock()
        self._waiting: List[QueuedTask] = []
        self._running: Dict[str, QueuedTask] = {}
        self._yielding: set[str] = set()
        self._sequence = 0

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    # -- submission --------------------------------------------------------

    def submit(self, feature: FeatureRequest) -> QueuedTask:
        """Add *feature* to the queue.

        A feature request carrying a reliability priority is demoted rather than
        rejected. Rejecting would lose the request over a field the operator
        probably did not set on purpose; honouring it would let the word
        "urgent" outrank an outage.
        """
        priority = feature.priority
        if priority in RELIABILITY_PRIORITIES:
            logger.warning(
                "feature %s asked for %s, which is reserved for reliability; "
                "queued at P2 instead",
                feature.id,
                priority.value,
            )
            priority = Priority.P2

        with self._lock:
            self._sequence += 1
            task = QueuedTask(
                feature_id=feature.id,
                priority=priority,
                sequence=self._sequence,
                title=feature.title,
            )
            self._waiting.append(task)
            self._waiting.sort(key=QueuedTask.sort_key)
            return task

    # -- admission ---------------------------------------------------------

    def admit_next(self) -> QueueDecision:
        """Start the highest-priority waiting task, if anything may start."""
        with self._lock:
            if self._production_busy():
                return QueueDecision(
                    admitted=False,
                    reason=(
                        "reliability is working on production; "
                        "feature work waits until it is finished"
                    ),
                )
            if len(self._running) >= self._max_concurrent:
                return QueueDecision(
                    admitted=False,
                    reason=(
                        f"{len(self._running)} of {self._max_concurrent} code "
                        "task slots are in use"
                    ),
                )
            if not self._waiting:
                return QueueDecision(admitted=False, reason="nothing is waiting")

            task = self._waiting.pop(0)
            self._running[task.feature_id] = task
            return QueueDecision(
                admitted=True, reason=f"started {task.feature_id}", task=task
            )

    def finish(self, feature_id: str) -> None:
        """Release the slot *feature_id* was holding."""
        with self._lock:
            self._running.pop(feature_id, None)
            self._yielding.discard(feature_id)

    def cancel(self, feature_id: str) -> bool:
        """Remove a waiting task. Running tasks are stopped, not cancelled."""
        with self._lock:
            before = len(self._waiting)
            self._waiting = [t for t in self._waiting if t.feature_id != feature_id]
            return len(self._waiting) != before

    # -- production pre-emption -------------------------------------------

    def yield_to_production(self, reason: str = "") -> List[QueuedTask]:
        """Mark everything running as needing to stop, and report what.

        Called when reliability picks up an incident. The queue does not do the
        stopping — see the module docstring — but from this moment nothing new
        is admitted and every running task is flagged.
        """
        with self._lock:
            yielding = list(self._running.values())
            self._yielding.update(task.feature_id for task in yielding)
        if yielding:
            logger.warning(
                "%d feature task(s) must yield to production%s",
                len(yielding),
                f": {reason}" if reason else "",
            )
        return yielding

    def must_yield(self, feature_id: str) -> bool:
        """Whether a running task has been told to stop."""
        with self._lock:
            return feature_id in self._yielding

    # -- inspection --------------------------------------------------------

    def snapshot(self) -> Dict[str, object]:
        """What the dashboard shows."""
        with self._lock:
            return {
                "max_concurrent": self._max_concurrent,
                "running": [
                    {
                        "feature_id": t.feature_id,
                        "title": t.title,
                        "priority": t.priority.value,
                        "yielding": t.feature_id in self._yielding,
                    }
                    for t in self._running.values()
                ],
                "waiting": [
                    {
                        "feature_id": t.feature_id,
                        "title": t.title,
                        "priority": t.priority.value,
                        "position": index + 1,
                    }
                    for index, t in enumerate(self._waiting)
                ],
                "production_busy": self._production_busy(),
            }

    def waiting(self) -> List[QueuedTask]:
        with self._lock:
            return list(self._waiting)

    def running(self) -> List[QueuedTask]:
        with self._lock:
            return list(self._running.values())
