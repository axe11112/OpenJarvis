"""FeatureOwnerNotifier: the owner hears about a feature exactly twice."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass

import pytest

from openjarvis.wiz.features.notify import (
    NEEDS_OWNER_KINDS,
    SUCCESS_KIND,
    SUCCESS_KINDS,
    FeatureOwnerNotifier,
)


@dataclass
class FakeFeature:
    id: str = "FEAT-00099"
    title: str = "Add a download button"


class Recorder:
    def __init__(self, *, fail=False):
        self.sent = []
        self.fail = fail

    def __call__(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram is down")
        self.sent.append(text)


def build(tmp_path, *, fail=False):
    recorder = Recorder(fail=fail)
    notifier = FeatureOwnerNotifier(
        send=recorder, ledger_path=tmp_path / "notify_ledger.json"
    )
    return notifier, recorder


class TestSuccessMessage:
    def test_a_shipped_feature_says_its_live(self, tmp_path):
        notifier, recorder = build(tmp_path)
        sent = notifier.notify(
            FakeFeature(), kind=SUCCESS_KIND, reason="production agrees"
        )
        assert sent
        assert len(recorder.sent) == 1
        assert "it's live" in recorder.sent[0]
        assert "Add a download button" in recorder.sent[0]

    def test_the_success_message_names_no_internal_kind_or_reason(self, tmp_path):
        # The owner should never see the machine's own vocabulary.
        notifier, recorder = build(tmp_path)
        notifier.notify(
            FakeFeature(), kind=SUCCESS_KIND, reason="production agrees: ok"
        )
        assert "feature.shipped" not in recorder.sent[0]

    def test_an_externally_reconciled_feature_also_says_its_live(self, tmp_path):
        # FEAT-00030: reconcile_external_merge() journals a distinct kind
        # (never "feature.shipped", so the audit trail never implies
        # ship() performed a merge it did not) — but the owner must still
        # hear "it's live" exactly like an ordinary shipped feature.
        notifier, recorder = build(tmp_path)
        sent = notifier.notify(
            FakeFeature(),
            kind="feature.external_bypass_reconciled",
            reason="production agrees",
        )
        assert sent
        assert "it's live" in recorder.sent[0]

    def test_success_kinds_include_both(self):
        assert SUCCESS_KIND in SUCCESS_KINDS
        assert "feature.external_bypass_reconciled" in SUCCESS_KINDS


class TestNeedsOwnerMessage:
    def test_every_needs_owner_kind_produces_a_message(self, tmp_path):
        for kind in NEEDS_OWNER_KINDS:
            notifier, recorder = build(tmp_path)
            sent = notifier.notify(FakeFeature(), kind=kind, reason="a specific ask")
            assert sent, kind
            assert "I need your help" in recorder.sent[0]
            assert "a specific ask" in recorder.sent[0]


class TestSilenceByDefault:
    @pytest.mark.parametrize(
        "kind",
        [
            "feature.received",
            "feature.building",
            "feature.testing",
            "feature.retrying",
            "feature.previewing",
            "feature.verifying",
            "feature.ready",
            "feature.pr_created",
            "feature.ship_refused",
            "feature.merging",
            "feature.deploying",
            "feature.production_verifying",
            "feature.cancelled",
            "feature.auto_ship_skipped",
            "feature.yielded",
        ],
    )
    def test_a_step_kind_sends_nothing(self, tmp_path, kind):
        notifier, recorder = build(tmp_path)
        sent = notifier.notify(FakeFeature(), kind=kind, reason="whatever")
        assert not sent
        assert recorder.sent == []


class TestDeduplication:
    def test_the_same_outcome_is_not_repeated(self, tmp_path):
        notifier, recorder = build(tmp_path)
        notifier.notify(FakeFeature(), kind=SUCCESS_KIND, reason="production agrees")
        second = notifier.notify(
            FakeFeature(), kind=SUCCESS_KIND, reason="production agrees"
        )
        assert not second
        assert len(recorder.sent) == 1

    def test_dedup_survives_a_fresh_instance_pointed_at_the_same_ledger(self, tmp_path):
        path = tmp_path / "notify_ledger.json"
        first = FeatureOwnerNotifier(send=Recorder(), ledger_path=path)
        first.notify(FakeFeature(), kind=SUCCESS_KIND, reason="production agrees")

        recorder = Recorder()
        second = FeatureOwnerNotifier(send=recorder, ledger_path=path)
        sent = second.notify(
            FakeFeature(), kind=SUCCESS_KIND, reason="production agrees"
        )
        assert not sent
        assert recorder.sent == []

    def test_a_materially_different_reason_is_told(self, tmp_path):
        notifier, recorder = build(tmp_path)
        notifier.notify(
            FakeFeature(), kind="feature.no_verifier", reason="no browser here"
        )
        notifier.notify(
            FakeFeature(),
            kind="feature.no_verifier",
            reason="a completely different problem now",
        )
        assert len(recorder.sent) == 2

    def test_different_features_are_independent(self, tmp_path):
        notifier, recorder = build(tmp_path)
        notifier.notify(
            FakeFeature(id="FEAT-00001"), kind=SUCCESS_KIND, reason="production agrees"
        )
        notifier.notify(
            FakeFeature(id="FEAT-00002"), kind=SUCCESS_KIND, reason="production agrees"
        )
        assert len(recorder.sent) == 2


class TestFailureIsolation:
    def test_a_failed_send_does_not_raise(self, tmp_path):
        notifier, _ = build(tmp_path, fail=True)
        sent = notifier.notify(
            FakeFeature(), kind=SUCCESS_KIND, reason="production agrees"
        )
        assert not sent

    def test_a_feature_with_no_id_is_not_notified(self, tmp_path):
        notifier, recorder = build(tmp_path)
        sent = notifier.notify(
            FakeFeature(id=""), kind=SUCCESS_KIND, reason="production agrees"
        )
        assert not sent
        assert recorder.sent == []

    def test_a_transient_send_failure_does_not_permanently_suppress_it(
        self, tmp_path
    ):
        # The ledger must not record "told them" before the send is actually
        # confirmed - otherwise a single flaky send (Telegram down for a
        # moment) marks the digest as delivered forever, and a COMPLETE or
        # HUMAN_REQUIRED message is lost with no way to recover it short of
        # the wording changing.
        ledger_path = tmp_path / "notify_ledger.json"
        failing = Recorder(fail=True)
        notifier = FeatureOwnerNotifier(send=failing, ledger_path=ledger_path)

        first_attempt = notifier.notify(
            FakeFeature(), kind=SUCCESS_KIND, reason="production agrees"
        )
        assert not first_attempt
        assert failing.sent == []
        assert not ledger_path.exists() or ledger_path.read_text().strip() in ("", "{}")

        # The underlying transport recovers (retried by whatever drives this
        # notifier - a later pipeline step, a restart). The exact same
        # outcome must still be deliverable.
        working = Recorder(fail=False)
        notifier.send = working
        retried = notifier.notify(
            FakeFeature(), kind=SUCCESS_KIND, reason="production agrees"
        )
        assert retried
        assert len(working.sent) == 1

        # And now that it has actually gone out, it is deduplicated normally.
        third_attempt = notifier.notify(
            FakeFeature(), kind=SUCCESS_KIND, reason="production agrees"
        )
        assert not third_attempt
        assert len(working.sent) == 1

    def test_concurrent_notifiers_never_double_send_the_same_outcome(self, tmp_path):
        # send() runs inside the lock precisely so this holds: two notify()
        # calls racing for the same (feature, digest) must not both pass the
        # dedup check and both send.
        import threading

        ledger_path = tmp_path / "notify_ledger.json"
        recorder = Recorder()
        notifier = FeatureOwnerNotifier(send=recorder, ledger_path=ledger_path)

        barrier = threading.Barrier(5)

        def fire():
            barrier.wait()
            notifier.notify(FakeFeature(), kind=SUCCESS_KIND, reason="production agrees")

        threads = [threading.Thread(target=fire) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(recorder.sent) == 1


class TestTwoProcessesShareOneLedger:
    """The watcher and a CLI ship run at once; both send these messages.

    A ``threading.Lock`` is invisible between them. Each would read the
    ledger, find nothing, send, and write back the whole object -- so the
    owner hears the same thing twice *and* whichever feature's record lost the
    write is told everything about it again too. These tests use real
    subprocesses holding real ``flock``s because that is the only thing that
    actually arbitrates between two processes.
    """

    def _child(self, tmp_path, body: str):
        script = textwrap.dedent(
            f"""
            from pathlib import Path
            from openjarvis.wiz.features.notify import FeatureOwnerNotifier

            class F:
                id = "FEAT-CHILD"
                title = "Child feature"

            tmp = Path({str(tmp_path)!r})
            {body}
            """
        )
        return subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
        )

    def test_a_second_process_waits_outside_the_send(self, tmp_path):
        """Mutual exclusion where it matters: around the send, not after it."""
        ledger = tmp_path / "notify_ledger.json"
        entered = tmp_path / "child-entered-its-send"
        child = self._child(
            tmp_path,
            f"""
            import time

            def send(text):
                (tmp / "child-entered-its-send").write_text("yes")

            n = FeatureOwnerNotifier(send=send, ledger_path=Path({str(ledger)!r}))
            print("ready", flush=True)
            # Only once the other process is demonstrably inside its own send,
            # so that a lease that does not work shows up immediately.
            while not (tmp / "parent-in-send").exists():
                time.sleep(0.01)
            n.notify(F(), kind="feature.failed", reason="child reason")
            print("done", flush=True)
            """,
        )
        try:
            assert child.stdout.readline().strip() == "ready"
            observed = []

            def send(text: str) -> None:
                # The child is now trying to notify a *different* feature. If
                # the lease is real it cannot get into its own send until this
                # one returns.
                (tmp_path / "parent-in-send").write_text("yes")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    observed.append(entered.exists())
                    time.sleep(0.05)

            notifier = FeatureOwnerNotifier(send=send, ledger_path=ledger)
            assert notifier.notify(
                FakeFeature(), kind="feature.failed", reason="parent reason"
            )
            assert not any(observed), (
                "another process entered its send while this one held the ledger"
            )
            child.wait(timeout=20)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)

    def test_the_same_outcome_is_not_sent_twice_by_two_processes(self, tmp_path):
        """The duplicate the ledger exists to prevent, across a process line.

        Both processes read an empty ledger, both conclude the owner has not
        been told, and the owner gets the same sentence twice from one
        outcome.
        """
        ledger = tmp_path / "notify_ledger.json"
        child = self._child(
            tmp_path,
            f"""
            import time

            def send(text):
                (tmp / "child-sent").write_text(text)

            class F:
                id = "FEAT-00099"
                title = "Add a download button"

            n = FeatureOwnerNotifier(send=send, ledger_path=Path({str(ledger)!r}))
            print("ready", flush=True)
            while not (tmp / "parent-in-send").exists():
                time.sleep(0.01)
            n.notify(F(), kind="feature.failed", reason="the same reason")
            print("done", flush=True)
            """,
        )
        try:
            assert child.stdout.readline().strip() == "ready"

            def send(text: str) -> None:
                (tmp_path / "parent-in-send").write_text("yes")
                time.sleep(1.0)

            notifier = FeatureOwnerNotifier(send=send, ledger_path=ledger)
            assert notifier.notify(
                FakeFeature(), kind="feature.failed", reason="the same reason"
            )
            child.wait(timeout=20)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)

        assert not (tmp_path / "child-sent").exists(), (
            "the owner was told the same thing twice, once per process"
        )

    def test_neither_process_loses_the_other_s_record(self, tmp_path):
        """The read-modify-write hazard, not just the double send.

        Every write replaces the whole ledger object. Two unsynchronised
        writers do not merely duplicate one message -- one of them silently
        drops the other's feature entirely, and that feature is then told
        everything it was already told.
        """
        ledger = tmp_path / "notify_ledger.json"
        child = self._child(
            tmp_path,
            f"""
            n = FeatureOwnerNotifier(
                send=lambda t: None, ledger_path=Path({str(ledger)!r})
            )
            print("ready", flush=True)
            n.notify(F(), kind="feature.failed", reason="child reason")
            print("done", flush=True)
            """,
        )
        try:
            assert child.stdout.readline().strip() == "ready"

            def send(text: str) -> None:
                time.sleep(0.5)

            notifier = FeatureOwnerNotifier(send=send, ledger_path=ledger)
            notifier.notify(FakeFeature(), kind="feature.failed", reason="parent")
            child.wait(timeout=20)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)

        entries = json.loads(ledger.read_text())
        assert set(entries) == {"FEAT-00099", "FEAT-CHILD"}

    def test_a_lease_it_cannot_take_sends_anyway(self, tmp_path):
        """The one guard in this system that deliberately fails open.

        A refusal here is silence about a feature that needs a person, and
        nothing ever retries a notification that was never attempted. A
        duplicate message is recoverable; that silence is not.
        """
        ledger = tmp_path / "notify_ledger.json"
        script = textwrap.dedent(
            f"""
            import time
            from pathlib import Path
            from openjarvis.core.proclock import ProcessLease

            lease = ProcessLease(Path({str(ledger) + ".lock"!r}))
            with lease.acquire(timeout=5):
                print("holding", flush=True)
                time.sleep(120)
            """
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
        )
        try:
            assert holder.stdout.readline().strip() == "holding"
            recorder = Recorder()
            notifier = FeatureOwnerNotifier(
                send=recorder, ledger_path=ledger, lease_timeout=0.3
            )
            assert notifier.notify(
                FakeFeature(), kind="feature.needs_a_person", reason="decide this"
            )
            assert len(recorder.sent) == 1
        finally:
            holder.kill()
            holder.wait(timeout=10)


class TestTheLedgerSurvivesTheMachineStopping:
    def test_the_ledger_is_written_atomically(self, tmp_path, monkeypatch):
        """No caller of this module may truncate the ledger in place.

        ``Path.write_text`` truncates and then writes; interrupted between the
        two -- a sleeping laptop, a launchd reload, a code update, all of
        which this ledger exists to survive -- it leaves a file that exists,
        is readable, and is not JSON. That reads back as an empty ledger, and
        an empty ledger means the owner is told everything again.
        """
        import openjarvis.wiz.features.notify as mod

        calls = []
        real = mod.write_json_atomic
        monkeypatch.setattr(
            mod,
            "write_json_atomic",
            lambda path, payload, **kw: (calls.append(path), real(path, payload, **kw))[
                1
            ],
        )
        notifier, _recorder = build(tmp_path)
        notifier.notify(FakeFeature(), kind=SUCCESS_KIND, reason="")
        assert calls == [tmp_path / "notify_ledger.json"]

    def test_a_ledger_that_cannot_be_written_repeats_rather_than_forgets(
        self, tmp_path, monkeypatch
    ):
        """At-least-once, stated as a test.

        A lost write costs a repeat, never silence. This is the failure mode
        the send-then-record ordering deliberately accepts.
        """
        import openjarvis.wiz.features.notify as mod

        monkeypatch.setattr(mod, "write_json_atomic", lambda *a, **kw: False)
        notifier, recorder = build(tmp_path)
        feature = FakeFeature()
        assert notifier.notify(feature, kind=SUCCESS_KIND, reason="")
        assert notifier.notify(feature, kind=SUCCESS_KIND, reason="")
        assert len(recorder.sent) == 2

    def test_a_corrupt_ledger_is_kept_rather_than_overwritten(self, tmp_path):
        ledger = tmp_path / "notify_ledger.json"
        ledger.write_text('{"FEAT-1": {"kind": "feature.fai')

        notifier, recorder = build(tmp_path)
        assert notifier.notify(FakeFeature(), kind=SUCCESS_KIND, reason="")

        spoiled = tmp_path / "notify_ledger.json.corrupt"
        assert spoiled.exists(), "a corrupt ledger is evidence; do not delete it"
        assert spoiled.read_text() == '{"FEAT-1": {"kind": "feature.fai'
        assert json.loads(ledger.read_text())["FEAT-00099"]["kind"] == SUCCESS_KIND

    def test_a_ledger_that_is_not_an_object_is_kept_rather_than_overwritten(
        self, tmp_path
    ):
        ledger = tmp_path / "notify_ledger.json"
        ledger.write_text("[1, 2, 3]")
        notifier, recorder = build(tmp_path)
        assert notifier.notify(FakeFeature(), kind=SUCCESS_KIND, reason="")
        assert (tmp_path / "notify_ledger.json.corrupt").exists()

    def test_an_outcome_can_be_told_again_after_a_different_one(self, tmp_path):
        """The contract is "not twice in a row", not "never twice".

        A feature that needed a person, was fixed, shipped, and later needs
        them again for the identical reason must say so. Remembering every
        outcome a feature ever had would make that second message silence.
        """
        notifier, recorder = build(tmp_path)
        feature = FakeFeature()
        notifier.notify(feature, kind="feature.needs_a_person", reason="decide")
        notifier.notify(feature, kind=SUCCESS_KIND, reason="")
        notifier.notify(feature, kind="feature.needs_a_person", reason="decide")
        assert len(recorder.sent) == 3
