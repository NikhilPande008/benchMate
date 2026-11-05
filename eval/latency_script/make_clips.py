"""Build per-line clips and a manifest for the turn-driven harness (run_experiments.py).

    uv run python eval/latency_script/make_clips.py synthetic   # macOS `say`, from lines.json (alternating languages)
    uv run python eval/latency_script/make_clips.py en          # single-language sets from lines_single.json:
    uv run python eval/latency_script/make_clips.py hinglish    #   en, hinglish, hi (one language per session)
    uv run python eval/latency_script/make_clips.py hi
    uv run python eval/latency_script/make_clips.py real        # your recordings in eval/intent_set/real/

Each manifest entry has the speech start and end inside the clip, measured from the samples,
and the longest pause inside the speech. Output: eval/latency_script/audio/<set>/ (git-ignored).

Real recordings: see eval/intent_set/real/README.md for naming. Any format `afconvert` reads
(wav, m4a, mp3, aiff, caf, flac) is converted to 48 kHz mono 16-bit WAV.
"""
from __future__ import annotations

import array
import json
import math
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parents[1]
AUDIO = HERE / "audio"
REAL_DIR = REPO / "eval" / "intent_set" / "real"
RATE = 48000
FRAME = RATE // 100  # 10 ms
LANGS = {"en": "en", "hi": "hi", "mix": "hinglish", "hinglish": "hinglish"}
AUDIO_EXT = {".wav", ".m4a", ".mp3", ".aif", ".aiff", ".caf", ".flac"}


def read_wav(path: Path) -> array.array:
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, RATE), path
        samples = array.array("h", w.readframes(w.getnframes()))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def write_wav(path: Path, samples: array.array) -> None:
    out = array.array("h", samples)
    if sys.byteorder == "big":
        out.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(out.tobytes())


def to_wav(src: Path, dst: Path) -> None:
    subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1", str(src), str(dst)], check=True)


def frame_rms(samples: array.array) -> list[float]:
    return [
        math.sqrt(sum(v * v for v in samples[i : i + FRAME]) / max(len(samples[i : i + FRAME]), 1))
        for i in range(0, len(samples), FRAME)
    ]


