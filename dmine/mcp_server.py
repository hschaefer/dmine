# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""MCP server for dmine.

Thin adapter over the dmine core so any MCP-capable client
can archive, search, and read Discord channel data. The CLI stays the
shared core; this only exposes it as structured tools.

Run:  python -m dmine.mcp_server   (stdio transport)
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import capture as capture_mod
from . import storage
from .util import media_root_for, norm_channel_id, render_export

mcp = FastMCP(
    "dmine",
    instructions=(
        "Read and capture a local Discord archive (SQLite) that was collected from "
        "a logged-in browser session. Read tools work against the archive alone and "
        "need no browser or network: recent, search, status, servers, channels, "
        "media, export. capture and remedia drive a real browser, are serialized by "
        "a lock, can run for a long time (they return a 'running' job summary rather "
        "than blocking), and require an authenticated session created first with the "
        "CLI (dmine login, or dmine browser start). Channel arguments "
        "accept an ID or a discord.com URL; the server argument accepts an ID or a "
        "name."
    ),
)

_EXPORT_DIR = Path(os.environ.get("DMINE_EXPORT_DIR", Path.home() / ".config" / "dmine" / "exports"))

# Default upper bound for how long a capture job waits for the global
# capture.lock (held by any other capture/backfill, e.g. the night
# archive-server run) before giving up with a clear error.
_LOCK_WAIT_S = 7200.0
# Finished jobs are kept this long so a follow-up call for the same channel
# can return the stored result instead of re-running.
_JOB_TTL_S = 300.0


# ------------------------------------------------------- job manager ------
# Long-running browser work (capture/backfill, remedia) runs in a background
# thread so the MCP request never outlives short client-side timeouts
# (many MCP clients abort tool calls after ~60 s). The tool call
# waits up to `wait` seconds for the job; if it is still running it returns
# a "running" summary immediately and the job continues server-side.
# Capture jobs additionally queue on the global capture.lock (bounded by
# _LOCK_WAIT_S) instead of failing instantly, so firing several channel
# backfills in parallel serializes automatically.
class _Job:
    __slots__ = ("kind", "channel_id", "started", "done", "result", "error", "waiting_lock")
    kind: str
    channel_id: str
    started: float
    done: threading.Event
    result: dict | None
    error: str | None
    waiting_lock: bool

    def __init__(self, kind: str, channel_id: str):
        self.kind = kind
        self.channel_id = channel_id
        self.started = time.time()
        self.done = threading.Event()
        self.result = None
        self.error = None
        self.waiting_lock = False


_jobs: dict[tuple[str, str], _Job] = {}
_jobs_guard = threading.Lock()


def _active_jobs() -> list[dict]:
    with _jobs_guard:
        return [
            {
                "kind": j.kind,
                "channel_id": j.channel_id,
                "running_s": round(time.time() - j.started, 1),
                "waiting_for_lock": j.waiting_lock,
            }
            for j in _jobs.values()
            if not j.done.is_set()
        ]


def _prune_jobs() -> None:
    now = time.time()
    stale = [k for k, j in _jobs.items() if j.done.is_set() and now - j.started > _JOB_TTL_S]
    for k in stale:
        _jobs.pop(k, None)


def _run_capture_job(cid: str, kwargs: dict, lock_wait_s: float) -> _Job:
    key = ("capture", cid)
    with _jobs_guard:
        _prune_jobs()
        job = _jobs.get(key)
        if job is None or job.done.is_set():
            job = _Job("capture", cid)
            _jobs[key] = job
            threading.Thread(
                target=_capture_worker, args=(job, cid, kwargs, lock_wait_s), daemon=True
            ).start()
    return job


def _capture_worker(job: _Job, cid: str, kwargs: dict, lock_wait_s: float) -> None:
    lp = None
    try:
        deadline = time.time() + lock_wait_s
        while True:
            try:
                lp = capture_mod.acquire_capture_lock()
                break
            except RuntimeError:
                if time.time() >= deadline:
                    job.error = (
                        f"capture.lock not free after {int(lock_wait_s)}s "
                        "(another capture/backfill is active) — run aborted, try again later."
                    )
                    return
                job.waiting_lock = True
                time.sleep(3)
        try:
            job.result = capture_mod.do_capture(
                cid,
                full=kwargs.get("full", False),
                since=kwargs.get("since"),
                limit=kwargs.get("limit"),
                no_media=kwargs.get("no_media", False),
                refresh=kwargs.get("refresh", False),
                lock_ok=True,
            )
        finally:
            capture_mod.release_capture_lock(lp)
    except Exception as e:  # noqa: BLE001
        job.error = str(e)
    finally:
        job.done.set()


