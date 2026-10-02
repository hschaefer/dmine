# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""Attachment/image downloader.

Discord CDN links are signed and expire, so media must be downloaded
while they are fresh. Downloads stream to a .part file and are renamed
atomically — a crash never leaves a truncated file that later looks
"complete".
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

from .util import sanitize_filename

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

_MAX_MEDIA_MB = int(os.environ.get("DMINE_MAX_MEDIA_MB", "1024"))
_RETRIES = 2


def _dest_path(dest: Path, url: str, message_id: str, index: int) -> Path:
    filename = url.split("?", 1)[0].rsplit("/", 1)[-1]
    return dest / f"{message_id}_{index}_{sanitize_filename(filename)}"


def download_attachments(urls: list[str], dest_dir: str | Path, message_id: str) -> list[dict]:
    """Download each URL; returns [{url, filename, local_path, error?}]. Never raises."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    with httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": UA}) as client:
        for i, url in enumerate(urls):
            local = _dest_path(dest, url, message_id, i)
            entry = {"url": url, "filename": local.name}
            if local.exists() and local.stat().st_size > 0:
                entry["local_path"] = str(local)
                out.append(entry)
                continue
            entry = _download_one(client, url, local, entry)
            out.append(entry)
    return out


def _download_one(client: httpx.Client, url: str, local: Path, entry: dict) -> dict:
    max_bytes = _MAX_MEDIA_MB * 1024 * 1024
    part = local.with_suffix(local.suffix + ".part")
    for attempt in range(_RETRIES + 1):
        try:
            with client.stream("GET", url) as r:
                if r.status_code != 200:
                    entry["error"] = f"HTTP {r.status_code}"
                    return entry
                size = 0
                with open(part, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 16):
                        size += len(chunk)
                        if size > max_bytes:
                            entry["error"] = f"too large (>{_MAX_MEDIA_MB}MB)"
                            part.unlink(missing_ok=True)
                            return entry
                        f.write(chunk)
                if size == 0:
                    entry["error"] = "empty body"
                    part.unlink(missing_ok=True)
                    return entry
                os.replace(part, local)  # atomic: no truncated final files
                entry["local_path"] = str(local)
                return entry
        except Exception as e:  # noqa: BLE001
            part.unlink(missing_ok=True)
            if attempt < _RETRIES:
                time.sleep(1.0 * (attempt + 1))
                continue
            entry["error"] = str(e)
            return entry
    return entry  # pragma: no cover
