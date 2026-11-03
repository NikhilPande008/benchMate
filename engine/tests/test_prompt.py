from __future__ import annotations

import re

import pytest

from engine.session.prompt import PROMPTS, load_prompt

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


@pytest.mark.parametrize("name", ["v0", "v1", "v2"])
def test_model_preamble_has_one_english_and_one_hindi_example(name):
    preambles = _sections(load_prompt(PROMPTS[name]).text)["Preambles"]
    assert re.search(r"English example: \".+\"", preambles)
    assert re.search(r"Hindi example: \".+\"", preambles)


@pytest.mark.parametrize("name", ["v1", "v2"])
def test_short_preamble_is_one_to_three_words(name):
    preambles = _sections(load_prompt(PROMPTS[name]).text)["Preambles"]
    for phrase in re.findall(r"example: \"(.+?)\"", preambles):
        assert 1 <= len(phrase.split()) <= 3, phrase


@pytest.mark.parametrize("name", ["v1_local", "v2_local"])
def test_local_preamble_prompt_forbids_speaking_before_tool_results(name):
    preambles = _sections(load_prompt(PROMPTS[name]).text)["Preambles"]
    assert "Never speak before a tool result" in preambles
    assert "say nothing" in preambles.lower()


@pytest.mark.parametrize("name", ["v1", "v1_local", "v2", "v2_local"])
def test_tight_verbosity_with_three_examples_per_language(name):
    verbosity = _sections(load_prompt(PROMPTS[name]).text)["Verbosity"]
    assert re.search(r"(about 15|at most 12) words", verbosity)
    assert "one instruction at a time" in verbosity
    english, hindi = verbosity.split("- English examples:")[1].split("- Hindi examples:")
    for block in (english, hindi):
        examples = re.findall(r"^\s+- \"(.+)\"$", block, flags=re.M)
        assert len(examples) == 3
        assert all(len(e.split()) <= 15 for e in examples)


@pytest.mark.parametrize("name", sorted(PROMPTS))
def test_prompt_names_only_the_exposed_tool(name):
    text = load_prompt(PROMPTS[name]).text
    assert "get_reading" in text
    assert "request_power_step" not in text  # not exposed before Phase 2


def test_version_is_stable():
    assert load_prompt().version == load_prompt().version


@pytest.mark.parametrize("name", ["v2", "v2_local"])
def test_v2_language_rule_uses_the_latest_turn(name):
    language = _sections(load_prompt(PROMPTS[name]).text)["Language"]
    assert "latest turn only" in language
