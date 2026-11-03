# CLAUDE.md — Benchmate

Benchmate is a Hindi/English voice partner for mobile phone board repair. It reads a bench power supply and multimeter through a camera, ranks likely faults from those readings, and tells the technician what to measure next. Hackathon entry: AgentVerse '26, PS-03 (Voice AI). Submit 28 Nov 2026.

## Read these first
- `docs/BUILD_GUIDE.md`: **source of truth** for decisions, latency techniques, corner cases and build order.
- `docs/ARCHITECTURE.md`: data model, ledger maths, vision pipeline details.
- `docs/PRODUCT.md`: users, stories, acceptance criteria, demo, field test.

Where documents disagree, **BUILD_GUIDE.md wins** (e.g. 8 coarse tools exposed to the voice model, not 15).

## Hard rules (never break these)
1. **The language model never ranks faults.** The diagnosis ledger (Bayes update + value-of-information next step) is plain, deterministic, unit-tested Python. The voice model only converses, explains and phrases what the engine returns.
2. **Every power instruction goes through the safety gate.** The voice model calls `request_power_step`; the engine checks the camera-read supply voltage and current limit (fresh within 20 s, confidence ≥ 0.9) and returns either the exact approved sentence or a block reason. No other path may tell the user to connect power.
3. **Every instrument reading uses both readers.** Reader A (OpenAI vision model, strict JSON schema) and Reader B (local seven-segment OCR) on the same two frames ~150 ms apart; accept only on agreement of digits and decimal point; otherwise low confidence and ask the user to read it aloud. Never trust a single frame or a single reader.
4. **At most 8 tools are exposed to the voice model**: `start_repair`, `get_reading`, `watch_boot_current`, `record_spoken_reading`, `next_step`, `request_power_step`, `explain`, `close_repair`. Fine-grained tools stay inside the engine/evidence server.
5. **The OpenAI API key never reaches the browser.** The browser gets short-lived Realtime client secrets from the engine.
6. **Tool outputs to the voice model are one short JSON line.** No logs, frames or long text in conversation context.
7. **Do not change session instructions or the tool list mid-session** (breaks prompt caching). Inject changing state as short engine messages.
8. **No customer data, no schematics** in the repo or the packs. Packs contain only our own reference measurements and observed faults.

## Stack
- Browser: TypeScript, Vite, React (or Svelte), **OpenAI Agents SDK `RealtimeSession`** over WebRTC.
- Engine: Python 3.11+, FastAPI (HTTP + WebSocket), Pydantic, SQLModel/SQLite, asyncio (no blocking calls on the event loop; OpenCV/OCR on worker threads).
- Evidence server: Python, MCP Python SDK, OpenCV, `ssocr` (Reader B), OpenAI vision model (Reader A).
- Tests: pytest; Playwright (Chrome with `--use-fake-device-for-media-stream --use-file-for-fake-audio-capture=<wav>`) for end-to-end voice tests.

## Verify before you write voice code
OpenAI's Realtime API and Agents SDK change often. **Before writing or changing voice/realtime code, look up the current docs** (Agents SDK voice agents guide, Realtime API reference, realtime prompting guide) and use current model names, event names and config keys. Do not rely on memory for these. Note any differences from the docs in this repo in `docs/NOTES.md`.

## Settings that are already decided
- Turn detection: `semantic_vad`, eagerness `medium`; push-to-talk fallback.
- Reasoning effort on the realtime model: `low`.
- Truncation: `retention_ratio` 0.8 with a `post_instructions` token cap (~8000).
- Rotate the realtime session after ~25 turns or ~30 min at a natural pause, seeded with an engine summary.
- Mic: `echoCancellation: true`, `autoGainControl: false`, no extra client denoiser; headset assumed.
- Vision call timeout 4 s; hedged second request at 2.5 s.

## How to work in this repo
- Work **one build-order phase at a time** (see BUILD_GUIDE.md → Build order). Stop at the end of a phase and report what was done, test results and latency numbers.
- Every new behaviour gets a test. Ledger and safety gate changes need unit tests in the same change.
- Telemetry spans from day 1: `speech_end`, `first_audio`, per-tool start/end, `reader_a_ms`, `reader_b_ms`, `agreement`.
- Use the **fake bench** (scripted evidence scenarios) until real hardware is available. Keep its numbers rough; do not tune the engine to invented values.
- Record evaluation results in `eval/REPORT-<date>.md`; keep `CLAIMS.md` honest (Proven / Stubbed / Assumed).
- Ask before adding dependencies or new services.

## Git and secrets
- The repo is **public** (hackathon rule). Never commit `.env`, keys, field footage, or anything under `.claude/`.
- `.gitignore` must include: `.env`, `.claude/`, `data/`, `frames/`, `*.sqlite`, `node_modules/`, `__pycache__/`.
- Do not create git repos or commit inside `.claude/`.

## Repo layout (target)
```
apps/web/            voice client + companion screen
engine/              FastAPI: api, session, ledger, safety, context, records, telemetry
mcp_evidence/        cameras, vision (reader A/B, agreement), boot watch, spoken parser, packs
packs/               general_dead_phone.json, <model_id>/
eval/                display_set/, boot_set/, intent_set/, run_eval.py, REPORT-*.md
docs/                BUILD_GUIDE.md, ARCHITECTURE.md, PRODUCT.md, NOTES.md
CLAUDE.md  CLAIMS.md  README.md
```
