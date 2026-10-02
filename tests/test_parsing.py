# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""Parsing helpers: URL/ID normalization, filename sanitization, remedia URL extraction."""
from dmine import capture
from dmine.media import _dest_path
from dmine.util import norm_channel_id, norm_server_id, sanitize_filename

URL = "https://discord.com/channels/100000000000000003/100000000000000004"


def test_norm_channel_id():
    assert norm_channel_id(URL) == "100000000000000004"
    assert norm_channel_id("100000000000000004") == "100000000000000004"


def test_norm_server_id():
    assert norm_server_id(URL) == "100000000000000003"
    assert norm_server_id("100000000000000004") is None
    assert norm_server_id("https://discord.com/channels/@me/123") is None


def test_sanitize_filename():
    assert sanitize_filename("a b/c..d?.png") == "a_b_c..d_.png"
    assert sanitize_filename("../etc/passwd") == "etc_passwd"
    assert sanitize_filename("") == "file"
    assert len(sanitize_filename("x" * 500)) <= 120


def test_dest_path_naming(tmp_path):
    p = _dest_path(tmp_path, "https://cdn.discordapp.com/attachments/1/2/photo.jpg?ex=1", "msg123", 0)
    assert p.name == "msg123_0_photo.jpg"
    assert p.parent == tmp_path


def test_remedia_entry_url():
    assert capture._entry_url("https://x/y.jpg") == "https://x/y.jpg"
    assert capture._entry_url({"url": "https://x/y.jpg", "local_path": "/tmp/y.jpg"}) is None  # done
    assert capture._entry_url({"url": "https://x/y.jpg", "error": "HTTP 404"}) == "https://x/y.jpg"  # retry
    assert capture._entry_url(None) is None
