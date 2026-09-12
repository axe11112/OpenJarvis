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

# Through the compatibility shim on purpose: this file predates the move to
# core/, and keeping it importing the old path is what proves the shim still
# re-exports everything its importers used.
from openjarvis.wiz.proclock import (
    PRODUCTION_CHANGE_LOCK,
    LeaseTimeout,
    ProcessLease,
    production_change_lease,
)


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


def _hold_until_killed(path_str, ready):
    import time

    from openjarvis.core.proclock import ProcessLease

    with ProcessLease(path_str, owner="doomed").acquire(timeout=5.0):
        ready.set()
        time.sleep(60)


class TestIsHeld:
    """`is_held` asks the kernel; `current_holder` reads a record.

    The difference decides whether one crash stalls the system forever. The
    holder record is written after the lock is taken and truncated before it is
    released, so a SIGKILLed process leaves its record behind while the kernel
    drops its flock immediately. Anything that answered "is production busy?"
    from that record would say yes for the rest of the machine's uptime.
    """

    def test_a_free_lease_is_not_held(self, tmp_path):
        assert ProcessLease(tmp_path / "p.lock", owner="a").is_held() is False

    def test_a_lease_that_was_never_taken_is_not_held(self, tmp_path):
        """No file at all is the first-run case, not an error."""
        assert ProcessLease(tmp_path / "never.lock", owner="a").is_held() is False

    def test_a_held_lease_is_held(self, tmp_path):
        path = tmp_path / "p.lock"
        with ProcessLease(path, owner="holder").acquire(timeout=1.0):
            assert ProcessLease(path, owner="asker").is_held() is True

    def test_it_is_free_again_after_release(self, tmp_path):
        path = tmp_path / "p.lock"
        with ProcessLease(path, owner="holder").acquire(timeout=1.0):
            pass
        assert ProcessLease(path, owner="asker").is_held() is False

    def test_asking_does_not_take_the_lease(self, tmp_path):
        """A probe that left the lock held would deadlock the next real caller."""
        path = tmp_path / "p.lock"
        probe = ProcessLease(path, owner="asker")
        assert probe.is_held() is False
        # Still immediately acquirable by someone who actually wants it.
        with ProcessLease(path, owner="real").acquire(timeout=0.5):
            pass

    def test_a_sigkilled_holder_does_not_leave_it_held_forever(self, tmp_path):
        """The trap. current_holder() still names the dead holder; is_held()
        must not, or one crash stalls every deferral permanently."""
        import multiprocessing
        import os
        import signal
        import time

        path = tmp_path / "p.lock"
        ready = multiprocessing.Event()
        holder = multiprocessing.Process(
            target=_hold_until_killed, args=(str(path), ready)
        )
        holder.start()
        try:
            assert ready.wait(timeout=10.0), "the holder never took the lease"
            probe = ProcessLease(path, owner="probe")
            assert probe.is_held() is True

            os.kill(holder.pid, signal.SIGKILL)
            holder.join(timeout=10.0)
            time.sleep(0.2)

            assert probe.is_held() is False, (
                "a SIGKILLed holder left the lease reading as held; anything "
                "deferring to it would stall for the rest of this machine's "
                "uptime"
            )
            # And the record really does still name the dead holder, which is
            # why is_held() must not be built on it.
            assert probe.current_holder() is not None
        finally:
            if holder.is_alive():  # pragma: no cover - defensive
                holder.terminate()
                holder.join(timeout=5.0)


class TestEveryProductionChangeCallerGetsTheSameLock:
    """One lock file, or the lease guards nothing.

    Three call sites take the production-change lease: FeaturePipeline.ship()
    (wired in wiz/assemble.py), the reliability repair loop (wired in
    cli/reliability_cmd.py), and the development queue's is_held() probe. If any
    of them resolved a different path the mutual exclusion would silently
    evaporate -- each subsystem serialised against itself, which is exactly the
    state this lease was introduced to fix.

    The specific trap: wiz_home() is get_config_dir()/wiz, and the lease belongs
    at get_config_dir(). A caller that reached for the Wiz root out of habit
    would produce a second, private lock that looks identical in every log line.
    """

    def test_the_default_root_is_stable_across_calls(self):
        first = production_change_lease(owner="a")
        second = production_change_lease(owner="b")
        assert first.path == second.path

    def test_it_is_not_under_the_wiz_root(self):
        """Where the repair loop, which knows nothing about Wiz, can find it."""
        from openjarvis.wiz.runtime import wiz_home

        path = production_change_lease(owner="a").path
        assert wiz_home() not in path.parents, (
            f"the production-change lease is inside the Wiz root ({path}); the "
            "reliability side resolves it from the config root and would take a "
            "different file"
        )

    def test_the_filename_is_the_shared_constant(self):
        assert production_change_lease(owner="a").path.name == PRODUCTION_CHANGE_LOCK

    def test_two_owners_on_the_default_root_really_exclude_each_other(
        self, monkeypatch, tmp_path
    ):
        """Not just equal paths -- actual mutual exclusion through them."""
        monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
        shipper = production_change_lease(owner="wiz@target")
        repairer = production_change_lease(owner="reliability-repair")
        assert shipper.path == repairer.path

        with shipper.acquire(timeout=1.0):
            assert repairer.is_held() is True
            with pytest.raises(LeaseTimeout):
                with repairer.acquire(timeout=0.3):
                    pass
        assert repairer.is_held() is False
