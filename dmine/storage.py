# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""SQLite storage for the dmine archive.

Single source of truth for all consumers (CLI, MCP server, library use).
Message IDs are Discord snowflakes and therefore strictly time-ordered:
MAX(id) per channel is the watermark for incremental captures.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    id   TEXT PRIMARY KEY,
    name TEXT
);
CREATE TABLE IF NOT EXISTS channels (
    id              TEXT PRIMARY KEY,
    server_id       TEXT,
    name            TEXT,
    last_message_id TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id           TEXT PRIMARY KEY,
    channel_id   TEXT NOT NULL,
    author_id    TEXT,
    author_name  TEXT,
    ts           TEXT NOT NULL,
    content      TEXT DEFAULT '',
    embeds       TEXT DEFAULT '[]',
    reactions    TEXT DEFAULT '[]',
    attachments  TEXT DEFAULT '[]',
    reply_to_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_channel_ts ON messages(channel_id, ts);
"""


def default_db_path() -> Path:
    p = Path(os.environ.get("DMINE_DB", Path.home() / ".config" / "dmine" / "archive.sqlite"))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


class Archive:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        # WAL + busy_timeout: archive writes (backfill) and reads (MCP tools)
        # run concurrently from different processes — avoid SQLITE_BUSY.
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    # context manager: long-lived processes (MCP server) must not leak fds
    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- metadata -----------------------------------------------------
    def upsert_server(self, sid: str, name: str) -> None:
        self.conn.execute(
            "INSERT INTO servers(id, name) VALUES(?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (sid, name),
        )
        self.conn.commit()

    def upsert_channel(self, cid: str, server_id: str, name: str, last_message_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO channels(id, server_id, name, last_message_id) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "server_id=excluded.server_id, name=excluded.name, "
            "last_message_id=COALESCE(excluded.last_message_id, channels.last_message_id)",
            (cid, server_id, name, last_message_id),
        )
        self.conn.commit()

    def servers(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM servers ORDER BY name").fetchall()

    def channels(self, server_id: str | None = None) -> list[sqlite3.Row]:
        # LEFT JOIN servers: every channel row carries its server name so
        # cross-server listings are self-describing.
        q = ("SELECT c.id, c.server_id, c.name, c.last_message_id, s.name AS server_name"
             " FROM channels c LEFT JOIN servers s ON s.id = c.server_id")
        args: list = []
        if server_id:
            q += " WHERE c.server_id=?"
            args.append(server_id)
        q += " ORDER BY c.name"
        return self.conn.execute(q, args).fetchall()

    def resolve_server_id(self, server: str) -> str:
        """Resolve a server ID or (case-insensitive) name to its id.

        Accepts an exact snowflake id, an exact name, or a unique name
        substring. Raises ValueError when unknown or ambiguous so agents get
        a corrective message instead of silently empty results.
        """
        s = server.strip()
        if not s:
            raise ValueError("empty server reference")
        row = self.conn.execute("SELECT id FROM servers WHERE id=?", (s,)).fetchone()
        if row:
            return row[0]
        rows = self.conn.execute(
            "SELECT id, name FROM servers WHERE name = ? COLLATE NOCASE", (s,)
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
        if not rows:  # substring fallback, only if unambiguous
            esc = s.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
            rows = self.conn.execute(
                "SELECT id, name FROM servers WHERE name LIKE ? ESCAPE '\\' ORDER BY name",
                (f"%{esc}%",),
            ).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
        if not rows:
            known = ", ".join(r["name"] for r in self.servers()) or "(no servers archived)"
            raise ValueError(f"unknown server {server!r} — archived: {known}")
        raise ValueError(
            f"server {server!r} is ambiguous (matches: {', '.join(r['name'] for r in rows)}); use the server ID"
        )

    # ---- messages -----------------------------------------------------
    def watermark(self, channel_id: str) -> str | None:
        # snowflakes are stored as TEXT; compare NUMERICALLY (18↔19 digit
        # boundary broke lexicographic MAX — wrong watermarks, re-scrolls)
        row = self.conn.execute(
            "SELECT MAX(CAST(id AS INTEGER)) FROM messages WHERE channel_id=?", (channel_id,)
        ).fetchone()
        return str(row[0]) if row and row[0] is not None else None

    def count(self, channel_id: str | None = None) -> int:
        if channel_id:
            return self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE channel_id=?", (channel_id,)
            ).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def upsert_messages(self, msgs: list[dict]) -> int:
        """Insert or ignore; returns number of newly stored messages."""
        if not msgs:
            return 0
        new = 0
        with self.conn:
            for m in msgs:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO messages"
                    "(id, channel_id, author_id, author_name, ts, content, embeds, reactions, attachments, reply_to_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        m["id"],
                        m.get("channel_id", ""),
                        m.get("author_id"),
                        m.get("author_name"),
                        m.get("ts", ""),
                        m.get("content", ""),
                        json.dumps(m.get("embeds", []), ensure_ascii=False),
                        json.dumps(m.get("reactions", []), ensure_ascii=False),
                        json.dumps(m.get("attachments", []), ensure_ascii=False),
                        m.get("reply_to_id"),
                    ),
                )
                new += cur.rowcount
        if new:
            # watermark = true numeric MAX over the whole channel (during
            # backfill the fresh batches get older, so a per-batch max regresses)
            row = self.conn.execute(
                "SELECT MAX(CAST(id AS INTEGER)) FROM messages WHERE channel_id=?", (msgs[0].get("channel_id", ""),)
            ).fetchone()
            if row and row[0]:
                self.conn.execute(
                    "UPDATE channels SET last_message_id=? WHERE id=?",
                    (row[0], msgs[0].get("channel_id", "")),
                )
            self.conn.commit()
        return new

    def messages_since(self, channel_id: str, since_id: str | None = None, limit: int | None = None, desc: bool = False) -> list[dict]:
        """Messages of a channel; chronological (asc) by default, desc=True
        for 'latest N' queries (recent/media/refresh use cases)."""
        q = ("SELECT id, author_id, author_name, ts, content, embeds, reactions, attachments, reply_to_id"
             " FROM messages WHERE channel_id=?")
        args: list = [channel_id]
        if since_id:
            q += " AND CAST(id AS INTEGER) > CAST(? AS INTEGER)"
            args.append(since_id)
        q += " ORDER BY CAST(id AS INTEGER) DESC" if desc else " ORDER BY CAST(id AS INTEGER)"
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = self.conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("embeds", "reactions", "attachments"):
                try:
                    d[k] = json.loads(d[k] or "[]")
                except json.JSONDecodeError:
                    d[k] = []
            out.append(d)
        return out

    _JSON_COLUMNS = {"attachments", "embeds"}

    def set_json(self, column: str, message_id: str, value) -> None:
        """Persist a JSON column (attachments/embeds) for one message."""
        if column not in self._JSON_COLUMNS:
            raise ValueError(f"column {column!r} is not a JSON column")
        self.conn.execute(
            f"UPDATE messages SET {column}=? WHERE id=?",
            (json.dumps(value, ensure_ascii=False), message_id),
        )
        self.conn.commit()

    def search(self, query: str, channel_id: str | None = None, server_id: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        # content + embeds (JSON text — embeds carry the payload for link-only
        # messages like tweets) + author name; LIKE wildcards are escaped so
        # user input with %/_ matches literally. LEFT JOINs channels/servers so
        # every hit carries its server identity; messages whose channel row is
        # missing (legacy/orphan rows) stay findable with NULL server info.
        escaped = query.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        like = f"%{escaped}%"
        q = ("SELECT m.id, m.channel_id, m.author_name, m.ts, m.content, "
             "c.server_id, s.name AS server_name "
             "FROM messages m "
             "LEFT JOIN channels c ON c.id = m.channel_id "
             "LEFT JOIN servers s ON s.id = c.server_id "
             "WHERE (m.content LIKE ? ESCAPE '\\' OR m.embeds LIKE ? ESCAPE '\\' "
             "OR m.author_name LIKE ? ESCAPE '\\')")
        args: list = [like, like, like]
        if channel_id:
            q += " AND m.channel_id=?"
            args.append(channel_id)
        if server_id:
            q += " AND c.server_id=?"
            args.append(server_id)
        q += " ORDER BY m.id DESC LIMIT ?"
        args.append(int(limit))
        return self.conn.execute(q, args).fetchall()
