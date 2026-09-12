"""The journal is tamper-evident, or it is decoration."""

from __future__ import annotations

import json

from openjarvis.wiz.journal import GENESIS, WizJournal


def _journal(tmp_path) -> WizJournal:
    return WizJournal(tmp_path / "journal.jsonl")


def _record(journal, n=3, kind="authority.granted"):
    for i in range(n):
        journal.record(
            at=f"2026-08-17T10:0{i}:00+00:00",
            kind=kind,
            capability="thing.read",
            actor_id="operator",
            channel="cli",
            reason="granted",
        )


class TestChaining:
    def test_an_empty_journal_verifies(self, tmp_path):
        assert _journal(tmp_path).verify() == (True, None)

    def test_the_first_entry_chains_to_genesis(self, tmp_path):
        journal = _journal(tmp_path)
        entry = journal.record(at="now", kind="k", reason="r")
        assert entry.previous_hash == GENESIS
        assert entry.sequence == 1

    def test_entries_chain_to_their_predecessor(self, tmp_path):
        journal = _journal(tmp_path)
        _record(journal, 5)
        entries = journal.entries()
        for previous, current in zip(entries, entries[1:]):
            assert current.previous_hash == previous.entry_hash
        assert journal.verify() == (True, None)

    def test_a_reopened_journal_continues_the_chain(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        _record(WizJournal(path), 2)
        _record(WizJournal(path), 2)
        journal = WizJournal(path)
        assert [e.sequence for e in journal.entries()] == [1, 2, 3, 4]
        assert journal.verify() == (True, None)


class TestTamperEvidence:
    def test_editing_an_entry_breaks_the_chain(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        journal = WizJournal(path)
        _record(journal, 4)

        lines = path.read_text().splitlines()
        forged = json.loads(lines[1])
        forged["reason"] = "granted, honestly"
        lines[1] = json.dumps(forged, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")

        intact, broken_at = journal.verify()
        assert not intact
        assert broken_at == 2

    def test_changing_a_refusal_into_a_grant_is_detected(self, tmp_path):
        # The forgery that actually matters: making the record say Wiz was
        # allowed to do the thing it was refused.
        path = tmp_path / "journal.jsonl"
        journal = WizJournal(path)
        journal.record(at="t", kind="authority.refused", reason="no")
        journal.record(at="t", kind="authority.granted", reason="yes")

        lines = path.read_text().splitlines()
        forged = json.loads(lines[0])
        forged["kind"] = "authority.granted"
        lines[0] = json.dumps(forged, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")

        intact, broken_at = journal.verify()
        assert not intact
        assert broken_at == 1

    def test_deleting_an_entry_is_detected(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        journal = WizJournal(path)
        _record(journal, 4)

        lines = path.read_text().splitlines()
        del lines[1]
        path.write_text("\n".join(lines) + "\n")

        intact, broken_at = journal.verify()
        assert not intact

    def test_appending_a_fabricated_entry_is_detected(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        journal = WizJournal(path)
        _record(journal, 2)

        fabricated = {
            "sequence": 3,
            "at": "later",
            "kind": "authority.granted",
            "capability": "thing.deploy",
            "actor_id": "operator",
            "channel": "control_center",
            "reason": "definitely allowed",
            "detail": {},
            "previous_hash": "0" * 64,
            "entry_hash": "f" * 64,
        }
        with open(path, "a") as handle:
            handle.write(
                json.dumps(fabricated, sort_keys=True, separators=(",", ":")) + "\n"
            )

        intact, broken_at = journal.verify()
        assert not intact
        assert broken_at == 3


def _hammer_journal(path_str: str, kind: str, n: int, barrier) -> None:
    journal = WizJournal(path_str)
    barrier.wait()  # start all workers together, to maximise contention
    for i in range(n):
        journal.record(at=f"{kind}-{i}", kind=kind, reason=f"{kind} entry {i}")


class TestCrossProcessSafety:
    """A threading.Lock alone would let two OpenJarvis processes each read
    the same tail and append the same sequence number. These prove the
    kernel-level lease actually prevents that between real processes, not
    just between threads in one."""

    def test_two_processes_appending_at_once_produce_no_duplicate_sequence(
        self, tmp_path
    ):
        import multiprocessing

        path = tmp_path / "journal.jsonl"
        n_per_worker = 15
        barrier = multiprocessing.Barrier(3)
        workers = [
            multiprocessing.Process(
                target=_hammer_journal,
                args=(str(path), f"worker-{i}", n_per_worker, barrier),
            )
            for i in range(3)
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=30)
            assert not w.is_alive()
            assert w.exitcode == 0

        journal = WizJournal(path)
        entries = journal.entries()
        assert len(entries) == 3 * n_per_worker

        sequences = [e.sequence for e in entries]
        assert sorted(sequences) == list(range(1, len(entries) + 1)), (
            "duplicate or skipped sequence numbers: two processes raced the "
            "same tail read"
        )
        assert len(set(sequences)) == len(sequences)

        intact, broken_at = journal.verify()
        assert intact, f"chain broken at sequence {broken_at}"

    def test_lease_file_sits_beside_the_journal_not_inside_it(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        journal = WizJournal(path)
        journal.record(at="t", kind="k", reason="r")
        assert (tmp_path / "journal.jsonl.lock").exists()
        # The lock file's bookkeeping content must never be mistaken for a
        # journal entry by a naive reader of the directory.
        entries = journal.entries()
        assert len(entries) == 1


class TestContents:
    def test_entries_carry_the_context_an_operator_needs(self, tmp_path):
        journal = _journal(tmp_path)
        entry = journal.record(
            at="2026-08-17T10:00:00+00:00",
            kind="authority.refused",
            capability="feature.merge",
            actor_id="operator",
            channel="voice",
            reason="PRODUCTION_CHANGE can never be exercised from voice",
            detail={"risk": "HIGH"},
        )
        assert entry.channel == "voice"
        assert entry.capability == "feature.merge"
        assert "voice" in entry.reason
        assert entry.detail == {"risk": "HIGH"}

    def test_tail_returns_the_most_recent(self, tmp_path):
        journal = _journal(tmp_path)
        _record(journal, 10)
        assert [e.sequence for e in journal.tail(3)] == [8, 9, 10]


class TestCorruptionIsReportedNotHidden:
    """A journal that says it is intact when it is not is worse than none.

    _read() stopped iterating at the first unparseable line, and carried a
    comment saying "the chain check below will fail on it, which is the point".
    Stopping the iteration is precisely what stopped the chain check from ever
    reaching it: verify() walked the clean prefix, found it valid, and returned
    (True, None) for a file with a torn line in the middle. Tamper-evidence
    that a tamper switches off is not tamper-evidence.
    """

    def _corrupt_line(self, path, index: int) -> None:
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[index] = '{"this is not valid json'
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_a_corrupt_middle_line_is_a_broken_chain(self, tmp_path):
        journal = _journal(tmp_path)
        _record(journal, n=5)
        assert journal.verify() == (True, None)

        self._corrupt_line(journal.path, 2)

        intact, broken_at = journal.verify()
        assert not intact, "a journal with an unparseable line reported itself intact"
        assert broken_at == 3, f"pointed at the wrong place: {broken_at}"

    def test_a_corrupt_first_line_is_a_broken_chain(self, tmp_path):
        journal = _journal(tmp_path)
        _record(journal, n=3)
        self._corrupt_line(journal.path, 0)
        intact, broken_at = journal.verify()
        assert not intact
        assert broken_at == 1

    def test_the_next_append_does_not_reuse_a_sequence(self, tmp_path):
        """The forked chain: _tail_locked read the last *readable* entry.

        With a corrupt line at position 3 of 5, the last readable entry is
        sequence 2, so the next record was issued sequence 3 -- a number
        entries further down the file already used. Disk order became
        1, 2, <corrupt>, 4, 5, 3, and verify() still said intact.
        """
        import json

        journal = _journal(tmp_path)
        _record(journal, n=5)
        self._corrupt_line(journal.path, 2)

        journal.record(at="later", kind="event.after", reason="after the damage")

        sequences = []
        for line in journal.path.read_text(encoding="utf-8").splitlines():
            try:
                sequences.append(int(json.loads(line)["sequence"]))
            except Exception:  # noqa: BLE001 - the corrupt line
                continue
        assert len(sequences) == len(set(sequences)), (
            f"a sequence number was issued twice: {sequences}"
        )
        assert max(sequences) == 6, f"expected to continue past 5, got {sequences}"
        # And the damage is still reported, not papered over by the new entry.
        assert journal.verify()[0] is False

    def test_a_torn_final_line_does_not_destroy_the_next_record(self, tmp_path):
        """A process killed mid-write leaves a line with no newline.

        The next append landed on the end of it and welded the two together,
        so one lost entry cost a second, perfectly good one as collateral.
        """
        import json

        journal = _journal(tmp_path)
        journal.record(at="t1", kind="event.first", reason="before the crash")
        with open(journal.path, "a", encoding="utf-8") as handle:
            handle.write('{"sequence": 2, "at": "t2", "kind": "torn')

        journal.record(at="t3", kind="event.after", reason="after the crash")

        lines = journal.path.read_text(encoding="utf-8").splitlines()
        parseable = []
        for line in lines:
            try:
                parseable.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        kinds = [entry["kind"] for entry in parseable]
        assert "event.first" in kinds, "the record before the crash was lost"
        assert "event.after" in kinds, (
            "the record after the crash was welded onto the torn line and lost"
        )
        assert journal.verify()[0] is False, "the torn line should be visible"

    def test_display_still_shows_what_can_be_read(self, tmp_path):
        """tail() stays lenient: showing two entries beats showing none.

        Integrity questions go to verify(), which does not forgive. These are
        different jobs and must not be collapsed into one answer.
        """
        journal = _journal(tmp_path)
        _record(journal, n=5)
        self._corrupt_line(journal.path, 2)
        assert len(journal.tail(50)) == 2
        assert journal.verify()[0] is False


class TestManyProcessesAtOnce:
    """Eight processes, a hundred entries each, one intact chain."""

    def test_eight_processes_produce_one_gapless_chain(self, tmp_path):
        import multiprocessing

        path = tmp_path / "journal.jsonl"
        workers_count = 8
        per_worker = 100
        barrier = multiprocessing.Barrier(workers_count)
        workers = [
            multiprocessing.Process(
                target=_hammer_journal,
                args=(str(path), f"worker-{i}", per_worker, barrier),
            )
            for i in range(workers_count)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=180)
            assert not worker.is_alive(), "a writer wedged on the journal lease"
            assert worker.exitcode == 0

        journal = WizJournal(path)
        entries = journal.entries()
        expected = workers_count * per_worker

        # Every event exactly once -- not merely the right count.
        assert len(entries) == expected, f"{len(entries)} of {expected} survived"
        reasons = [entry.reason for entry in entries]
        assert len(set(reasons)) == expected, "an entry was lost or duplicated"
        for i in range(workers_count):
            mine = [r for r in reasons if r.startswith(f"worker-{i} ")]
            assert len(mine) == per_worker, (
                f"worker-{i} recorded {len(mine)} of {per_worker} entries"
            )

        sequences = [entry.sequence for entry in entries]
        assert sequences == list(range(1, expected + 1)), (
            "sequences are not gapless and in order"
        )

        intact, broken_at = journal.verify()
        assert intact, f"chain broken at sequence {broken_at}"
