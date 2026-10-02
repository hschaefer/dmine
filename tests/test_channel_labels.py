# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""Sidebar label -> channel name, for both German and English Discord clients.

Discord localises the text it puts into sidebar aria-labels. A locale that is
missing from the type-word list does not raise -- it silently leaks the label
through as a channel name, which then shows up in `channels`, `search` and
`export`. These cases therefore cover both locales explicitly.

Pure parsing only: no browser, no DOM, no network.
"""
from dmine.cli import _channel_name_from_label, _is_type_label


def test_plain_name_passes_through():
    assert _channel_name_from_label("main-chat") == "main-chat"


def test_german_type_word_with_qualifier_is_dropped():
    # "Text (begrenzt)" is the type, "gold" the name, "2" a badge count
    assert _channel_name_from_label("Text (begrenzt)\ngold\n2") == "gold"


def test_english_type_word_with_qualifier_is_dropped():
    # regression: this used to come back as "Text Channel (limited) general"
    assert _channel_name_from_label("Text Channel (limited)\ngeneral\n3") == "general"


def test_english_voice_channel_is_dropped():
    assert _channel_name_from_label("Voice Channel\nGeneral\n1") == "General"


def test_german_type_suffix_on_the_name_is_stripped():
    # "(Textkanal)" is a suffix on the real name; "Privater Kanal" IS the name
    assert (
        _channel_name_from_label("gold (Textkanal), Privater Kanal (gesperrt)")
        == "gold Privater Kanal (gesperrt)"
    )


def test_english_type_suffix_on_the_name_is_stripped():
    assert _channel_name_from_label("gold (Text channel)") == "gold"
    assert _channel_name_from_label("gold (Voice channel)") == "gold"


def test_unread_prefixes_are_stripped_both_locales():
    assert _channel_name_from_label("3 Mentions main-chat") == "main-chat"
    assert _channel_name_from_label("Ungelesene Nachrichten main-chat") == "main-chat"
    assert _channel_name_from_label("Unread messages main-chat") == "main-chat"


def test_invite_anchor_is_skipped_when_mixed_with_a_name():
    assert _channel_name_from_label("einladen, general") == "general"
    assert _channel_name_from_label("invite, general") == "general"


def test_digit_only_parts_are_skipped():
    assert _channel_name_from_label("7\ngeneral") == "general"


def test_name_is_truncated_to_80_chars():
    assert _channel_name_from_label("x" * 200) == "x" * 80


def test_empty_input():
    assert _channel_name_from_label("") == ""
    assert _channel_name_from_label("   ") == ""


def test_type_word_predicate_covers_both_locales():
    for label in ("Textkanal", "Text Channel", "Voice Channel", "Voicekanal",
                  "Ankündigungen", "Announcements", "Regeln", "Rules",
                  "Text (begrenzt)", "Text Channel (limited)"):
        assert _is_type_label(label), label
    for label in ("general", "main-chat", "Privater Kanal (gesperrt)"):
        assert not _is_type_label(label), label
