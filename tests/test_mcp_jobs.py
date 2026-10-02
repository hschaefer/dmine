# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""Job-manager semantics for the MCP layer (async continuation of long
captures/backfills so short client request timeouts never abort them).

All browser work is mocked; only the job/lock/wait logic is exercised.
"""
import threading
import time

import pytest

from dmine import capture as capture_mod
from dmine import mcp_server as srv


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("DMINE_STATE", str(tmp_path))
    with srv._jobs_guard:
        srv._jobs.clear()
    yield
    # let any straggler daemon threads finish on their (already removed) jobs
    for _ in range(50):
        with srv._jobs_guard:
            running = [j for j in srv._jobs.values() if not j.done.is_set()]
        if not running:
            break
        time.sleep(0.05)


def _wait_done(cid: str, kind: str = "capture", timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with srv._jobs_guard:
            j = srv._jobs.get((kind, cid))
            if j and j.done.is_set():
                return
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


def test_fast_capture_returns_result_synchronously(monkeypatch):
    sentinel = {"new_messages": 3, "seen": 3, "stop_reason": "limit reached", "total_in_db": 10}
    calls = []

    def fake(cid, **kw):
        calls.append((cid, kw))
        return dict(sentinel)

    monkeypatch.setattr(capture_mod, "do_capture", fake)
    res = srv.submit_capture("111", {"full": True, "limit": 5}, wait_s=5.0)
    assert res == sentinel
    assert calls == [("111", {"full": True, "since": None, "limit": 5,
                              "no_media": False, "refresh": False, "lock_ok": True})]


def test_long_capture_returns_running_then_result(monkeypatch):
    calls = []

    def fake(cid, **kw):
        calls.append(cid)
        time.sleep(0.5)
        return {"new_messages": 1, "seen": 1, "stop_reason": "watermark reached", "total_in_db": 9}

    monkeypatch.setattr(capture_mod, "do_capture", fake)
    first = srv.submit_capture("222", {}, wait_s=0.1)
    assert first["status"] == "running"
    assert first["job"]["channel_id"] == "222"
    assert "running_s" in first["job"]
    assert srv._active_jobs()  # visible in status()

    second = srv.submit_capture("222", {}, wait_s=5.0)
    assert second["new_messages"] == 1
    assert len(calls) == 1  # second call joined, did not re-run
    assert not srv._active_jobs()


def test_parallel_same_channel_deduped(monkeypatch):
    calls = []

    def fake(cid, **kw):
        calls.append(cid)
        time.sleep(0.4)
        return {"new_messages": 0, "seen": 0, "stop_reason": "x", "total_in_db": 0}

    monkeypatch.setattr(capture_mod, "do_capture", fake)
    results = [None, None]

    def caller(i):
        results[i] = srv.submit_capture("333", {}, wait_s=0.1)

    t1 = threading.Thread(target=caller, args=(0,))
    t2 = threading.Thread(target=caller, args=(1,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert results[0] is not None and results[1] is not None
    assert results[0]["status"] == "running" and results[1]["status"] == "running"
    assert len(calls) == 1
    final = srv.submit_capture("333", {}, wait_s=5.0)
    assert final.get("status") not in ("running", "error")
    assert len(calls) == 1


def test_lock_busy_queues_and_serializes(monkeypatch):
    calls = []

    def fake(cid, **kw):
        calls.append(cid)
        return {"new_messages": 2, "seen": 2, "stop_reason": "ok", "total_in_db": 2}

    monkeypatch.setattr(capture_mod, "do_capture", fake)
    lp = capture_mod.acquire_capture_lock()  # someone else is capturing
    try:
        first = srv.submit_capture("444", {}, wait_s=0.2, lock_wait_s=30.0)
        assert first["status"] == "running"
        assert first["job"]["waiting_for_lock"] is True
        assert len(calls) == 0  # not started yet — queued
    finally:
        capture_mod.release_capture_lock(lp)

    final = srv.submit_capture("444", {}, wait_s=5.0, lock_wait_s=30.0)
    assert final["new_messages"] == 2
    assert len(calls) == 1


def test_lock_timeout_gives_clear_error(monkeypatch):
    calls = []

    def fake(cid, **kw):
        calls.append(cid)
        return {}

    monkeypatch.setattr(capture_mod, "do_capture", fake)
    lp = capture_mod.acquire_capture_lock()
    try:
        res = srv.submit_capture("555", {}, wait_s=5.0, lock_wait_s=0.3)
    finally:
        capture_mod.release_capture_lock(lp)
    assert res["status"] == "error"
    assert "capture.lock" in res["error"]
    assert "not free" in res["error"]
    assert calls == []


def test_error_propagates(monkeypatch):
    def fake(cid, **kw):
        raise RuntimeError("Login lost")

    monkeypatch.setattr(capture_mod, "do_capture", fake)
    res = srv.submit_capture("666", {}, wait_s=5.0)
    assert res == {"status": "error", "error": "Login lost"}


def test_remedia_sync_and_running(monkeypatch):
    calls = []

    def fake(cid, limit=None):
        calls.append((cid, limit))
        time.sleep(0.4)
        return {"channel_id": cid, "messages_with_media": 0, "media_entries": 0}

    monkeypatch.setattr(capture_mod, "do_remedia", fake)
    first = srv.submit_remedia("777", {"limit": 3}, wait_s=0.1)
    assert first["status"] == "running"
    second = srv.submit_remedia("777", {"limit": 3}, wait_s=5.0)
    assert second["media_entries"] == 0
    assert len(calls) == 1
