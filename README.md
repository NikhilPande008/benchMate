# Benchmate

A Hindi/English voice partner for mobile phone board repair. It reads a bench power supply and a multimeter through a camera, ranks likely faults from those readings with a deterministic engine, and tells the technician what to measure next.

Hackathon entry for AgentVerse '26, PS-03 (Voice AI). **Status: Phase 1b (latency fixes and re-measurement).** See [CLAIMS.md](CLAIMS.md) for what is proven, stubbed or assumed.

```
Browser (React + Agents SDK RealtimeSession) ──WebRTC──▶ OpenAI Realtime
        │  tool calls, spans, UI events (WebSocket)
        ▼
Engine (FastAPI): client secrets · tool router · telemetry (SQLite)
```

The OpenAI API key lives only in the engine. The browser gets a short-lived client secret (`ek_…`, pre-minted by the engine so connecting doesn't wait for it) and a fixed session config:
- prompt and tools;
- turn detection mode;
- reasoning effort `low`;
- `retention_ratio` truncation;
- an output cap of about 8 s of speech.

## Setup (macOS)

Needs Python 3.11+ with [uv](https://docs.astral.sh/uv/), Node 20+, and Google Chrome (only for the latency script).

```bash
cp .env.example .env
```

Put your `OPENAI_API_KEY` in `.env`. Never commit it: `.env` is git-ignored and the hooks below block it.

```bash
git config core.hooksPath .githooks
```

```bash
uv sync
```

```bash
cd apps/web && npm install && npm run build
```

## Run

```bash
uv run python -m engine
```

Open http://127.0.0.1:8000 and put on a headset. The page connects on load (allow the mic; if Chrome blocks audio, press any key or click once). Try "read the meter" or "supply padho". The dummy `get_reading` tool answers after 1.5 s (`BENCHMATE_DUMMY_DELAY_S`, or `?delay=` in the URL).

**Turn mode** (dev controls on the page, `BENCHMATE_TURN_MODE`, or `?turn_mode=`):
- semantic VAD (medium or high);
- server VAD (300, 400 or 500 ms silence);
- push-to-talk: hold the talk key while you speak. The default key is `Space`. To use a USB foot pedal, focus the "Talk key" box and press the pedal; the choice is remembered, or pass `?ptt_key=` in the URL.

**Preamble:** with the default prompt (`v2_local`), the page plays a short pre-recorded "Checking." or "एक सेकंड।" the moment the model calls a tool. Regenerate the clips with:

```bash
uv run python scripts/make_preamble_clips.py
```

For live-reload development, run the engine as above and Vite alongside it (Vite proxies `/realtime` and `/ws` to the engine):

```bash
cd apps/web && npm run dev
```

## Tests

```bash
uv run pytest
```

```bash
cd apps/web && npm run typecheck
```

## Latency

Every turn records spans (`speech_end`, `first_audio`, `audio_onset`, tool start/end) to `data/benchmate.sqlite`. To print p50/p95:

```bash
uv run python -m engine.telemetry
```

Experiments (turn-driven harness: 45 scripted lines played into an injected stream, one at a time). Build the clips first:

```bash
uv run python eval/latency_script/make_clips.py synthetic
```

Then run the configs:

```bash
uv run python eval/latency_script/run_experiments.py --start-engine --lanes 4
```

Connect-time benchmark:

```bash
uv run python eval/latency_script/run_experiments.py --start-engine --connect 10
```

Your own recordings go in `eval/intent_set/real/` (see its README); then pass `--set real`. The 3 Nov fake-WAV runner (`make_audio.py`, `run_latency.py`) still works. Results are in `eval/REPORT-*.md`.

## Layout

```
apps/web/        voice client (React, Agents SDK RealtimeSession over WebRTC)
engine/          FastAPI: api/ (client secret, WebSocket), tools/ (router), session/ (prompt), telemetry/
mcp_evidence/    evidence server (Phase 3)
packs/           model packs (Phase 2)
eval/            latency_script/, display_set/, boot_set/, intent_set/, REPORT-*.md
docs/            BUILD_GUIDE.md (source of truth), ARCHITECTURE.md, PRODUCT.md, NOTES.md
scripts/         secret_scan.py (pre-commit and pre-push hooks)
```
