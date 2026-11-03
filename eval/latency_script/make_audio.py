"""Synthesize the scripted turns into one WAV for Chrome's fake microphone.

    uv run python eval/latency_script/make_audio.py

Uses macOS `say` (Rishi for English/Hinglish, Lekha for Hindi). Writes
eval/latency_script/audio/session.wav (48 kHz mono, 16-bit) and manifest.json with each
line's speech start/end offset in the file, measured from the samples (not from `say`'s
padding). Stand-in voices only: real Hindi/shop-noise recordings come in Phase 4.
"""
from __future__ import annotations

import array
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "audio"
RATE = 48000
FRAME = RATE // 100  # 10 ms
THRESHOLD = 300  # int16 amplitude counted as speech


def synth(text: str, voice: str, path: Path) -> array.array:
    subprocess.run(["say", "-v", voice, "-o", str(path), f"--data-format=LEI16@{RATE}", text], check=True)
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, RATE), path
        samples = array.array("h", w.readframes(w.getnframes()))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def silence(seconds: float) -> array.array:
    return array.array("h", bytes(2 * int(RATE * seconds)))


def speech_bounds(samples: array.array) -> tuple[int, int]:
    """First and last sample index of 10 ms frames whose peak exceeds THRESHOLD."""
    loud = [i for i in range(0, len(samples), FRAME) if max(map(abs, samples[i : i + FRAME]), default=0) > THRESHOLD]
    if not loud:
        raise ValueError("no speech in clip")
    return loud[0], min(loud[-1] + FRAME, len(samples))


def main() -> None:
    script = json.loads((HERE / "turns.json").read_text())
    OUT.mkdir(exist_ok=True)
    out = silence(script["lead_in_s"])
    manifest = []
    with tempfile.TemporaryDirectory() as tmp:
        for turn in script["turns"]:
            clip = synth(turn["text"], turn["voice"], Path(tmp) / f"{turn['id']}.wav")
            s, e = speech_bounds(clip)
            base = len(out)
            out.extend(clip)
            manifest.append(
                {
                    **{k: turn[k] for k in ("id", "kind", "lang", "text")},
                    "start_ms": round((base + s) * 1000 / RATE, 1),
                    "end_ms": round((base + e) * 1000 / RATE, 1),
                }
            )
            out.extend(silence(turn["gap_s"]))
    if sys.byteorder == "big":
        out.byteswap()
    with wave.open(str(OUT / "session.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(out.tobytes())
    duration_s = len(out) / RATE
    (OUT / "manifest.json").write_text(json.dumps({"duration_s": duration_s, "turns": manifest}, indent=1))
    print(f"wrote {OUT / 'session.wav'} ({duration_s:.0f} s, {len(manifest)} turns)")


if __name__ == "__main__":
    main()