def _run_remedia_job(cid: str, kwargs: dict) -> _Job:
    key = ("remedia", cid)
    with _jobs_guard:
        _prune_jobs()
        job = _jobs.get(key)
        if job is None or job.done.is_set():
            job = _Job("remedia", cid)
            _jobs[key] = job
            threading.Thread(target=_remedia_worker, args=(job, cid, kwargs), daemon=True).start()
    return job


def _remedia_worker(job: _Job, cid: str, kwargs: dict) -> None:
    try:
        job.result = capture_mod.do_remedia(cid, limit=kwargs.get("limit"))
    except Exception as e:  # noqa: BLE001
        job.error = str(e)
    finally:
        job.done.set()


def _running_summary(job: _Job) -> dict:
    return {
        "status": "running",
        "job": {
            "kind": job.kind,
            "channel_id": job.channel_id,
            "running_s": round(time.time() - job.started, 1),
            "waiting_for_lock": job.waiting_lock,
        },
        "note": (
            "The capture/backfill keeps running in the background (the MCP request "
            "timeout is deliberately not awaited). Calling capture() again for the "
            "same channel returns the final result; progress is also visible via "
            "status()/recent()."
        ),
    }


def submit_capture(cid: str, kwargs: dict, wait_s: float, lock_wait_s: float = _LOCK_WAIT_S) -> dict:
    """Start (or join) a capture job for one channel; wait up to wait_s.

    Returns the full capture result if it finished in time (sync behaviour),
    otherwise a "running" summary — the job continues in the background.
    """
    job = _run_capture_job(cid, kwargs, lock_wait_s)
    job.done.wait(wait_s)
    if job.done.is_set():
        if job.error:
            return {"status": "error", "error": job.error}
        assert job.result is not None
        return job.result
    return _running_summary(job)


def submit_remedia(cid: str, kwargs: dict, wait_s: float) -> dict:
    job = _run_remedia_job(cid, kwargs)
    job.done.wait(wait_s)
    if job.done.is_set():
        if job.error:
            return {"status": "error", "error": job.error}
        assert job.result is not None
        return job.result
    return _running_summary(job)


def _trim(text: str, limit: int = 600) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _norm_attachment(a) -> dict:
    """Normalize an attachment/image entry: pre-download strings or post-download dicts."""
    if isinstance(a, str):
        return {"filename": a.split("?", 1)[0].rsplit("/", 1)[-1], "url": a, "local_path": None}
    return {
        "filename": a.get("filename"),
        "url": a.get("url"),
        "local_path": a.get("local_path"),
    }


def _summarize(msg: dict, include_embeds: bool = True) -> dict:
    out = {
        "id": msg["id"],
        "ts": msg.get("ts"),
        "author": msg.get("author_name"),
        "content": msg.get("content", ""),
    }
    if include_embeds:
        embeds = []
        for e in msg.get("embeds", []):
            if not isinstance(e, dict):
                continue
            images = [_norm_attachment(i) for i in (e.get("images") or [])]
            embeds.append({"text": _trim(e.get("text", "")), "url": e.get("url", ""), "images": images})
        out["embeds"] = embeds
    atts = [_norm_attachment(a) for a in (msg.get("attachments") or [])]
    if atts:
        out["attachments"] = atts
    return out


