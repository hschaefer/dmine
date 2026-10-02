# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""dmine — bot-free Discord channel archiver & miner.

Commands:
  scan                 list visible servers & channels (writes them to the DB)
  capture <channel>    incremental capture since watermark (--full for backfill)
  search <query>       full-text search over the archive
  export <channel>     export as jsonl or markdown
  status               watermark / message count per channel
  channels             channels known to the archive
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import __version__, capture, storage
from .browser import (
    DEFAULT_PORT,
    DEFAULT_PROFILE,
    DiscordBrowser,
    daemon_health,
    start_daemon,
    stop_daemon,
)
from .util import norm_channel_id, norm_server_id, render_export

# Discord LOCALISES the text it puts into sidebar aria-labels, so every pattern
# below has to cover each locale that can appear. A missing locale does not fail
# loudly -- it silently leaks a type/badge label through as a channel name.
#
# German/English prefixes Discord prepends to guild/channel names in the UI
_GUILD_NAME_PREFIX = re.compile(
    r"^(\d+\s*(Erwähnungen?|Mentions?)\s*|Ungelesene Nachrichten,?\s*|Unread messages,?\s*|"
    r"Neue Nachrichten,?\s*|New messages,?\s*)*",
    re.IGNORECASE,
)
# "(Textkanal)", "(Forum)", "(Text channel)" … suffixes on channel aria-labels
_CHANNEL_TYPE_SUFFIX = re.compile(r"\s*\([^)]*(kanal|channel|forum|voice|thread)[^)]*\)\s*$", re.IGNORECASE)
# skip junk anchors in the sidebar (invite buttons etc.)
_SKIP_LABEL = re.compile(r"einladen|invite", re.IGNORECASE)

# A label that is only a channel *type* -- optionally with a qualifier, as in
# "Text (begrenzt)" or "Text Channel (limited)" -- is not a channel name.
# Longest wording first so fullmatch prefers the specific one.
_CHANNEL_TYPE_WORDS = sorted(
    (
        "Text Channel", "Textkanal", "Text",
        "Voice Channel", "Voicekanal", "Voice",
        "Forum Channel", "Forum",
        "Announcement Channel", "Announcements", "Announcement", "Ankündigungen",
        "Rules", "Regeln",
    ),
    key=len,
    reverse=True,
)
_CHANNEL_TYPE_WORD = re.compile(
    "(?:" + "|".join(re.escape(w) for w in _CHANNEL_TYPE_WORDS) + r")(?:\s*\([^)]*\))?",
    re.IGNORECASE,
)
_LABEL_SPLIT = re.compile(r"[\n,]+")


def _clean_guild_name(raw: str) -> str:
    return _GUILD_NAME_PREFIX.sub("", raw or "").strip()


def _is_type_label(label: str) -> bool:
    """True when a cleaned sidebar label is a channel-type word, not a name."""
    return bool(_CHANNEL_TYPE_WORD.fullmatch(label.strip()))


def _channel_name_from_label(raw: str) -> str:
    """Pick the channel name out of one aria-label / inner-text string.

    Pure (no DOM, no browser) so the locale rules above stay unit-testable.
    """
    # aria-labels carry type/badge noise:
    #   "Text (begrenzt)\ngold\n2"
    #   "gold (Textkanal), Privater Kanal (gesperrt)"
    #   "Text Channel (limited)\ngeneral\n3"
    parts = [p.strip() for p in _LABEL_SPLIT.split(raw or "") if p.strip()]
    keep = []
    for p in parts:
        p2 = _clean_guild_name(p)
        p2 = _CHANNEL_TYPE_SUFFIX.sub("", p2).strip()
        if not p2 or p2.isdigit() or _SKIP_LABEL.search(p2) or _is_type_label(p2):
            continue
        keep.append(p2)
    return (" ".join(keep) or (parts[0] if parts else ""))[:80]


def _channel_name(el) -> str:
    raw = ""
    try:
        raw = el.get_attribute("aria-label") or ""
    except Exception:
        pass
    if not raw:
        try:
            raw = el.inner_text(timeout=500)
        except Exception:
            return ""
    return _channel_name_from_label(raw)


