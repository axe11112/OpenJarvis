"""ProcessLease: the guard shipping (and the journal, soon) actually needs
when more than one OpenJarvis process is running against the same state
directory — which this system explicitly is meant to support.

Every test here proves a property a plain ``threading.Lock`` cannot: that the
exclusion holds *between processes*, that a timed-out caller finds out who is
holding the lease, and that a crashed holder never wedges everyone else.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import time

import pytest

from openjarvis.wiz.proclock import LeaseTimeout, ProcessLease


def test_sequential_acquire_release(tmp_path):
    lease = ProcessLease(tmp_path / "ship.lock", owner="a")
    with lease.acquire(timeout=1.0):
        assert lease.current_holder() is not None
        assert lease.current_holder().owner == "a"
    assert lease.current_holder() is None


def test_reentrant_same_object_after_release(tmp_path):
    """Releasing truly frees the lease — a second acquire from the same
    process, after the first's ``with`` block exits, must not hang."""
    lease = ProcessLease(tmp_path / "ship.lock", owner="a")
    with lease.acquire(timeout=1.0):
        pass
    with lease.acquire(timeout=1.0):
        pass


def test_held_lease_blocks_a_second_acquirer_in_process(tmp_path):
    """Two ``ProcessLease`` objects on the same path are still one lease —
    the mutual exclusion is the file, not the Python object."""
    path = tmp_path / "ship.lock"
    first = ProcessLease(path, owner="first")
    second = ProcessLease(path, owner="second")
    with first.acquire(timeout=1.0):
        with pytest.raises(LeaseTimeout) as excinfo:
            with second.acquire(timeout=0.3, poll_interval=0.05):
                pytest.fail("must not be reachable while 'first' holds the lease")
        assert "first" in str(excinfo.value)


def test_timeout_message_names_the_holder(tmp_path):
    path = tmp_path / "ship.lock"
    holder = ProcessLease(path, owner="feature-pipeline")
    with holder.acquire(timeout=1.0, reason="shipping FEAT-00099"):
        waiter = ProcessLease(path, owner="waiter")
        with pytest.raises(LeaseTimeout) as excinfo:
            with waiter.acquire(timeout=0.2, poll_interval=0.05):
                pytest.fail("unreachable")
        err = excinfo.value
        assert err.holder is not None
        assert err.holder.owner == "feature-pipeline"
        assert err.holder.pid == os.getpid()


def _hold_lease_and_signal(path_str: str, ready_event, hold_seconds: float) -> None:
    lease = ProcessLease(path_str, owner="child")
    with lease.acquire(timeout=5.0):
        ready_event.set()
        time.sleep(hold_seconds)


def test_cross_process_mutual_exclusion(tmp_path):
    """The actual point of this module: a lock a second *process*, not just
    a second thread, cannot bypass while the first holds it."""
    path = tmp_path / "ship.lock"
    ready = multiprocessing.Event()
    proc = multiprocessing.Process(
        target=_hold_lease_and_signal, args=(str(path), ready, 1.0)
    )
    proc.start()
    try:
        assert ready.wait(timeout=5.0), "child never acquired the lease"
        # The child process genuinely holds the OS-level lock now. A second
        # acquirer in *this* process must be refused, not silently succeed.
        waiter = ProcessLease(path, owner="parent")
        with pytest.raises(LeaseTimeout):
            with waiter.acquire(timeout=0.3, poll_interval=0.05):
                pytest.fail("acquired a lease a live child process holds")
    finally:
        proc.join(timeout=5.0)
    # Now that the child exited (and released cleanly), it must be free.
    with ProcessLease(path, owner="parent").acquire(timeout=1.0):
        pass


def _hold_lease_forever(path_str: str, ready_event) -> None:
    lease = ProcessLease(path_str, owner="doomed-child")
    with lease.acquire(timeout=5.0):
        ready_event.set()
        time.sleep(60)


def test_killed_holder_releases_the_lease(tmp_path):
    """A crashed holder (SIGKILL, no chance to run its own cleanup) must not
    wedge the lease forever. This is the whole reason the design is
    ``flock`` rather than a manual PID/TTL steal scheme: the kernel releases
    the lock the instant the process dies, no staleness policy required."""
    path = tmp_path / "ship.lock"
    ready = multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold_lease_forever, args=(str(path), ready))
    proc.start()
    try:
        assert ready.wait(timeout=5.0), "child never acquired the lease"
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=5.0)
        assert not proc.is_alive()

        # The kernel drops flock automatically on process exit, dead or
        # alive — a fresh acquirer must succeed well within a short timeout,
        # with no manual recovery step.
        deadline = time.monotonic() + 5.0
        acquired = False
        last_error = None
        while time.monotonic() < deadline and not acquired:
            try:
                with ProcessLease(path, owner="recoverer").acquire(
                    timeout=0.5, poll_interval=0.05
                ):
                    acquired = True
            except LeaseTimeout as exc:  # pragma: no cover - retry loop
                last_error = exc
        assert acquired, f"lease never recovered after holder was killed: {last_error}"
    finally:
        if proc.is_alive():  # pragma: no cover - safety net
            proc.terminate()
            proc.join(timeout=5.0)


def test_holder_record_cleared_on_clean_release(tmp_path):
    path = tmp_path / "ship.lock"
    lease = ProcessLease(path, owner="a")
    with lease.acquire(timeout=1.0):
        pass
    # A stale holder record left behind after a clean release would make a
    # later timeout message lie about who currently holds the lease.
    assert lease.current_holder() is None


def test_exception_inside_the_block_still_releases(tmp_path):
    path = tmp_path / "ship.lock"
    lease = ProcessLease(path, owner="a")
    with pytest.raises(RuntimeError):
        with lease.acquire(timeout=1.0):
            raise RuntimeError("boom")
    with ProcessLease(path, owner="b").acquire(timeout=1.0):
        pass