@mcp.tool()
async def capture(
    channel: str,
    full: bool = False,
    since: str | None = None,
    limit: int | None = None,
    no_media: bool = False,
    refresh: bool = False,
    wait: float = 45.0,
) -> dict:
    """Archive a Discord channel into the SQLite archive.

    Incremental by default: only messages newer than the stored watermark
    (MAX message id per channel) are fetched. Use full=True for a one-time
    backfill. Requires the persistent Chrome daemon (auto-starts; the user
    must have logged into Discord in its profile at least once).

    Long jobs run in the background: the call waits up to `wait` seconds
    (default 45) and returns the full result if the capture finished in
    time. If it is still running, it returns {"status": "running", ...}
    immediately and the capture continues — call capture() again for the
    same channel to fetch the final result, or watch progress via
    status()/recent(). Never fire captures for several channels in
    parallel: they queue on the global capture lock and serialize
    automatically (capture() reports waiting_for_lock while queued).

    Args:
        channel: channel ID or full discord.com/channels/... URL.
        full: backfill the entire history instead of incrementing.
        since: explicit watermark message ID (overrides the stored one).
        limit: hard cap on messages (test runs).
        no_media: skip attachment + embed-image downloads (CDN links expire!).
        refresh: re-extract ALL visible messages, retrofitting embed images
                 onto rows captured before image extraction existed (combine
                 with full=True for the whole channel).
        wait: seconds to wait synchronously for completion before returning
              a "running" summary (keep below your client's request
              timeout (many clients cap it at ~60 s).
    Returns:
        Dict with new_messages, seen, stop_reason, total_in_db — or a
        {"status": "running", "job": {...}} summary for long backfills.
    """
    cid = norm_channel_id(channel)
    # The capture core drives the browser via Playwright's SYNC API, which
    # refuses to run inside an asyncio loop (FastMCP executes tool calls
    # there). Run it in a worker thread so sync Playwright works in every
    # MCP client.
    return await asyncio.to_thread(
        submit_capture, cid,
        {"full": full, "since": since, "limit": limit, "no_media": no_media, "refresh": refresh},
        wait,
    )


@mcp.tool()
def recent(
    channel: str,
    since: str | None = None,
    limit: int = 100,
    include_embeds: bool = True,
) -> dict:
    """Read the most recent archived messages of a channel (no browser needed).

    Args:
        channel: channel ID or full URL.
        since: only messages with id > this message ID (e.g. the watermark
               from status() before a capture, to see what's new).
        limit: max messages (default 100).
        include_embeds: include embed text (tweet payloads live there).
    Returns:
        Dict: {"messages": [ {id, ts, author, content, embeds, attachments}, ... ]}.
    """
    cid = norm_channel_id(channel)
    with storage.Archive() as arch:
        return {"messages": [_summarize(m, include_embeds) for m in arch.messages_since(cid, since_id=since, limit=limit, desc=True)]}


@mcp.tool()
def search(query: str, server: str | None = None, channel: str | None = None, limit: int = 50) -> dict:
    """Full-text search over the archive (message content, embeds, author).

    Without scope this spans every archived server. Use server= or channel=
    to restrict; every hit carries its server_id/server_name.

    Args:
        query: search term (substring match; % and _ match literally).
        server: optional server ID or name (case-insensitive, unique
                substring ok) to restrict to.
        channel: optional channel ID/URL to restrict to (applies within
                 the server scope, if any).
        limit: max hits (default 50).
    Returns:
        Dict: {"hits": [ {id, server_id, server_name, channel_id, author_name,
                          ts, content}, ... ]}.
    """
    with storage.Archive() as arch:
        sid = arch.resolve_server_id(server) if server else None
        cid = norm_channel_id(channel) if channel else None
        return {"hits": [dict(r) for r in arch.search(query, channel_id=cid, server_id=sid, limit=limit)]}


@mcp.tool()
def status(channel: str | None = None) -> dict:
    """Archive status: total messages, per-channel watermark and counts.

    Args:
        channel: optional channel ID/URL to show only that channel.
    Returns:
        Dict with totals, per-channel rows (id, name, msgs, watermark) and
        active_captures (background capture/backfill jobs currently
        running or queued on the capture lock, if any).
    """
    with storage.Archive() as arch:
        chans = arch.channels()
        if channel:
            cid = norm_channel_id(channel)
            chans = [c for c in chans if c["id"] == cid]
        rows = [
            {"id": c["id"], "name": c["name"], "server_id": c["server_id"],
             "msgs": arch.count(c["id"]), "watermark": arch.watermark(c["id"])}
            for c in chans
        ]
        total = arch.count()
        active = _active_jobs()
    return {"total_messages": total, "channels": rows, "active_captures": active}


@mcp.tool()
def servers() -> dict:
    """List Discord servers known to the archive (id, name, channels, messages)."""
    with storage.Archive() as arch:
        out = []
        for s in arch.servers():
            chans = arch.channels(server_id=s["id"])
            out.append({
                "id": s["id"], "name": s["name"],
                "channels": len(chans), "messages": sum(arch.count(c["id"]) for c in chans),
            })
        return {"servers": out}


