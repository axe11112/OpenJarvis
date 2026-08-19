"""Tests for durable state writes.

The property under test is not "the file has the right bytes afterwards" —
`write_text` manages that. It is that there is no moment at which a reader sees
anything other than the whole old file or the whole new one, because the reader
in question is the next process to start after this one was killed mid-write,
and every loader in this package treats an unparseable file as an empty one.
"""

from __future__ import annotations

import json
import os

from openjarvis.reliability.statefile import (
    write_bytes_atomic,
    write_json_atomic,
    write_text_atomic,
)


class TestItWrites:
    def test_a_new_file(self, tmp_path):
        target = tmp_path / "state.json"
        assert write_json_atomic(target, {"a": 1})
        assert json.loads(target.read_text()) == {"a": 1}

    def test_it_creates_missing_parents(self, tmp_path):
        target = tmp_path / "deep" / "deeper" / "state.json"
        assert write_json_atomic(target, [1, 2])
        assert json.loads(target.read_text()) == [1, 2]

    def test_it_replaces_existing_content(self, tmp_path):
        target = tmp_path / "state.json"
        write_json_atomic(target, {"old": True})
        write_json_atomic(target, {"new": True})
        assert json.loads(target.read_text()) == {"new": True}

    def test_it_applies_a_mode(self, tmp_path):
        target = tmp_path / "key.json"
        write_json_atomic(target, {"k": "v"}, mode=0o600)
        assert oct(target.stat().st_mode)[-3:] == "600"

    def test_text_round_trips_unicode(self, tmp_path):
        target = tmp_path / "note.txt"
        write_text_atomic(target, "Sir, it's fixed — 🜂")
        assert target.read_text(encoding="utf-8") == "Sir, it's fixed — 🜂"


class TestTheOldFileSurvivesAFailedWrite:
    """The point of the exercise. A truncating write cannot promise this."""

    def test_a_failure_at_the_rename_leaves_the_previous_state(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "ledger.json"
        write_json_atomic(target, {"told-the-owner": "once"})

        def _no(*_a, **_k):
            raise OSError("interrupted")

        monkeypatch.setattr(os, "replace", _no)
        assert write_json_atomic(target, {"clobbered": True}) is False
        assert json.loads(target.read_text()) == {"told-the-owner": "once"}

    def test_an_unserializable_payload_never_touches_the_file(self, tmp_path):
        """Serialize first, then write: the old state outlives a bad payload."""
        target = tmp_path / "ledger.json"
        write_json_atomic(target, {"told-the-owner": "once"})
        assert write_json_atomic(target, {"fh": object()}) is False
        assert json.loads(target.read_text()) == {"told-the-owner": "once"}

    def test_a_failed_write_leaves_no_litter(self, tmp_path, monkeypatch):
        target = tmp_path / "ledger.json"
        write_json_atomic(target, {"a": 1})

        def _no(*_a, **_k):
            raise OSError("interrupted")

        monkeypatch.setattr(os, "replace", _no)
        write_json_atomic(target, {"b": 2})
        assert [p.name for p in tmp_path.iterdir()] == ["ledger.json"]

    def test_an_unwritable_directory_is_reported_not_raised(self, tmp_path):
        target = tmp_path / "nope"
        target.mkdir()
        # A directory where a file should be: every OSError path returns False
        # rather than unwinding into the watcher.
        assert write_bytes_atomic(target, b"x") is False


class TestTheLedgerSurvivesIt:
    def test_a_failed_save_does_not_erase_what_the_owner_was_told(
        self, tmp_path, monkeypatch
    ):
        """The concrete regression: a sleep between truncate and write used to
        empty the ledger, and an empty ledger means every incident is news
        again — including the CRITICAL ones."""
        from openjarvis.reliability.notify_ledger import NotificationLedger

        path = tmp_path / "ledger.json"
        ledger = NotificationLedger(path=path)
        ledger._entries = {"INC-00001:needs-you:HIGH": {"at": "2026-01-01T00:00:00Z"}}
        ledger._save()

        def _no(*_a, **_k):
            raise OSError("the machine went to sleep")

        monkeypatch.setattr(os, "replace", _no)
        ledger._entries = {"INC-00002:needs-you:HIGH": {"at": "2026-01-02T00:00:00Z"}}
        ledger._save()

        assert json.loads(path.read_text()) == {
            "INC-00001:needs-you:HIGH": {"at": "2026-01-01T00:00:00Z"}
        }
        assert NotificationLedger(path=path)._entries
