# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""Storage unit tests: numeric snowflake ordering, idempotency, set_json whitelist, search escaping."""
import sqlite3

import pytest

from dmine.storage import Archive

# 18-digit vs 19-digit snowflake ids: lexicographic order diverges from numeric
ID_18 = "999999999999999999"
ID_19 = "1000000000000000000"


def _msg(i, channel="c1"):
    return {"id": str(i), "channel_id": channel, "author_name": "a", "ts": "2026-01-01T00:00:00Z",
            "content": f"msg {i}", "embeds": [], "reactions": [], "attachments": [], "reply_to_id": None}


def test_watermark_numeric_across_digit_boundary(tmp_path):
    with Archive(tmp_path / "a.sqlite") as a:
        a.upsert_messages([_msg(ID_18), _msg(ID_19)])
        assert a.watermark("c1") == ID_19  # numeric max, not lexicographic


def test_messages_since_across_digit_boundary(tmp_path):
    with Archive(tmp_path / "a.sqlite") as a:
        a.upsert_messages([_msg(ID_18), _msg(ID_19)])
        got = a.messages_since("c1", since_id=str(int(ID_18) - 1))
        ids = [m["id"] for m in got]
        assert ids == [ID_18, ID_19]
        # only the 19-digit one is newer than the 18-digit max
        got2 = a.messages_since("c1", since_id=ID_18)
        assert [m["id"] for m in got2] == [ID_19]


def test_upsert_idempotent(tmp_path):
    with Archive(tmp_path / "a.sqlite") as a:
        assert a.upsert_messages([_msg(1)]) == 1
        assert a.upsert_messages([_msg(1)]) == 0
        assert a.count("c1") == 1


def test_set_json_whitelist(tmp_path):
    with Archive(tmp_path / "a.sqlite") as a:
        a.upsert_messages([_msg(1)])
        with pytest.raises(ValueError):
            a.set_json("content", "1", "x")  # not a JSON column
        a.set_json("embeds", "1", [{"text": "hi"}])  # allowed
        assert a.messages_since("c1")[0]["embeds"] == [{"text": "hi"}]


def test_search_escapes_wildcards(tmp_path):
    with Archive(tmp_path / "a.sqlite") as a:
        a.upsert_messages([_msg(1), _msg(2)])
        a.set_json("embeds", "1", [{"text": "100% sicher"}])
        assert len(a.search("100%")) == 1      # literal % matches the one message
        assert len(a.search("%")) == 1         # bare % matches ONLY the literal-%-message
        assert len(a.search("_")) == 0         # bare _ must NOT match everything


def test_search_embeds_and_author(tmp_path):
    with Archive(tmp_path / "a.sqlite") as a:
        a.upsert_messages([_msg(1)])
        a.set_json("embeds", "1", [{"text": "Palladium drop"}])
        assert len(a.search("Palladium")) == 1
        assert len(a.search("author a")) == 0  # author is 'a'
        assert len(a.search("a")) >= 1


def _seed_two_servers(a):
    a.upsert_server("s1", "Alpha Guild")
    a.upsert_server("s2", "Beta Guild")
    a.upsert_channel("c1", "s1", "general")
    a.upsert_channel("c2", "s2", "general")
    for i, ch in ((11, "c1"), (12, "c2")):
        m = _msg(i, channel=ch)
        m["content"] = "shared needle topic"
        a.upsert_messages([m])


def test_search_server_scope(tmp_path):
    with Archive(tmp_path / "a.sqlite") as a:
        _seed_two_servers(a)
        hits = a.search("needle", server_id="s1")
        assert [h["channel_id"] for h in hits] == ["c1"]
        assert hits[0]["server_id"] == "s1"
        assert hits[0]["server_name"] == "Alpha Guild"
        # server + channel scopes combine
        assert a.search("needle", server_id="s2", channel_id="c1") == []
        # unscoped search still finds both, each carrying its server name
        both = a.search("needle")
        assert {h["server_name"] for h in both} == {"Alpha Guild", "Beta Guild"}


def test_search_keeps_orphan_rows(tmp_path):
    # messages whose channel has no channels row (legacy) stay findable,
    # with NULL server identity instead of being dropped by the JOIN
    with Archive(tmp_path / "a.sqlite") as a:
        a.upsert_messages([_msg(1, channel="noreg")])
        hits = a.search("msg")
        assert len(hits) == 1
        assert hits[0]["server_id"] is None
        assert hits[0]["server_name"] is None


def test_resolve_server_id(tmp_path):
    with Archive(tmp_path / "a.sqlite") as a:
        _seed_two_servers(a)
        assert a.resolve_server_id("s1") == "s1"          # exact id
        assert a.resolve_server_id("beta guild") == "s2"  # name, NOCASE
        assert a.resolve_server_id("BETA") == "s2"        # partial exact-case name
        with pytest.raises(ValueError):                   # ambiguous substring
            a.resolve_server_id("guild")
        with pytest.raises(ValueError):                   # unknown
            a.resolve_server_id("nope")


def test_channels_carry_server_name(tmp_path):
    with Archive(tmp_path / "a.sqlite") as a:
        _seed_two_servers(a)
        all_ch = a.channels()
        assert {c["server_name"] for c in all_ch} == {"Alpha Guild", "Beta Guild"}
        only = a.channels(server_id="s2")
        assert [c["id"] for c in only] == ["c2"]
        assert only[0]["server_name"] == "Beta Guild"
