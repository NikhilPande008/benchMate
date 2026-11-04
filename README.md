# Benchmate

A Hindi/English voice partner for mobile phone board repair. It reads a bench power supply and a multimeter through a camera, ranks likely faults from those readings with a deterministic engine, and tells the technician what to measure next.

Hackathon entry for AgentVerse '26, PS-03 (Voice AI). **Status: Phase 2 (diagnosis loop on a fake bench; no camera or hardware yet).** See [CLAIMS.md](CLAIMS.md) for what is proven, stubbed or assumed.

```
Browser (React + Agents SDK RealtimeSession) ──WebRTC──▶ OpenAI Realtime
        │  8 tool calls, spans, transcripts, barge-in ▲ UI events, engine notes (WebSocket)
        ▼
Engine (FastAPI): client secrets · tool router · repair session
        ├─ ledger (deterministic Bayes + value-of-information next step)
        ├─ safety gate (every power step) · scope checks · at-limit watchdog
        ├─ spoken-reading parser + read-back
        └─ evidence source: fake bench (scripted scenarios) · SQLite (evidence, ledger snapshots, records)
```

The voice model never ranks faults or chooses a step: it calls the engine's 8 tools and says what they return. Power instructions are spoken only with the safety gate's exact sentence.

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

Open http://127.0.0.1:8000 and put on a headset. The page connects on load (allow the mic; if Chrome blocks audio, press any key or click once). Hold **Space** and say "Demo phone hai, dead hai, battery nikal di hai", then follow the steps. Readings come from the **fake bench**: pick a scenario in the dev controls ("Fake bench") or with `?scenario=` (short_battery_line, no_current, not_charging, glare_low_confidence, conflicting_reading, camera_unavailable, vision_timeout). Meter reads take 1.5 s (`BENCHMATE_DUMMY_DELAY_S`, or `?delay=`). The ledger panel shows the top faults and the current step. `GET /dev/sessions/<id>` shows the engine's view of a session.

**Turn mode** (default push-to-talk; dev controls on the page, `BENCHMATE_TURN_MODE`, or `?turn_mode=`):
- semantic VAD (medium or high);
- server VAD (300, 400 or 500 ms silence);
- push-to-talk: hold the talk key while you speak. The default key is `Space`. To use a USB foot pedal, focus the "Talk key" box and press the pedal; the choice is remembered, or pass `?ptt_key=` in the URL.

**Preamble:** with the default prompt (`v3_local`), the page plays a short pre-recorded "Checking." or "एक सेकंड।" the moment the model calls a tool. Regenerate the clips with:

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

Engine-level scenario runs (no voice, all seven fake-bench scenarios through the 8 tools) and end-to-end spoken sessions (push-to-talk, synthetic clips, the real voice model):

```bash
uv run python eval/scenarios/engine_run.py
```

```bash
uv run python eval/scenarios/voice_run.py --start-engine
```

Your own recordings go in `eval/intent_set/real/` (see its README); then pass `--set real`. The 3 Nov fake-WAV runner (`make_audio.py`, `run_latency.py`) still works. Results are in `eval/REPORT-*.md`.

## Layout

```
apps/web/        voice client (React, Agents SDK RealtimeSession over WebRTC)
engine/          FastAPI: api/ (client secret, WebSocket, dev), tools/ (8-tool router), session/ (repair session, prompt, language), ledger/, safety/, telemetry/
mcp_evidence/    evidence: record schema, fake bench, boot-pattern classifier, spoken parser, pack loader (MCP server in Phase 3)
packs/           model packs (general_dead_phone.json, demo_model/; placeholder numbers) and their JSON Schemas
eval/            latency_script/, scenarios/ (engine and spoken runs), display_set/, boot_set/, intent_set/, REPORT-*.md
docs/            BUILD_GUIDE.md (source of truth), ARCHITECTURE.md, PRODUCT.md, NOTES.md
scripts/         secret_scan.py (pre-commit and pre-push hooks)
```
