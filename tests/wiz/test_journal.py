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
