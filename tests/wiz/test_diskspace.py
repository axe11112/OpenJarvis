"""has_enough_disk: the preflight that stops heavy work before it crashes."""

from __future__ import annotations

from openjarvis.wiz.features import diskspace


class FakeUsage:
    def __init__(self, free):
        self.free = free


class TestHasEnoughDisk:
    def test_enough_free_space_is_true(self, monkeypatch):
        monkeypatch.setattr(
            diskspace.shutil, "disk_usage", lambda p: FakeUsage(5 * 1024**3)
        )
        assert diskspace.has_enough_disk("/", min_free_bytes=2 * 1024**3)

    def test_too_little_free_space_is_false(self, monkeypatch):
        monkeypatch.setattr(diskspace.shutil, "disk_usage", lambda p: FakeUsage(1024))
        assert not diskspace.has_enough_disk("/", min_free_bytes=2 * 1024**3)

    def test_exactly_the_threshold_counts_as_enough(self, monkeypatch):
        monkeypatch.setattr(
            diskspace.shutil, "disk_usage", lambda p: FakeUsage(2 * 1024**3)
        )
        assert diskspace.has_enough_disk("/", min_free_bytes=2 * 1024**3)

    def test_an_unstattable_path_is_not_enough(self, monkeypatch):
        def raises(path):
            raise OSError("no such volume")

        monkeypatch.setattr(diskspace.shutil, "disk_usage", raises)
        assert not diskspace.has_enough_disk("/gone")

    def test_default_threshold_is_two_gib(self):
        assert diskspace.DEFAULT_MIN_FREE_BYTES == 2 * 1024 * 1024 * 1024
