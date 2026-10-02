# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""Capture-loop termination tests with a fake Page (no browser, no network).

Regression tests for the refresh-mode infinite loop and the watermark stop.
"""
import pytest

from dmine import capture
from dmine.storage import Archive

CHANNEL = "c1"


class FakeLocator:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    def wait_for(self, **kw):
        return None

    def hover(self, **kw):
        return None

    def count(self):
        return 0


class FakeMouse:
    def move(self, *a, **k):
        pass

    def wheel(self, *a, **k):
        pass


class FakePage:
    """Serves scripted extraction batches; converges to the same batch."""

    def __init__(self, batches, repeat_last=True):
        self.batches = list(batches)
        self.repeat_last = repeat_last
        self._n = 0
        self.mouse = FakeMouse()

    def locator(self, sel):
        return FakeLocator(self)

    def evaluate(self, js):
        if self._n < len(self.batches):
            batch = self.batches[self._n]
        elif self.repeat_last and self.batches:
            batch = self.batches[-1]
        else:
            batch = []
        self._n += 1
        return batch

    def wait_for_timeout(self, ms):
        return None


def _msg(i):
    return {"id": str(1000 + i), "channel_id": CHANNEL, "author_name": "a",
            "ts": "2026-01-01T00:00:00Z", "content": f"m{i}", "embeds": [],
            "reactions": [], "attachments": [], "reply_to_id": None}


def _batch(ids):
    return [_msg(i) for i in ids]


def test_refresh_full_terminates_at_top(tmp_path):
    """Refresh mode must stop when nothing new appears (regression: infinite loop)."""
    with Archive(tmp_path / "a.sqlite") as arch:
        page = FakePage([_batch(range(1, 11))])  # 10 msgs, then repeats forever
        res = capture.run_capture(page, arch, CHANNEL, refresh=True, progress=lambda *a, **k: None)
        assert res["stop_reason"] == "no more messages loading (reached the top)"
        assert res["new_messages"] == 10


def test_incremental_stops_at_watermark(tmp_path):
    with Archive(tmp_path / "a.sqlite") as arch:
        page = FakePage([_batch(range(1, 11))])
        res = capture.run_capture(page, arch, CHANNEL, since_id="1005", progress=lambda *a, **k: None)
        assert res["stop_reason"].startswith("watermark")
        assert res["new_messages"] == 5  # ids 1006..1010


def test_limit_honored(tmp_path):
    with Archive(tmp_path / "a.sqlite") as arch:
        page = FakePage([_batch(range(1, 11)), _batch(range(11, 21))])
        res = capture.run_capture(page, arch, CHANNEL, limit=15, progress=lambda *a, **k: None)
        assert res["stop_reason"].startswith("limit")
        assert res["seen"] >= 15


def test_empty_channel_stops(tmp_path):
    with Archive(tmp_path / "a.sqlite") as arch:
        page = FakePage([])
        res = capture.run_capture(page, arch, CHANNEL, progress=lambda *a, **k: None)
        assert "empty" in res["stop_reason"]


def test_full_backfill_reaches_top(tmp_path):
    with Archive(tmp_path / "a.sqlite") as arch:
        page = FakePage([_batch(range(1, 11)), _batch(range(1, 21))])  # grows once, then repeats
        res = capture.run_capture(page, arch, CHANNEL, progress=lambda *a, **k: None)
        assert res["stop_reason"] == "no more messages loading (reached the top)"
        assert res["new_messages"] == 20