def speech_profile(samples: array.array, threshold: float | None = None, min_gap_ms: int = 150) -> dict:
    """Speech start/end (ms) and the longest internal pause. With no threshold, it adapts to the
    recording: 4x the noise floor (10th percentile frame RMS), and at least 5% of the loud frames."""
    rms = frame_rms(samples)
    if threshold is None:
        ordered = sorted(rms)
        floor = ordered[len(ordered) // 10]
        loud = ordered[int(len(ordered) * 0.98)]
        threshold = max(floor * 4, loud * 0.05, 60.0)
    speech = [i for i, r in enumerate(rms) if r > threshold]
    if not speech:
        raise ValueError("no speech found")
    gaps = [(b - a - 1) * 10 for a, b in zip(speech, speech[1:]) if (b - a - 1) * 10 >= min_gap_ms]
    return {
        "speech_start_ms": speech[0] * 10,
        "speech_end_ms": (speech[-1] + 1) * 10,
        "pause_ms": max(gaps, default=0),
    }


SINGLE_LANGUAGE_SETS = ("en", "hinglish", "hi")
OTHER_SETS = ("redteam",)  # mixed-language lines that try to get a power instruction without the gate


def synthetic_lines(which: str) -> tuple[list[dict], int]:
    """(lines, default pause ms) of a synthetic set: the alternating lines.json, or one of the
    single-language sets in lines_single.json (every line takes the set's language)."""
    if which == "synthetic":
        spec = json.loads((HERE / "lines.json").read_text())
        return spec["lines"], spec["default_pause_ms"]
    spec = json.loads((HERE / "lines_single.json").read_text())
    return [{"lang": which, **line} for line in spec["sets"][which]], spec["default_pause_ms"]


def build_synthetic(which: str = "synthetic") -> list[dict]:
    lines, default_pause_ms = synthetic_lines(which)
    out_dir = AUDIO / which
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    with tempfile.TemporaryDirectory() as tmp:
        for line in lines:
            parts = [p.strip() for p in line["text"].split("|")]
            pause = line.get("pause_ms", default_pause_ms) if len(parts) > 1 else 0
            clip = array.array("h", bytes(2 * RATE // 10))  # 100 ms lead-in
            for k, part in enumerate(parts):
                tmp_wav = Path(tmp) / f"{line['id']}_{k}.wav"
                subprocess.run(["say", "-v", line["voice"], "-o", str(tmp_wav), f"--data-format=LEI16@{RATE}", part], check=True)
                if k:
                    clip.extend(array.array("h", bytes(2 * RATE * pause // 1000)))
                clip.extend(read_wav(tmp_wav))
            clip.extend(array.array("h", bytes(2 * RATE // 10)))
            path = out_dir / f"{line['id']}.wav"
            write_wav(path, clip)
            prof = speech_profile(clip, threshold=300)
            entries.append(
                {
                    "id": line["id"],
                    "set": which,
                    "kind": line["kind"],
                    "lang": line["lang"],
                    "text": line["text"].replace(" | ", " "),
                    "file": path.name,
                    "clip_ms": round(len(clip) * 1000 / RATE),
                    "scripted_pause_ms": pause,
                    **prof,
                }
            )
    return entries


def parse_name(stem: str) -> dict | None:
    """<lang>-<kind>-<n>[-pause], e.g. hi-tool-03, mix-plain-07-pause."""
    parts = stem.lower().split("-")
    if len(parts) < 3 or parts[0] not in LANGS or parts[1] not in ("plain", "tool"):
        return None
    return {"lang": LANGS[parts[0]], "kind": parts[1], "scripted_pause": "pause" in parts[3:]}


def build_real() -> list[dict]:
    if not REAL_DIR.is_dir():
        raise SystemExit(f"no recordings: {REAL_DIR} does not exist (see eval/intent_set/real/README.md)")
    labels_file = REAL_DIR / "labels.json"
    labels = json.loads(labels_file.read_text()) if labels_file.exists() else {}
    out_dir = AUDIO / "real"
    out_dir.mkdir(parents=True, exist_ok=True)
    entries, skipped = [], []
    for src in sorted(p for p in REAL_DIR.iterdir() if p.suffix.lower() in AUDIO_EXT):
        meta = labels.get(src.name) or parse_name(src.stem)
        if not meta:
            skipped.append(src.name)
            continue
        dst = out_dir / f"{src.stem}.wav"
        to_wav(src, dst)
        prof = speech_profile(read_wav(dst))
        entries.append(
            {
                "id": src.stem,
                "set": "real",
                "kind": meta["kind"],
                "lang": LANGS.get(meta["lang"], meta["lang"]),
                "text": meta.get("text", ""),
                "file": dst.name,
                "clip_ms": round(len(read_wav(dst)) * 1000 / RATE),
                "scripted_pause_ms": 1 if meta.get("scripted_pause") else 0,
                **prof,
            }
        )
    if skipped:
        print(f"skipped (name not <lang>-<kind>-<n>[-pause] and not in labels.json): {', '.join(skipped)}")
    return entries


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "synthetic"
    if which not in ("synthetic", "real", *SINGLE_LANGUAGE_SETS, *OTHER_SETS):
        raise SystemExit(f"set must be synthetic, real or one of {SINGLE_LANGUAGE_SETS + OTHER_SETS}")
    entries = build_real() if which == "real" else build_synthetic(which)
    (AUDIO / which).mkdir(parents=True, exist_ok=True)
    (AUDIO / which / "manifest.json").write_text(json.dumps({"set": which, "lines": entries}, indent=1, ensure_ascii=False))
    kinds = {k: sum(1 for e in entries if e["kind"] == k) for k in ("plain", "tool")}
    paused = sum(1 for e in entries if e["pause_ms"] >= 300)
    print(f"{which}: {len(entries)} clips ({kinds['plain']} plain, {kinds['tool']} tool, {paused} with a pause >= 300 ms)")


if __name__ == "__main__":
    main()
