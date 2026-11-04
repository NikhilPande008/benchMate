"""Session language (drives the preamble clip): set by the first tagged turn, switched only after
two consecutive turns in the other language; Hindi and Hinglish count as one."""
from __future__ import annotations

import asyncio

import pytest

from engine.session.language import SessionLanguage, group
from engine.session.repair import RepairSession
from engine.telemetry import lang_tag
from mcp_evidence.fake_bench import FakeBench


@pytest.mark.parametrize("tag,expected", [("hi", "hi"), ("hinglish", "hi"), ("en", "en"), ("unknown", None), (None, None)])
def test_group(tag, expected):
    assert group(tag) == expected


def feed(tags, start=None):
    s = SessionLanguage(start)
    changes = [s.observe(t) for t in tags]
    return s.lang, changes


def test_default_is_english_until_a_tagged_turn():
    assert SessionLanguage().lang == "en"
    assert feed(["unknown", None]) == ("en", [False, False])


def test_first_tagged_turn_sets_the_language():
    assert feed(["hinglish"]) == ("hi", [True])
    assert feed(["unknown", "en"]) == ("en", [False, True])


@pytest.mark.parametrize(
    "tags,lang,changes",
    [
        (["hi", "en"], "hi", [True, False]),  # one English turn: stays Hindi
        (["hi", "en", "en"], "en", [True, False, True]),  # two in a row: switches
        (["hi", "en", "hi", "en"], "hi", [True, False, False, False]),  # interrupted streak resets
        (["hi", "en", "unknown", "en"], "en", [True, False, False, True]),  # untagged turns don't break a streak
        (["en", "hinglish", "hi"], "hi", [True, False, True]),  # Hinglish and Hindi count together
        (["hi", "hinglish", "en", "hi", "en", "en"], "en", [True, False, False, False, False, True]),
        (["en", "hi", "hi", "en", "en"], "en", [True, False, True, False, True]),
    ],
)
def test_switch_only_after_two_consecutive_turns_in_the_other_language(tags, lang, changes):
    assert feed(tags) == (lang, changes)


def test_switch_after_is_configurable():
    s = SessionLanguage(switch_after=3)
    for t in ["hi", "en", "en"]:
        s.observe(t)
    assert s.lang == "hi"
    s.observe("en")
    assert s.lang == "en"


@pytest.mark.parametrize(
    "text,tag",
    [
        ("read the meter", "en"),
        ("मीटर पढ़ो", "hi"),
        ("meter padho, kitna dikha raha hai", "hinglish"),
    ],
)
def test_transcript_tags_feed_the_rule(text, tag):
    assert lang_tag(text) == tag


def test_repair_session_emits_session_lang_only_on_change():
    r = RepairSession("se_l", FakeBench(time_scale=0))

    async def turns():
        for text in ["read the meter", "meter padho", "ok", "kitna dikha raha hai", "supply padho"]:
            await r.on_user_transcript(text, lang_tag(text))

    asyncio.run(turns())
    langs = [e["lang"] for e in r.events if e["event"] == "session.lang"]
    assert langs == ["en", "hi"]
    assert r.lng == "hi"
