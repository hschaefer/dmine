# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""Lock semantics: mutual exclusion, stale-lock reclaim, release."""
import os

import pytest

from dmine import capture
from dmine.util import lock_path


def test_acquire_blocks_second(monkeypatch, tmp_path):
    monkeypatch.setenv("DMINE_STATE", str(tmp_path))
    lp = capture.acquire_capture_lock()
    try:
        with pytest.raises(RuntimeError, match="Another capture/backfill is running"):
            capture.acquire_capture_lock()
    finally:
        capture.release_capture_lock(lp)
    # after release it is free again
    capture.acquire_capture_lock()
    capture.release_capture_lock(lock_path())


def test_stale_lock_reclaimed(monkeypatch, tmp_path):
    monkeypatch.setenv("DMINE_STATE", str(tmp_path))
    lp = lock_path()
    lp.write_text("99999999")  # dead PID
    got = capture.acquire_capture_lock()
    assert got == lp
    capture.release_capture_lock(lp)


def test_live_lock_blocks(monkeypatch, tmp_path):
    monkeypatch.setenv("DMINE_STATE", str(tmp_path))
    lock_path().write_text(str(os.getpid()))
    try:
        with pytest.raises(RuntimeError, match="Another capture/backfill is running"):
            capture.acquire_capture_lock()
    finally:
        lock_path().unlink(missing_ok=True)