def _norm_channel_id(raw: str) -> str:
    return norm_channel_id(raw)


def _norm_server_id(raw: str) -> str | None:
    return norm_server_id(raw)


# ------------------------------------------------------------- browser --
def cmd_browser(args) -> int:
    if args.action == "start":
        if start_daemon():
            print(f"# dmine browser daemon running on port {DEFAULT_PORT}")
            print(f"# profile: {DEFAULT_PROFILE}")
            print("# if the window shows a login page, log in once — the session persists while the daemon runs")
        else:
            print("# could not start browser daemon", file=sys.stderr)
            return 1
    elif args.action == "status":
        print("running" if daemon_health() else "not running")
    elif args.action == "stop":
        print("# stopped" if stop_daemon() else "# nothing to stop (daemon not running)")
    return 0


# --------------------------------------------------------------- login --
def cmd_login(args) -> int:
    b = DiscordBrowser(headless=args.headless).start()
    try:
        if b.is_logged_in():
            print("# already logged in", file=sys.stderr)
            return 0
        ok = b.login_with_qr(timeout_s=args.timeout, qr_out=args.qr_out)
        return 0 if ok else 1
    finally:
        b.stop()


# ---------------------------------------------------------------- scan --
def cmd_scan(args) -> int:
    b = DiscordBrowser(headless=args.headless).start()
    try:
        b.ensure_logged_in()
        page = b.page
        page.goto("https://discord.com/channels/@me", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        arch = storage.Archive()
        guilds = page.locator('[data-list-id="guildsnav"] [role="treeitem"][data-list-item-id^="guildsnav___"]')
        n = guilds.count()
        print(f"# {n} guild entries visible (folders may hide more)", file=sys.stderr)
        for i in range(n):
            it = guilds.nth(i)
            raw = it.get_attribute("data-list-item-id") or ""
            gid = raw.split("___", 1)[-1]
            if not gid.isdigit():
                continue
            name = ""
            try:
                name = it.locator("span[class*='hiddenVisually']").first.inner_text(timeout=500).strip()
            except Exception:
                pass
            label = _clean_guild_name(name) or gid
            arch.upsert_server(gid, label)
            print(f"SERVER {gid}\t{label}")

            # open the guild, then list its text channels from the sidebar
            try:
                it.click()
                page.wait_for_timeout(2500)
            except Exception:
                continue
            chans = page.locator(f'a[href^="/channels/{gid}/"]')
            for j in range(chans.count()):
                try:
                    ch = chans.nth(j)
                    chref = ch.get_attribute("href") or ""
                    name = _channel_name(ch)
                except Exception:
                    continue
                if not name or _SKIP_LABEL.search(name):
                    continue
                parts = chref.split("/channels/", 1)[-1].split("/")
                if len(parts) < 2 or not parts[-1].isdigit():
                    continue
                cid = parts[-1]
                arch.upsert_channel(cid, gid, name)
                print(f"  CHANNEL {cid}\t{name}")
    finally:
        b.stop()
    return 0


# ------------------------------------------------------------- capture --
def cmd_capture(args) -> int:
    channel_id = _norm_channel_id(args.channel)
    server_id = _norm_server_id(args.channel) or args.server
    result = capture.do_capture(
        channel_id,
        server_id=server_id,
        full=args.full,
        since=args.since,
        limit=args.limit,
        no_media=args.no_media,
        headless=args.headless,
        refresh=args.refresh,
        progress=print,
    )
    print(json.dumps(result))
    return 0


# --------------------------------------------------------- archive-server --
def cmd_archive_server(args) -> int:
    result = capture.do_archive_server(args.server, progress=print)
    print(json.dumps(result))
    return 0


# ------------------------------------------------------------- remedia --
def cmd_remedia(args) -> int:
    channel_id = _norm_channel_id(args.channel)
    result = capture.do_remedia(channel_id, limit=args.limit, progress=print)
    print(json.dumps(result))
    return 0


# ------------------------------------------------------------- search --
def cmd_search(args) -> int:
    with storage.Archive() as arch:
        try:
            sid = arch.resolve_server_id(args.server) if args.server else None
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        rows = arch.search(args.query, channel_id=_norm_channel_id(args.channel) if args.channel else None,
                           server_id=sid, limit=args.limit)
    print(f"# {len(rows)} matches", file=sys.stderr)
    for r in rows:
        ts = (r["ts"] or "")[:16]
        srv = r["server_name"] or r["server_id"] or "?"
        print(f"{srv}  {r['channel_id']}  {ts}  {r['author_name']}: {r['content'][:200]}")
    return 0


# ------------------------------------------------------------- export --
def cmd_export(args) -> int:
    with storage.Archive() as arch:
        channel_id = _norm_channel_id(args.channel)
        msgs = arch.messages_since(channel_id, since_id=args.since)
        out = args.out or f"{channel_id}.{args.format}"
        Path(out).write_text(render_export(msgs, args.format), encoding="utf-8")
    print(f"# {len(msgs)} messages -> {out}", file=sys.stderr)
    return 0


# ------------------------------------------------------------- status --
def cmd_status(args) -> int:
    with storage.Archive() as arch:
        chans = arch.channels()
        if args.channel:
            chans = [c for c in chans if c["id"] == _norm_channel_id(args.channel)]
        print(f"# total messages in archive: {arch.count()}", file=sys.stderr)
        for c in chans:
            n = arch.count(c["id"])
            wm = arch.watermark(c["id"])
            print(f"{c['id']}\t{c['name']}\tmsgs={n}\twatermark={wm}")
    return 0


# ----------------------------------------------------------- channels --
def cmd_channels(args) -> int:
    with storage.Archive() as arch:
        for c in arch.channels():
            print(f"{c['id']}\t{c['server_id']}\t{c['name']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dmine", description=__doc__)
    ap.add_argument("--version", action="version", version=f"dmine {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("browser", help="manage the persistent browser daemon")
    p.add_argument("action", choices=["start", "status", "stop"])
    p.set_defaults(func=cmd_browser)

    p = sub.add_parser("login", help="QR-code login (scan with the Discord mobile app)")
    p.add_argument("--timeout", type=int, default=300, help="seconds to wait for the scan (default 300)")
    p.add_argument("--qr-out", dest="qr_out", help="write the QR code PNG to this path")
    p.add_argument("--headless", action="store_true")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("scan", help="list visible servers and channels")
    p.add_argument("--headless", action="store_true")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("capture", help="capture a channel (incremental unless --full)")
    p.add_argument("channel", help="channel ID or full discord.com/channels/... URL")
    p.add_argument("--server", dest="server", help="server ID (optional)")
    p.add_argument("--full", action="store_true", help="backfill from the beginning")
    p.add_argument("--since", help="explicit watermark message ID")
    p.add_argument("--limit", type=int, help="hard cap on messages (test runs)")
    p.add_argument("--no-media", action="store_true", help="skip attachment downloads")
    p.add_argument("--refresh", action="store_true", help="re-extract ALL visible messages (retrofits embed images onto existing rows); combine with --full")
    p.add_argument("--headless", action="store_true")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("search", help="full-text search over the archive")
    p.add_argument("query")
    p.add_argument("--channel", help="restrict to a channel ID/URL")
    p.add_argument("--server", help="restrict to a server (ID or name)")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("archive-server", help="full backfill of all channels of a server (long-running)")
    p.add_argument("server", help="server ID")
    p.set_defaults(func=cmd_archive_server)

    p = sub.add_parser("remedia", help="re-download media (attachments + embed images) for archived messages")
    p.add_argument("channel")
    p.add_argument("--limit", type=int, help="only the N most recent messages")
    p.set_defaults(func=cmd_remedia)

    p = sub.add_parser("export", help="export a channel as jsonl or markdown")
    p.add_argument("channel")
    p.add_argument("--format", choices=["jsonl", "md"], default="md")
    p.add_argument("--since", help="only messages after this ID")
    p.add_argument("--out")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("status", help="watermark and message counts")
    p.add_argument("--channel")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("channels", help="channels known to the archive")
    p.set_defaults(func=cmd_channels)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
