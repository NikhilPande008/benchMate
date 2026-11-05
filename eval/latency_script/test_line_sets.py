"""Line sets: the alternating set, the single-language sets, and real recordings filtered by language."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import make_clips  # noqa: E402
import run_experiments  # noqa: E402


@pytest.mark.parametrize("which", make_clips.SINGLE_LANGUAGE_SETS)
def test_single_language_sets_stay_in_one_language(which):
    lines, pause = make_clips.synthetic_lines(which)
    assert pause > 0
    assert {l["lang"] for l in lines} == {which}
    assert len({l["id"] for l in lines}) == len(lines) == 20
    assert sum(l["kind"] == "plain" for l in lines) == 12 and sum(l["kind"] == "tool" for l in lines) == 8
    assert sum("|" in l["text"] for l in lines) == 3  # scripted pauses, as in the alternating set
    voice = {"en": {"Rishi"}, "hinglish": {"Rishi"}, "hi": {"Lekha"}}[which]
    assert {l["voice"] for l in lines} == voice


def test_alternating_set_is_unchanged():
    lines, _ = make_clips.synthetic_lines("synthetic")
    assert len(lines) == 45 and {l["lang"] for l in lines} == {"en", "hi", "hinglish"}


def test_ids_are_unique_across_all_sets():
    ids = [l["id"] for w in ("synthetic", *make_clips.SINGLE_LANGUAGE_SETS) for l in make_clips.synthetic_lines(w)[0]]
    assert len(ids) == len(set(ids))


def _manifest(root: Path, which: str, lines: list[dict]) -> None:
    (root / which).mkdir(parents=True, exist_ok=True)
    (root / which / "manifest.json").write_text(json.dumps({"set": which, "lines": lines}))


def test_real_recordings_are_filtered_by_language(tmp_path, monkeypatch):
    monkeypatch.setattr(run_experiments, "AUDIO", tmp_path)
    _manifest(tmp_path, "real", [
        {"id": "hi-tool-01", "lang": "hi", "kind": "tool"},
        {"id": "mix-plain-01", "lang": "hinglish", "kind": "plain"},
        {"id": "en-plain-01", "lang": "en", "kind": "plain"},
    ])
    assert [l["id"] for l in run_experiments.load_manifest("real-hinglish")] == ["mix-plain-01"]
    assert run_experiments.audio_dir("real-hi") == "real"
    with pytest.raises(SystemExit, match="no hi recordings"):
        _manifest(tmp_path, "real", [{"id": "en-plain-01", "lang": "en", "kind": "plain"}])
        run_experiments.load_manifest("real-hi")


def test_single_language_sets_are_not_repeated(tmp_path, monkeypatch):
    monkeypatch.setattr(run_experiments, "AUDIO", tmp_path)
    lines = [{"id": f"g{i:02}", "lang": "hinglish", "kind": "plain" if i <= 12 else "tool"} for i in range(1, 21)]
    _manifest(tmp_path, "hinglish", lines)
    assert len(run_experiments.planned_lines("hinglish", None, None)) == 20
    assert [l["id"] for l in run_experiments.planned_lines("hinglish", ["g0*"], None)] == [f"g0{i}" for i in range(1, 10)]


def test_missing_set_says_how_to_build_it(tmp_path, monkeypatch):
    monkeypatch.setattr(run_experiments, "AUDIO", tmp_path)
    with pytest.raises(SystemExit, match="make_clips.py hi"):
        run_experiments.load_manifest("hi")
