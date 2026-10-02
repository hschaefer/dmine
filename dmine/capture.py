# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""Capture loop + shared capture flow for dmine.

One browser tab drives all captures, so a single non-blocking lock file
(capture.lock, O_EXCL + PID liveness check) serializes every browser-
driving operation. Read-only DB access is never locked.
"""
from __future__ import annotations

import os
import random
from pathlib import Path

from playwright.sync_api import Page

from . import extract, media, storage
from .util import lock_path, media_root_for

_LOGIN_WAIT_HEADLESS_S = 60
_LOGIN_WAIT_INTERACTIVE_S = 1800


# ------------------------------------------------------------- lock ------
def acquire_capture_lock() -> Path:
    """Take the capture lock (O_EXCL). Reclaims stale locks (dead PID).

    Raises RuntimeError if another capture/backfill is running.
    """
    lp = lock_path()
    lp.parent.mkdir(parents=True, exist_ok=True)
    import errno

    for attempt in (0, 1):
        try:
            fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lp
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
            if attempt == 1:
                raise RuntimeError(
                    "Another capture/backfill is running (capture.lock). "
                    "Wait until it finishes — read-only DB access keeps working."
                )
            # stale check: PID alive?
            try:
                pid = int(Path(lp).read_text().strip() or "0")
                os.kill(pid, 0)
                raise RuntimeError(
                    "Another capture/backfill is running (capture.lock). "
                    "Wait until it finishes — read-only DB access keeps working."
                )
            except (ValueError, ProcessLookupError):
                # dead PID (or garbage) → stale lock, reclaim
                Path(lp).unlink(missing_ok=True)
    raise RuntimeError("capture lock could not be acquired")  # pragma: no cover


def release_capture_lock(lp: Path) -> None:
    try:
        lp.unlink(missing_ok=True)
    except OSError:
        pass


# ------------------------------------------------------------- capture ---
def do_capture(
    channel_id: str,
    server_id: str | None = None,
    channel_name: str = "",
    full: bool = False,
    since: str | None = None,
    limit: int | None = None,
    no_media: bool = False,
    headless: bool = False,
    refresh: bool = False,
    lock_ok: bool = False,
    progress=None,
) -> dict:
    """Shared capture flow used by the CLI and the MCP server.

    Incremental by default (since the stored watermark); --full or an
    explicit `since` overrides. refresh=True re-extracts every visible
    message (retrofits embed images onto existing rows). Returns a
    JSON-able summary dict. Serialized by the capture lock.
    """
    progress = progress or (lambda *a, **k: None)
    lp = None
    if not lock_ok:
        lp = acquire_capture_lock()
    try:
        with storage.Archive() as arch:
            if server_id is None:
                # bare channel ID: resolve the server from the archive
                row = arch.conn.execute(
                    "SELECT server_id FROM channels WHERE id=?", (channel_id,)
                ).fetchone()
                server_id = row[0] if row else None
            if server_id is None:
                raise RuntimeError(
                    f"Channel {channel_id} is unknown to the DB (no server assigned). "
                    "Run 'dmine scan' first or pass the full channel URL."
                )
            since_id = None if full else (since if since is not None else arch.watermark(channel_id))
            if since_id is not None:
                progress(f"# incremental: watermark = {since_id}", flush=True)
            else:
                progress("# no watermark yet -> full backfill", flush=True)

            from .browser import DiscordBrowser

            wait = _LOGIN_WAIT_HEADLESS_S if (headless or not os.environ.get("DISPLAY")) else _LOGIN_WAIT_INTERACTIVE_S
            b = DiscordBrowser(headless=headless).start()
            try:
                b.ensure_logged_in(timeout_s=wait)
                target = f"https://discord.com/channels/{server_id}/{channel_id}"
                b.goto(target)
                b.page.wait_for_timeout(2500)
                # guard: never let a failed navigation clobber stored metadata
                if f"/channels/{server_id}/" not in (b.page.url or ""):
                    raise RuntimeError(
                        f"Navigation to {target} failed (now at {b.page.url}) — "
                        "check access to the channel (private channel? membership?)"
                    )
                name = extract.channel_header_name(b.page) or channel_name
                if name:
                    arch.upsert_channel(channel_id, server_id, name)
                    progress(f"# channel: {name} ({channel_id})", flush=True)
                result = run_capture(
                    b.page,
                    arch,
                    channel_id,
                    server_id=server_id,
                    channel_name=name,
                    since_id=since_id,
                    limit=limit,
                    download_media=not no_media,
                    refresh=refresh,
                    progress=progress,
                )
            finally:
                b.stop()
            return {**result, "channel_id": channel_id, "channel_name": name, "total_in_db": arch.count(channel_id)}
    finally:
        if lp:
            release_capture_lock(lp)


# -------------------------------------------------------- archive-server --
def do_archive_server(
    server_id: str,
    progress=None,
) -> dict:
    """Full backfill of every archived channel of a server, sequentially.

    Holds the capture lock for the whole run. On login loss (TimeoutError)
    the run aborts instead of burning the wait timeout per channel.
    """
    progress = progress or (lambda *a, **k: None)
    lp = acquire_capture_lock()
    try:
        with storage.Archive() as arch:
            chans = [dict(r) for r in arch.conn.execute(
                "SELECT id, name FROM channels WHERE server_id=? ORDER BY CAST(id AS INTEGER)", (server_id,)
            )]
        progress(f"# archive-server {server_id}: {len(chans)} channels", flush=True)
        results = []
        for ch in chans:
            try:
                res = do_capture(ch["id"], server_id=server_id, full=True, lock_ok=True, progress=progress)
                row = {"channel": ch["id"], "name": ch.get("name"), "new": res["new_messages"],
                       "seen": res["seen"], "stop": res["stop_reason"]}
                progress(f"  OK  {ch.get('name') or ch['id']}: {res['new_messages']} new ({res['stop_reason']})", flush=True)
            except TimeoutError:
                progress(f"  ABORT login lost — run aborted (channel {ch['id']})", flush=True)
                results.append({"channel": ch["id"], "name": ch.get("name"), "error": "login lost"})
                break
            except Exception as e:  # noqa: BLE001
                row = {"channel": ch["id"], "name": ch.get("name"), "error": str(e)}
                progress(f"  ERR {ch.get('name') or ch['id']}: {e}", flush=True)
                results.append(row)
                continue
            results.append(row)
        return {"server_id": server_id, "channels": len(chans), "results": results}
    finally:
        release_capture_lock(lp)


# ------------------------------------------------------------- capture ---
def run_capture(
    page: Page,
    archive: storage.Archive,
    channel_id: str,
    server_id: str | None = None,
    channel_name: str = "",
    since_id: str | None = None,
    limit: int | None = None,
    download_media: bool = True,
    media_dir: str | Path | None = None,
    refresh: bool = False,
    progress=None,
) -> dict:
    """Scroll a channel and persist everything rendered.

    Stop conditions (convergence-based, robust against deleted watermarks):
      * no growth of the seen-set for N consecutive rounds → reached the top
      * incremental: the watermark message itself becomes visible
      * --limit: hard cap
    """
    progress = progress or (lambda *a, **k: None)
    extract.wait_for_chat_list(page)

    seen: set[str] = set()
    stable_rounds = 0
    total_new = 0
    media_root = Path(media_dir) if media_dir else media_root_for(channel_id)
    stop_reason = ""

    while True:
        msgs = extract.extract_visible_messages(page, channel_id)
        prev_seen = len(seen)
        if not msgs:
            # empty render window (channel empty or list not loaded): count as no growth
            if prev_seen == len(seen):
                stable_rounds += 1
            if stable_rounds >= 4:
                stop_reason = "no messages rendered (channel empty?)"
                break
            extract.scroll_up(page, jitter_ms=500)
            continue

        fresh = [m for m in msgs if m["id"] not in seen]
        seen.update(m["id"] for m in msgs)

        if since_id is not None:
            # incremental: keep only newer-than-watermark, stop when watermark visible
            before_count = len(fresh)
            fresh = [m for m in fresh if int(m["id"]) > int(since_id)]
            if len(fresh) < before_count or any(int(m["id"]) <= int(since_id) for m in msgs):
                stop_reason = f"watermark {since_id} reached"
                work = msgs if refresh else fresh
                if work:
                    total_new += _store(archive, work, download_media, media_root, refresh)
                break

        if limit is not None and len(seen) >= limit:
            stop_reason = f"limit {limit} reached"
            work = msgs if refresh else fresh
            if work:
                total_new += _store(archive, work, download_media, media_root, refresh)
            break

        work = msgs if refresh else fresh
        if work:
            total_new += _store(archive, work, download_media, media_root, refresh)

        # termination: stop when nothing NEW appears for several rounds
        if len(seen) > prev_seen:
            stable_rounds = 0
        else:
            stable_rounds += 1
            # Discord virtualizes long message lists: while scrolling up, the
            # client can pause loading older history for a few seconds. Wait
            # with a backoff before counting the round as "dead", otherwise
            # throttling on big channels false-triggers "reached the top".
            page.wait_for_timeout(min(1000 * (2 ** min(stable_rounds, 3)), 8000))

        if server_id and channel_name:
            archive.upsert_channel(channel_id, server_id, channel_name)

        progress(
            f"captured {total_new} new / {len(seen)} seen (min id {min(seen, key=int)})  [{stop_reason or 'scrolling…'}]",
            flush=True,
        )

        if stable_rounds >= 10:
            stop_reason = "no more messages loading (reached the top)"
            break

        extract.scroll_up(page, jitter_ms=random.randint(320, 520))

    if server_id and channel_name:
        archive.upsert_channel(channel_id, server_id, channel_name)

    return {"new_messages": total_new, "seen": len(seen), "stop_reason": stop_reason}


def _store(archive: storage.Archive, msgs: list[dict], download_media: bool, media_root: Path, refresh: bool) -> int:
    """Ordered media pipeline for one batch. Returns new-row count.

    Order matters: upsert FIRST (rows must exist before _persist_media
    UPDATEs local paths), then refresh-overwrite embeds, then download,
    then persist the downloaded paths back into the DB.
    """
    new = archive.upsert_messages(msgs)
    if refresh:
        _apply_refresh(archive, msgs)
    if download_media:
        _grab_media(msgs, media_root)
        _persist_media(archive, msgs)
    return new


def _apply_refresh(arch: storage.Archive, msgs: list[dict]) -> None:
    """Refresh mode: overwrite embeds (with image URLs) for already-stored rows."""
    for m in msgs:
        if m.get("embeds"):
            arch.set_json("embeds", m["id"], m["embeds"])


def _grab_media(msgs: list[dict], media_root: Path) -> None:
    """Download attachments + embed images for the given messages (in place)."""
    for m in msgs:
        if m.get("attachments"):
            m["attachments"] = media.download_attachments(m["attachments"], media_root, m["id"])
        for e in m.get("embeds", []):
            if e.get("images"):
                e["images"] = media.download_attachments(e["images"], media_root / "embeds", f"emb_{m['id']}")


def _persist_media(arch: storage.Archive, msgs: list[dict]) -> None:
    """Write downloaded local paths back into the DB (media JSON columns)."""
    for m in msgs:
        if m.get("attachments"):
            arch.set_json("attachments", m["id"], m["attachments"])
        embeds = m.get("embeds")
        if embeds and any(e.get("images") for e in embeds):
            arch.set_json("embeds", m["id"], embeds)


def do_remedia(
    channel_id: str,
    limit: int | None = None,
    progress=None,
) -> dict:
    """Re-download media (attachments + embed images) for ALREADY archived
    messages and persist the local paths. Retries entries that failed
    before (dict with url but no local_path) and plain URL strings.

    Idempotent: files that already exist locally are skipped.
    """
    progress = progress or (lambda *a, **k: None)
    with storage.Archive() as arch:
        media_root = media_root_for(channel_id)
        rows = arch.messages_since(channel_id, limit=limit, desc=True)
        msgs = []
        for r in rows:
            m = {"id": r["id"], "attachments": [], "embeds": []}
            for a in r.get("attachments", []):
                u = _entry_url(a)
                if u:
                    m["attachments"].append(u)
            for e in r.get("embeds", []):
                if isinstance(e, dict):
                    imgs = [u for i in e.get("images", []) if (u := _entry_url(i))]
                    if imgs:
                        m["embeds"].append({"images": imgs})
            if m["attachments"] or any(e["images"] for e in m["embeds"]):
                msgs.append(m)
        progress(f"# {len(msgs)} messages with media out of {len(rows)} rows", flush=True)
        _grab_media(msgs, media_root)
        _persist_media(arch, msgs)
        files = sum(
            len(m.get("attachments", [])) + sum(len(e.get("images", [])) for e in m.get("embeds", []))
            for m in msgs
        )
        return {"channel_id": channel_id, "messages_with_media": len(msgs), "media_entries": files}


def _entry_url(a) -> str | None:
    """URL of a stored media entry: plain string, or dict without a local file."""
    if isinstance(a, str):
        return a
    if isinstance(a, dict) and a.get("url") and not a.get("local_path"):
        return a["url"]
    return None
