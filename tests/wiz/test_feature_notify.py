"""FeatureOwnerNotifier: the owner hears about a feature exactly twice."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from openjarvis.wiz.features.notify import (
    NEEDS_OWNER_KINDS,
    SUCCESS_KIND,
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