@mcp.tool()
def channels(server: str | None = None) -> dict:
    """List channels known to the archive (optionally of one server).

    Args:
        server: optional server ID or name to restrict to.
    Returns:
        Dict: {"channels": [ {id, server_id, server_name, name,
                              last_message_id}, ... ]}.
    """
    with storage.Archive() as arch:
        sid = arch.resolve_server_id(server) if server else None
        return {"channels": [dict(c) for c in arch.channels(server_id=sid)]}


@mcp.tool()
def media(channel: str, limit: int = 200, message_since: str | None = None) -> dict:
    """List attachments AND embed images archived for a channel.

    Downloaded files live under DMINE_MEDIA (default
    ~/.config/dmine/media/<channel_id>/, embed images in
    <channel>/embeds/). Entries without local_path only carry the CDN URL,
    which may already be expired. Use remedia() to (re-)download while the
    links are valid.

    Args:
        channel: channel ID or full URL.
        limit: max messages to scan (default 200).
        message_since: only messages with id > this message ID.
    Returns:
        Dict: {"media_dir": str, "count": int, "attachments": [ ... ]}
        with per-entry kind ("attachment"|"embed_image"), message_id, ts,
        author, filename, downloaded, local_path, url.
    """
    cid = norm_channel_id(channel)
    atts = []

    def push(kind, message_id, ts, author, a):
        norm = _norm_attachment(a)
        lp = norm.get("local_path")
        exists = bool(lp and Path(lp).exists())
        atts.append(
            {
                "kind": kind,
                "message_id": message_id,
                "ts": ts,
                "author": author,
                "filename": norm.get("filename"),
                "downloaded": exists,
                "local_path": lp if exists else None,
                "url": norm.get("url"),
            }
        )

    with storage.Archive() as arch:
        for m in arch.messages_since(cid, since_id=message_since, limit=limit, desc=True):
            for a in m.get("attachments") or []:
                push("attachment", m["id"], m.get("ts"), m.get("author_name"), a)
            for e in m.get("embeds") or []:
                if not isinstance(e, dict):
                    continue
                for im in e.get("images") or []:
                    push("embed_image", m["id"], m.get("ts"), m.get("author_name"), im)
    return {"media_dir": str(media_root_for(cid)), "count": len(atts), "attachments": atts}


@mcp.tool()
async def remedia(channel: str, limit: int | None = None, wait: float = 45.0) -> dict:
    """Re-download media (attachments + embed images) for ALREADY archived
    messages and persist local paths (idempotent, skips existing files).
    Also retries entries whose earlier download failed (no local file).

    Like capture(), long runs continue in the background: the call returns
    a {"status": "running", ...} summary after `wait` seconds at the latest
    and the download continues — call remedia() again for the same channel
    to fetch the final result.

    Args:
        channel: channel ID or full URL.
        limit: only the N most recent messages (None = all).
        wait: seconds to wait synchronously before returning a running summary.
    Returns:
        Dict: {"channel_id", "messages_with_media", "media_entries"}.
    """
    cid = norm_channel_id(channel)
    # Same as capture: sync Playwright core must run off the asyncio loop.
    return await asyncio.to_thread(submit_remedia, cid, {"limit": limit}, wait)


@mcp.tool()
def export(channel: str, format: str = "jsonl", since: str | None = None, out: str | None = None) -> dict:
    """Export a channel from the archive to a file (jsonl or markdown).

    The output path is sandboxed to the export directory
    (DMINE_EXPORT_DIR, default ~/.config/dmine/exports/) —
    absolute paths and '..' traversal are rejected.

    Args:
        channel: channel ID or full URL.
        format: "jsonl" or "md".
        since: only messages with id > this message ID.
        out: filename relative to the export dir (default <channel>.<format>).
    Returns:
        Dict with path, message_count.
    """
    cid = norm_channel_id(channel)
    _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    if out:
        p = Path(out)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"out must be a filename inside {_EXPORT_DIR} (no absolute paths, no '..')")
        target = _EXPORT_DIR / p
    else:
        target = _EXPORT_DIR / f"{cid}.{format}"
    with storage.Archive() as arch:
        msgs = arch.messages_since(cid, since_id=since)
        target.write_text(render_export(msgs, format), encoding="utf-8")
    return {"path": str(target), "message_count": len(msgs)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
