# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""Small shared helpers (no heavy imports — safe for the MCP server)."""
from __future__ import annotations

import os
import re
from pathlib import Path

CHANNEL_URL_RE = "discord.com/channels/"

_FILENAME_OK = re.compile(r"[^A-Za-z0-9._-]")


def norm_channel_id(raw: str) -> str:
    """Channel ID from a full URL, or the bare ID itself."""
    if CHANNEL_URL_RE in raw:
        return raw.split(CHANNEL_URL_RE, 1)[1].split("/")[-1]
    return raw


def norm_server_id(raw: str) -> str | None:
    """Server ID from a full channel URL; bare IDs return None (DB lookup)."""
    if CHANNEL_URL_RE in raw:
        parts = raw.split(CHANNEL_URL_RE, 1)[1].split("/")
        return parts[0] if parts and parts[0].isdigit() else None
    return None


def media_root_for(channel_id: str) -> Path:
    """Media directory for a channel (DMINE_MEDIA override)."""
    return Path(os.environ.get("DMINE_MEDIA", Path.home() / ".config" / "dmine" / "media")) / channel_id


def sanitize_filename(name: str, fallback: str = "file") -> str:
    """Keep only safe filename characters; never allow traversal."""
    name = _FILENAME_OK.sub("_", name or "").strip("._ ")
    return name[:120] or fallback


def lock_path() -> Path:
    state_dir = Path(os.environ.get("DMINE_STATE", Path.home() / ".config" / "dmine"))
    return state_dir / "capture.lock"


def render_export(msgs: list[dict], fmt: str) -> str:
    """Shared export rendering (jsonl or markdown) for CLI + MCP server."""
    import json

    if fmt == "jsonl":
        return "\n".join(json.dumps(m, ensure_ascii=False) for m in msgs)
    lines = []
    for m in msgs:
        ts = (m.get("ts") or "")[:16].replace("T", " ")
        lines.append(f"**{m.get('author_name')}** · {ts}\n\n{m.get('content', '')}\n")
        for e in m.get("embeds", []):
            if not isinstance(e, dict):
                continue
            if e.get("text"):
                lines.append(f"> {e['text'].replace(chr(10), ' ')}\n")
            for im in e.get("images") or []:
                url = im.get("local_path") or im.get("url") if isinstance(im, dict) else im
                name = im.get("filename", "") if isinstance(im, dict) else "image"
                lines.append(f"- 🖼️ {name}: {url}\n")
        for a in m.get("attachments", []):
            url = a.get("local_path") or a.get("url") if isinstance(a, dict) else a
            name = a.get("filename", "") if isinstance(a, dict) else "file"
            lines.append(f"- 📎 {name}: {url}\n")
        if m.get("reactions"):
            lines.append(f"  reactions: {', '.join(m['reactions'])}\n")
        lines.append("\n")
    return "\n".join(lines)

def archive_path_hint() -> str:
    """Return the env var users should set (dmine archive location)."""
    return "DMINE_DB"
