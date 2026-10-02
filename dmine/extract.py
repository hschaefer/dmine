# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""DOM extraction of Discord chat messages.

Discord's web client virtualizes the message list: only a window of
messages is rendered at a time. We read exactly what is on screen — no
API, no tokens.

Extraction runs as ONE in-page JS pass (page.evaluate) instead of
per-element Playwright round-trips, which is 50-100x faster and keeps the
capture loop at human scroll speed.
"""
from __future__ import annotations

import re

from playwright.sync_api import Page

CHAT_LIST = 'ol[data-list-id="chat-messages"]'

_EXTRACT_JS = r"""
(() => {
  const list = document.querySelector('ol[data-list-id="chat-messages"]');
  if (!list) return [];
  const items = list.querySelectorAll('li[id^="chat-messages-"], li[data-list-item-id^="chat-messages__"]');
  const out = [];
  for (const li of items) {
    let mid = null;
    if (li.id && li.id.startsWith('chat-messages-')) {
      mid = li.id.split('-').pop();
    } else {
      const di = li.getAttribute('data-list-item-id');
      if (di && di.indexOf('__') !== -1) mid = di.split('__')[1].split('-').pop();
    }
    if (!mid) continue;

    const authorEl = li.querySelector('h3 span[class*="username"], span[class*="username"]');
    const author = authorEl ? authorEl.textContent.trim() : '';

    const timeEl = li.querySelector('time[datetime]');
    const ts = timeEl ? (timeEl.getAttribute('datetime') || '') : '';

    const embeds = [];
    for (const e of li.querySelectorAll('div[class*="embed"], article[class*="embed"]')) {
      const text = (e.innerText || '').trim();
      const a = e.querySelector('a[href]');
      const url = a ? a.getAttribute('href') : '';
      const images = [];
      for (const img of e.querySelectorAll('img[src]')) {
        const src = img.getAttribute('src') || '';
        if (src.indexOf('http') !== 0) continue;
        if (src.indexOf('/avatars/') !== -1 || src.indexOf('/emojis/') !== -1 || src.indexOf('/assets/') !== -1) continue;
        if (images.indexOf(src) === -1) images.push(src);
      }
      if (text || url || images.length) embeds.push({ text, url, images });
    }

    const contentEl = li.querySelector('div[id^="message-content-"]');
    let content = contentEl ? (contentEl.innerText || '') : '';
    for (const e of embeds) { if (e.text) content = content.replace(e.text, '', 1); }
    content = content.replace(/\n\s*\n+/g, '\n').trim();

    const reactions = [];
    for (const r of li.querySelectorAll('div[class*="reactions"] div[class*="reaction"]')) {
      const t = (r.innerText || '').trim().replace(/\s+/g, ' ');
      if (t) reactions.push(t);
    }

    const atts = [];
    for (const a of li.querySelectorAll('a[href*="/attachments/"]')) {
      const href = a.getAttribute('href');
      if (href && atts.indexOf(href) === -1) atts.push(href);
    }

    out.push({ id: mid, author_name: author, ts, content, embeds, reactions, attachments: atts });
  }
  return out;
})()
"""


def extract_visible_messages(page: Page, channel_id: str) -> list[dict]:
    """Return normalized message dicts for every message currently rendered."""
    rows = page.evaluate(_EXTRACT_JS) or []
    for r in rows:
        r["channel_id"] = channel_id
        r["reply_to_id"] = None
    return rows


def channel_header_name(page: Page) -> str:
    """Best-effort channel name from the header (breadcrumb 'Server: | # name')."""
    try:
        h1 = page.locator('div[class*="title"] h1, div[class*="chat"] header h1').first
        text = h1.inner_text(timeout=1500).strip()
    except Exception:
        return ""
    if "|" in text:
        text = text.rsplit("|", 1)[-1]
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return (lines[-1] if lines else text).lstrip("#").strip()


def wait_for_chat_list(page: Page, timeout_s: int = 45) -> None:
    page.locator(CHAT_LIST).first.wait_for(timeout=timeout_s * 1000)


def scroll_up(page: Page, amount: int = 2600, jitter_ms: int = 350) -> None:
    # hover the chat list so the wheel lands on the right scroller even in
    # daemon windows of arbitrary size (no fixed coordinates)
    try:
        page.locator(CHAT_LIST).first.hover(timeout=2000)
    except Exception:
        page.mouse.move(840, 450)
    page.mouse.wheel(0, -amount)
    page.wait_for_timeout(jitter_ms)
