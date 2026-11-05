"""Session language: set from the first two clear turns (mixed → Hinglish), then changed only by the
screen toggle or two consecutive clear turns in the other language; sent to the model as a note."""
from __future__ import annotations

import asyncio

import pytest

from engine.session.language import NOTE, SessionLanguage, clear_language, group
from engine.session.repair import RepairSession
from mcp_evidence.fake_bench import FakeBench


@pytest.mark.parametrize("tag,expected", [("hi", "hi"), ("hinglish", "hi"), ("en", "en"), ("unknown", None), (None, None)])
def test_group(tag, expected):
    assert group(tag) == expected


@pytest.mark.parametrize(
    "text,clear",
    [
        ("read the meter please", "en"),
        ("meter padho, kitna dikha raha hai", "hi"),
        ("मीटर पढ़ो", "hi"),  # Devanagari is clear even when short
        ("हाँ", "hi"),
        ("ok", None),  # too short to say anything
        ("yes", None),
        ("haan ji", None),
        ("Yes, that's right.", "en"),
        ("Haan, sahi hai.", "hi"),
        ("", None),
    ],
)
def test_clear_language(text, clear):
    assert clear_language(text) == clear


def feed(turns: list[str | None], start: SessionLanguage | None = None) -> tuple[str, list[bool], SessionLanguage]:
    s = start or SessionLanguage()
    changes = [s.observe(t) for t in turns]
    return s.lang, changes, s


def test_english_until_a_clear_turn():
    lang, changes, s = feed([None, None])
    assert (lang, changes, s.main, s.decided) == ("en", [False, False], None, False)


@pytest.mark.parametrize(
    "turns,lang,changes",
    [
        (["hi"], "hi", [True]),  # provisional
        (["hi", "hi"], "hi", [True, False]),  # decided
        (["en", "en"], "en", [True, False]),
        (["en", "hi"], "hi", [True, True]),  # mixed first turns: Hinglish
        (["hi", "en"], "hi", [True, False]),
        (["en", None, "en"], "en", [True, False, False]),  # unclear turns don't count
    ],
)
def test_the_first_two_clear_turns_set_the_language(turns, lang, changes):
    got, ch, s = feed(turns)
    assert (got, ch) == (lang, changes)
    assert s.decided == (sum(t is not None for t in turns) >= 2)


@pytest.mark.parametrize(
    "turns,lang,changes",
    [
        (["hi", "hi", "en"], "hi", [True, False, False]),  # one English turn: stays
        (["hi", "hi", "en", "en"], "en", [True, False, False, True]),  # two in a row: switches
        (["hi", "hi", "en", "hi", "en"], "hi", [True, False, False, False, False]),  # interrupted streak
        (["hi", "hi", "en", None, "en"], "en", [True, False, False, False, True]),  # unclear turns don't break it
        (["en", "en", "hi", "hi", "en", "en"], "en", [True, False, False, True, False, True]),
    ],
)
def test_after_deciding_only_two_consecutive_turns_switch(turns, lang, changes):
    got, ch, _ = feed(turns)
    assert (got, ch) == (lang, changes)


def test_the_toggle_sets_and_decides():
    s = SessionLanguage()
    assert s.toggle("hi") is True and s.decided and s.source == "toggle"
    assert s.toggle("hi") is False
    assert s.observe("en") is False and s.lang == "hi"  # a first English turn doesn't undo the toggle
    s.observe("en")
    assert s.lang == "en" and s.source == "turns"  # two in a row still switch
    with pytest.raises(ValueError):
        s.toggle("fr")


def test_repair_session_sends_a_note_only_when_the_language_changes():
    notes: list[dict] = []

    async def inject(msg):
        notes.append(msg)

    r = RepairSession("se_l", FakeBench(time_scale=0), inject=inject)

    async def turns():
        for text in ["read the meter please", "meter padho, kitna dikha raha hai", "ok", "supply kitna dikha raha hai",
                     "what is the reading now", "and the supply please"]:
            await r.on_user_transcript(text, None)
        await r.set_language("hi")
        await r.set_language("hi")

    asyncio.run(turns())
    langs = [e["lang"] for e in r.events if e["event"] == "session.lang"]
    assert langs == ["en", "hi", "en", "hi"]  # provisional en; mixed → hi; two English → en; toggle → hi
    assert [n["text"] for n in notes] == [f"[engine] {NOTE[l]}" for l in langs]
    assert all(n["urgent"] is False for n in notes)
    assert [e for e in r.events if e["event"] == "session.lang"][-1]["source"] == "toggle"


def test_the_session_language_survives_a_restart():
    r = RepairSession("se_l", FakeBench(time_scale=0))
    asyncio.run(r.on_user_transcript("meter padho, kitna dikha raha hai", None))
    asyncio.run(r.on_user_transcript("supply bhi padho na", None))
    restored = RepairSession.restore(r.state_dict(), [], FakeBench(time_scale=0))
    assert (restored.lng, restored.lang.decided) == ("hi", True)
