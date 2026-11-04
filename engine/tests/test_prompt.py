from __future__ import annotations

import re

import pytest

from engine.session.prompt import LOCAL_PREAMBLE_PROMPTS, PROMPTS, load_prompt
from engine.tools import ALLOWED_TOOL_NAMES

REQUIRED_SECTIONS = [
    "Role and Objective",
    "Personality and Tone",
    "Language",
    "Preambles",
    "Verbosity",
    "Tools",
    "Unclear Audio",
]


def _sections(text: str) -> dict[str, str]:
    parts = re.split(r"^# (.+)$", text, flags=re.M)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


@pytest.mark.parametrize("name", sorted(PROMPTS))
def test_has_openai_realtime_sections(name):
    sections = _sections(load_prompt(PROMPTS[name]).text)
    for section in REQUIRED_SECTIONS:
        assert section in sections, (name, section)


@pytest.mark.parametrize("name", ["v0", "v1", "v2", "v3"])
def test_model_preamble_has_one_english_and_one_hindi_example(name):
    preambles = _sections(load_prompt(PROMPTS[name]).text)["Preambles"]
    assert re.search(r"English example: \".+\"", preambles)
    assert re.search(r"Hindi example: \".+\"", preambles)


@pytest.mark.parametrize("name", ["v1", "v2", "v3"])
def test_short_preamble_is_one_to_three_words(name):
    preambles = _sections(load_prompt(PROMPTS[name]).text)["Preambles"]
    for phrase in re.findall(r"example: \"(.+?)\"", preambles):
        assert 1 <= len(phrase.split()) <= 3, phrase


@pytest.mark.parametrize("name", ["v1_local", "v2_local", "v3_local"])
def test_local_preamble_prompt_forbids_speaking_before_tool_results(name):
    preambles = _sections(load_prompt(PROMPTS[name]).text)["Preambles"]
    assert "Never speak before a tool result" in preambles
    assert "say nothing" in preambles.lower()


@pytest.mark.parametrize("name", ["v1", "v1_local", "v2", "v2_local", "v3", "v3_local"])
def test_tight_verbosity_with_three_examples_per_language(name):
    verbosity = _sections(load_prompt(PROMPTS[name]).text)["Verbosity"]
    assert re.search(r"(about 15|at most 12) words", verbosity)
    assert "one instruction at a time" in verbosity
    english, hindi = verbosity.split("- English examples:")[1].split("- Hindi examples:")
    for block in (english, hindi):
        examples = re.findall(r"^\s+- \"(.+)\"$", block, flags=re.M)
        assert len(examples) == 3
        assert all(len(e.split()) <= 15 for e in examples)


@pytest.mark.parametrize("name", ["v0", "v1", "v1_local", "v2", "v2_local"])
def test_phase_1_prompts_name_only_the_dummy_tool(name):
    text = load_prompt(PROMPTS[name]).text
    assert "get_reading" in text
    assert "request_power_step" not in text  # not exposed before Phase 2


def test_version_is_stable():
    assert load_prompt().version == load_prompt().version


@pytest.mark.parametrize("name", ["v2", "v2_local", "v3", "v3_local"])
def test_v2_language_rule_uses_the_latest_turn(name):
    language = _sections(load_prompt(PROMPTS[name]).text)["Language"]
    assert "latest turn only" in language


# --- v3: the Phase 2 prompt ------------------------------------------------------------------

V3 = ["v3", "v3_local"]


def _v3(name: str) -> dict[str, str]:
    return _sections(load_prompt(PROMPTS[name]).text)


@pytest.mark.parametrize("name", V3)
def test_v3_tools_section_covers_all_eight_tools(name):
    tools = _v3(name)["Tools"]
    for tool in ALLOWED_TOOL_NAMES:
        assert re.search(rf"^- {tool}\(", tools, flags=re.M), tool  # one bullet per tool, saying when to use it


@pytest.mark.parametrize("name", V3)
def test_v3_language_rules(name):
    language = _v3(name)["Language"]
    assert "latest turn only" in language
    assert "mixes Hindi and English" in language and "Hinglish" in language
    assert "Numbers, units and trade terms always stay in English" in language
    examples = re.findall(r'^\s+- "(.+)"$', language.split("Hinglish reading examples:")[1], flags=re.M)
    assert "Point three five volt, yeh normal hai." in examples and len(examples) == 3


@pytest.mark.parametrize("name", V3)
def test_v3_values_and_power_steps_only_from_tool_results(name):
    text = load_prompt(PROMPTS[name]).text
    assert "Never state a value, class, fault or probability that is not in a tool result" in text
    tools, safety = _v3(name)["Tools"], _v3(name)["Safety"]
    assert 'say the "say" sentence in the reply language exactly, word for word' in tools
    assert 'except with the exact "say" sentence of the safety gate' in safety
    assert 'engine has already asked the safety gate' in tools  # next_step "power" carries the gate's answer
    assert "never diagnose, rank faults or choose a step yourself" in _v3(name)["Role and Objective"]


@pytest.mark.parametrize("name", V3)
def test_v3_follows_engine_messages(name):
    engine = _v3(name)["Engine Messages"]
    assert '"[engine]"' in engine and "SAFETY STOP" in engine
    assert "[engine] SAFETY STOP. Say exactly:" in engine  # the text RepairSession.stop_session injects


def test_v3_variants_differ_only_in_preambles():
    a, b = _v3("v3"), _v3("v3_local")
    assert a.keys() == b.keys()
    assert {k for k in a if a[k] != b[k]} == {"Preambles"}
    assert "v3_local" in LOCAL_PREAMBLE_PROMPTS and "v3" not in LOCAL_PREAMBLE_PROMPTS
