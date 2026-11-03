# First prompt for Claude Code (Phase 1: 3–4 Nov)

Paste everything below the line into Claude Code, from an empty folder that will become the repo.

---

Read `CLAUDE.md`, then `docs/BUILD_GUIDE.md` (sections: Decisions at a glance, Realtime API features to use, Reference architecture, Latency engineering, Build order). Skim `docs/ARCHITECTURE.md` for the data model.

Do **only Phase 1 ("3–4 Nov: skeleton and measurement")** from the Build order. Do not start Phase 2.

Before writing any voice/realtime code, look up the **current** OpenAI Agents SDK (TypeScript) voice-agents docs and Realtime API reference, and use the current model names, event names and session config keys. Write anything that differs from my docs into `docs/NOTES.md`.

Deliverables for this phase:
1. Repo scaffold matching the target layout in `CLAUDE.md`, with `.gitignore` (including `.env` and `.claude/`), a `.env.example`, and a pre-commit secret scan.
2. `engine/`: FastAPI app with
   - `POST /realtime/client-secret` returning a short-lived Realtime client secret (API key stays server-side),
   - a WebSocket endpoint the browser uses to relay tool calls and receive UI events,
   - a telemetry module that records per-turn spans (`speech_end`, `first_audio`, tool start/end) to SQLite and can print p50/p95.
3. `apps/web/`: a minimal page that opens an Agents SDK `RealtimeSession` over WebRTC with `semantic_vad` (eagerness medium), low reasoning effort, `echoCancellation: true`, `autoGainControl: false`, and **one dummy tool** (`get_reading`) whose call is relayed to the engine over the WebSocket and returns a fixed JSON line after a configurable delay (use 1.5 s to test async calling and a spoken preamble).
4. A short system prompt using OpenAI's realtime prompt sections (Role and Objective, Personality and Tone, Language, Preambles, Verbosity, Tools, Unclear Audio), with one Hindi and one English example line for the preamble.
5. `eval/latency_script`: a way to run ~20 scripted turns (recorded WAV files via Chrome fake-audio flags in Playwright, or manual if that is not ready yet) against both the flagship and the mini realtime model, and print `speech_end → first_audio` p50/p95 for plain turns and for the dummy-tool turn.

When done, stop and report:
- what you built and how to run it,
- the measured p50/p95 for both models,
- anything in the current OpenAI docs that differs from `docs/BUILD_GUIDE.md`,
- anything you were unsure about.

Do not commit secrets. Ask me before adding any dependency not listed in `CLAUDE.md`.
