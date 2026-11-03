"""Generate the local preamble clips once with OpenAI TTS.

    uv run python scripts/make_preamble_clips.py

Writes apps/web/public/preambles/en.wav and hi.wav (24 kHz mono 16-bit), trimmed of leading and
trailing silence so they start the moment they are played. Same voice as the realtime session.
"""
from __future__ import annotations

import array
import sys
import wave
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.config import REPO_ROOT, Settings  # noqa: E402

OUT = REPO_ROOT / "apps" / "web" / "public" / "preambles"
RATE = 24000  # response_format "pcm" is 24 kHz 16-bit little-endian mono
# (text, delivery instructions, speed)
CLIPS = {
    "en": ("Checking.", "Say it briefly and calmly, like a technician at the bench.", 1.0),
    "hi": ("एक सेकंड।", "Speak in Hindi, quickly and casually, like a technician at the bench.", 1.3),
}


def trim(samples: array.array, threshold: int = 200, pad_ms: int = 15) -> array.array:
    loud = [i for i, v in enumerate(samples) if abs(v) > threshold]
    if not loud:
        return samples
    pad = RATE * pad_ms // 1000
    return samples[max(0, loud[0] - pad) : min(len(samples), loud[-1] + pad)]


def main() -> None:
    settings = Settings.from_env()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is not set")
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30) as http:
        for lang, (text, instructions, speed) in CLIPS.items():
            resp = http.post(
                f"{settings.openai_base_url}/audio/speech",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": "gpt-4o-mini-tts",
                    "voice": settings.voice,
                    "input": text,
                    "instructions": instructions,
                    "response_format": "pcm",
                    "speed": speed,
                },
            )
            resp.raise_for_status()
            samples = array.array("h", resp.content)
            if sys.byteorder == "big":
                samples.byteswap()
            samples = trim(samples)
            path = OUT / f"{lang}.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(samples.tobytes())
            print(f"{path}: {text!r} {len(samples) / RATE * 1000:.0f} ms")


if __name__ == "__main__":
    main()
