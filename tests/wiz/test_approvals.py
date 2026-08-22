"""An approval means one specific thing, once, for a while.

Every test here is one of the brief's §32 requirements, written as the attack it
prevents rather than as the feature it describes.
"""

from __future__ import annotations

import pytest

from openjarvis.wiz.approvals import (
    DEFAULT_TTL_SECONDS,
    ApprovalError,
    ApprovalStore,
    fingerprint,
)
from openjarvis.wiz.journal import WizJournal


class _Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def store(clock, tmp_path):
    return ApprovalStore(
        clock=clock,
        journal=WizJournal(tmp_path / "journal.jsonl"),
        timestamp=lambda: "2026-08-17T10:00:00+00:00",
    )


def _issue(store, **kwargs):
    defaults = dict(
        capability="feature.merge",
        subject="FEAT-00001",
        parameters={"sha": "a" * 40},
        actor_id="operator",
        channel="control_center",
        summary="merge the coach dashboard",
    )
    defaults.update(kwargs)
    return store.issue(**defaults)


class TestBinding:
    def test_an_approval_redeems_for_the_action_it_named(self, store):
        approval = _issue(store)
        redeemed = store.redeem(
            approval.token,
            capability="feature.merge",
            subject="FEAT-00001",
            parameters={"sha": "a" * 40},
        )
        assert redeemed.redeemed

    def test_a_changed_sha_invalidates_the_approval(self, store):
        # The attack: operator approves merging commit A, branch advances to B,
        # the same token is presented for B.
        approval = _issue(store)
        with pytest.raises(ApprovalError) as exc:
            store.redeem(
                approval.token,
                capability="feature.merge",
                subject="FEAT-00001",
                parameters={"sha": "b" * 40},
            )
        assert "changed" in str(exc.value)

    def test_a_different_capability_cannot_use_the_approval(self, store):
        approval = _issue(store)
        with pytest.raises(ApprovalError):
            store.redeem(
                approval.token,
                capability="feature.deploy",
                subject="FEAT-00001",
                parameters={"sha": "a" * 40},
            )

    def test_a_different_subject_cannot_use_the_approval(self, store):
        approval = _issue(store)
        with pytest.raises(ApprovalError):
            store.redeem(
                approval.token,
                capability="feature.merge",
                subject="FEAT-00002",
                parameters={"sha": "a" * 40},
            )

    def test_extra_parameters_invalidate_the_approval(self, store):
        approval = _issue(store)
        with pytest.raises(ApprovalError):
            store.redeem(
                approval.token,
                capability="feature.merge",
                subject="FEAT-00001",
                parameters={"sha": "a" * 40, "force": True},
            )

    def test_the_fingerprint_is_order_independent(self):
        assert fingerprint(
            capability="c", subject="s", parameters={"a": 1, "b": 2}
        ) == fingerprint(capability="c", subject="s", parameters={"b": 2, "a": 1})


class TestSingleUse:
    def test_an_approval_cannot_be_used_twice(self, store):
        approval = _issue(store)
        store.redeem(
            approval.token,
            capability="feature.merge",
            subject="FEAT-00001",
            parameters={"sha": "a" * 40},
        )
        with pytest.raises(ApprovalError) as exc:
            store.redeem(
                approval.token,
                capability="feature.merge",
                subject="FEAT-00001",
                parameters={"sha": "a" * 40},
            )
        assert "already been used" in str(exc.value)


class TestExpiry:
    def test_a_stale_approval_cannot_execute(self, store, clock):
        approval = _issue(store)
        clock.advance(DEFAULT_TTL_SECONDS + 1)
        with pytest.raises(ApprovalError) as exc:
            store.redeem(
                approval.token,
                capability="feature.merge",
                subject="FEAT-00001",
                parameters={"sha": "a" * 40},
            )
        assert "expired" in str(exc.value)

    def test_an_approval_works_right_up_to_expiry(self, store, clock):
        approval = _issue(store)
        clock.advance(DEFAULT_TTL_SECONDS - 1)
        assert store.redeem(
            approval.token,
            capability="feature.merge",
            subject="FEAT-00001",
            parameters={"sha": "a" * 40},
        ).redeemed

    def test_expired_approvals_are_not_pending(self, store, clock):
        _issue(store)
        assert len(store.pending()) == 1
        clock.advance(DEFAULT_TTL_SECONDS + 1)
        assert store.pending() == []

    def test_purging_removes_expired_approvals(self, store, clock):
        _issue(store)
        clock.advance(DEFAULT_TTL_SECONDS + 1)
        assert store.purge_expired() == 1


class TestUnknownTokens:
    def test_an_invented_token_is_refused(self, store):
        with pytest.raises(ApprovalError):
            store.redeem("not-a-real-token", capability="feature.merge")

    def test_an_empty_token_is_refused(self, store):
        with pytest.raises(ApprovalError):
            store.redeem("", capability="feature.merge")


class TestAudit:
    def test_issuing_and_redeeming_are_both_recorded(self, store, tmp_path):
        approval = _issue(store)
        store.redeem(
            approval.token,
            capability="feature.merge",
            subject="FEAT-00001",
            parameters={"sha": "a" * 40},
        )
        journal = WizJournal(tmp_path / "journal.jsonl")
        kinds = [e.kind for e in journal.entries()]
        assert kinds == ["approval.issued", "approval.redeemed"]
        intact, _ = journal.verify()
        assert intact

    def test_the_token_is_never_written_to_the_journal(self, store, tmp_path):
        # It is a bearer credential, and an audit log is a file that gets read.
        approval = _issue(store)
        contents = (tmp_path / "journal.jsonl").read_text()
        assert approval.token not in contents

    def test_the_record_says_what_was_agreed_to(self, store, tmp_path):
        _issue(store)
        journal = WizJournal(tmp_path / "journal.jsonl")
        entry = journal.entries()[0]
        assert entry.reason == "merge the coach dashboard"
        assert entry.detail["subject"] == "FEAT-00001"

    def test_a_broken_journal_does_not_block_an_approval(self, clock):
        class BrokenJournal:
            def record(self, **kwargs):
                raise OSError("disk full")

        store = ApprovalStore(clock=clock, journal=BrokenJournal())
        approval = _issue(store)
        assert store.redeem(
            approval.token,
            capability="feature.merge",
            subject="FEAT-00001",
            parameters={"sha": "a" * 40},
        ).redeemed


class TestDoesNotSurviveRestart:
    def test_a_new_store_does_not_honour_an_old_token(self, clock):
        first = ApprovalStore(clock=clock)
        approval = _issue(first)
        second = ApprovalStore(clock=clock)
        with pytest.raises(ApprovalError):
            second.redeem(
                approval.token,
                capability="feature.merge",
                subject="FEAT-00001",
                parameters={"sha": "a" * 40},
            )
