# Notes: current OpenAI docs vs our docs

Checked 3 Nov 2026 against `@openai/agents-realtime` 0.21.0 and `openai` (Node) 7.47.0 type definitions, the Agents SDK voice-agents guide, and OpenAI's "Prompting Realtime models" guide. BUILD_GUIDE.md stays the source of truth for decisions; this file records where the platform differs from what our docs say, and what the code does about it.

## Models
- **Flagship `gpt-realtime-2.1`, mini `gpt-realtime-2.1-mini`.** Both released 6 Jul 2026. `gpt-realtime-2.1` is the SDK's `DEFAULT_OPENAI_REALTIME_MODEL`. OpenAI's prompting guide is still written for `gpt-realtime-2`. Set in `.env` (`BENCHMATE_REALTIME_MODEL*`), and the engine allows only these two.
- **GPT-Live (`gpt-live-1`) is a different architecture**, not a faster drop-in. It is full duplex (it can speak while listening, with backchannels) and hands work to a *backend agent* (`Generate <|SpawnThinking|>`, `session.commentary.append`) instead of calling function tools the way `RealtimeSession` does. Our safety gate and the "model never ranks faults" rule would have to live in that backend. Not tested in Phase 1. See "Open questions".
- The realtime context window grew from 32k to 128k tokens with `gpt-realtime-2`. Our `post_instructions` cap of 8000 still applies.

## Session config shape (ARCHITECTURE.md §4.2 uses the old beta shape)
- Turn detection and transcription now sit under `audio.input`: `audio.input.turn_detection`, `audio.input.transcription`, `audio.input.noise_reduction`. Voice is `audio.output.voice`. `output_modalities` replaces `modalities`. The session needs `type: "realtime"`.
- The SDK uses camelCase (`audio.input.turnDetection`) and accepts snake_case keys inside turn detection. The old top-level keys still work but are deprecated.
- `turn_detection`: `{type: "semantic_vad", eagerness: "medium", create_response: true, interrupt_response: true}`, as decided.
- `reasoning: {effort}` takes `minimal | low | medium | high | xhigh`. We use `low`, and OpenAI's guide also recommends starting there.
- **`truncation` is in the API but is not an SDK config key.** It is sent as `{type: "retention_ratio", retention_ratio: 0.8, token_limits: {post_instructions: 8000}}`, in the client-secret session and through the SDK's `providerData`, which is spread into `session.update`.
- Transcription: the SDK default `gpt-4o-mini-transcribe` is **deprecated (shutdown 2027-02-26)**. We use `gpt-transcribe` with `languages: ["hi","en"]` and trade-term `keywords`. Transcripts are for the screen and the log only.
- `noise_reduction` (`near_field` / `far_field`) is a server-side option. It is `null` here to follow "no extra denoiser". Worth an A/B test with a headset in shop noise (Phase 4).

## Client secret and WebRTC
- `POST /v1/realtime/client_secrets` with `{expires_after: {anchor: "created_at", seconds: 10..7200}, session: {...}}` returns `{value: "ek_...", expires_at, session}`. A secret can open several sessions until it expires, so we keep the TTL short (120 s).
- The SDK posts the SDP offer to `/v1/realtime/calls`. In a browser it **refuses any key that is not `ek_`**, which is a second guard for rule 5.
- The engine configures the whole session when it mints the secret and returns the same config. The browser maps it 1:1 into `RealtimeSession`, whose first `session.update` (sent when the data channel opens; `connect()` waits for `session.updated`) is therefore identical. Nothing changes after that (rule 7).
- **The SDK's default mic is `getUserMedia({audio: true})`**, which would turn AGC on. We open the mic ourselves (`echoCancellation: true, autoGainControl: false, noiseSuppression: false`) and pass `mediaStream`. The SDK does not stop a stream you pass in, so we stop it ourselves.

## Events (what telemetry hooks)
- On WebRTC, the SDK's `audio` and `audio_start` session events **do not fire**, because audio arrives on the media track. First audio is the WebRTC-only server event `output_audio_buffer.started`, read through `session.on('transport_event')`. We also watch the remote track with an `AnalyserNode` for audible onset.
- `speech_end` is the client's receipt of `input_audio_buffer.speech_stopped` (it carries `audio_end_ms`). With semantic VAD this event arrives *after* the end-of-turn wait, so `speech_end → first_audio` leaves that wait out. The eval script measures it separately (`vad_lag`) from the known end of each scripted line.
- `gpt-realtime-2.x` responses come in phases: `commentary` (preambles, tool calls) and `final_answer`, visible in `response.done` as `output[].phase`.

## Tools and async calling (ARCHITECTURE.md §4.3-4.5)
- §4.3 says the browser sends `conversation.item.create` and `response.create` after a tool result. **The SDK does both automatically** when `execute()` returns.
- §4.4's "return `{status: "started"}` at once, push the result later" is **not needed for get_reading**. `execute()` is async, the session keeps handling events while it runs, and the GA model supports asynchronous function calling. `backgroundResult()` exists for outputs that should *not* trigger a reply; use it for `watch_boot_current` in Phase 3.
- §4.5's manual `response.cancel` / `conversation.item.truncate`: on WebRTC with `interrupt_response: true`, the server and SDK do this (`audio_interrupted` and `output_audio_buffer.cleared`). Do not hand-write it.
- Tool parameters can be plain JSON Schema (`strict: true`). The engine owns the specs (`engine/tools`), and the browser builds generic relay tools from them, so the 8-tool limit is enforced and tested in one place.

## Prompting (OpenAI guide vs BUILD_GUIDE "Prompting rules")
- Current section list: Role and Objective, Personality and Tone, Language, **Reasoning**, **Message Channels**, Preambles, Verbosity, Tools, Unclear Audio, **Entity Capture**, **Long Context Behavior**, **Escalation**. Our prompt has the seven asked for, plus a one-line **Reasoning** section ("answer direct questions without reasoning", which matters for latency) and our own **Safety** section. Entity Capture (number read-back) belongs in Phase 2's spoken-reading prompt.
- **`gpt-realtime-2.x` speaks preambles by default.** The guide says to test the default before tuning. We still give one fixed English and one Hindi example, as BUILD_GUIDE asks.
- The guide recommends a no-op **`wait_for_user` tool** for background noise, TV audio and side conversation. That would be a 9th tool, against rule 4. **Needs a decision** (Phase 4, shop noise).
- The Hindi preamble in BUILD_GUIDE ("Supply padh raha hoon…") is masculine first person. With a voice like `marin` it may sound off, so the prompt uses a gender-neutral line: "Ek second, meter padhte hain."

## Tooling
- Tracing: set through `RealtimeSession` options (`workflowName`, `groupId` = our session id, `traceMetadata`). The SDK applies it after `session.created`, and it cannot be changed once set, so the client secret does not set tracing.
- Starlette's `TestClient` warns that `httpx` is deprecated in favour of `httpx2`. Harmless for now; it would be a dependency change.
- Vite 8 (rolldown) and TypeScript 7 (native compiler) work as-is. The web bundle is 1.2 MB, mostly agents-core; worth code-splitting later.

## Observed in the first latency run (3 Nov, `eval/REPORT-2026-11-03.md`)
- Both `gpt-realtime-2.1` and `gpt-realtime-2.1-mini` accept `reasoning.effort` and `truncation` in the client-secret session (no errors).
- Preambles happen by default as documented: every tool turn produced a `commentary` + `function_call` response, then a `final_answer` response after the result.
- `semantic_vad` at medium eagerness signals `speech_stopped` about 1 s after speech really ends (synthetic speech). BUILD_GUIDE's budget is 0.3–0.5 s. The cheap fixes are still untested: eagerness `high`, `server_vad` with a short `silence_duration_ms`, or push-to-talk.
- The value reply plays only after the preamble's audio finishes, so a long preamble delays the value. Preambles ran 1.5–2.8 s.
- The SDK's `audio_interrupted` session event did not fire on WebRTC when the server cleared audio for a barge-in. `output_audio_buffer.cleared` did, so telemetry uses that.


## Phase 1b (4 Nov): keys checked against `openai` 7.47.0 and `@openai/agents-realtime` 0.21.0 types and OpenAI's realtime conversations guide
- **Turn detection options.** `semantic_vad.eagerness` is `low | medium | high | auto`; `auto` equals `medium`. The maximum waits are 8 s, 4 s and 2 s. `server_vad` takes `threshold` (default 0.5), `prefix_padding_ms` (default 300), `silence_duration_ms` (default 500) and an optional `idle_timeout_ms`. Both take `create_response` and `interrupt_response`. The engine exposes six modes (`engine/api/realtime.py` `TURN_MODES`).
- **Push-to-talk on WebRTC** (OpenAI's documented procedure, which differs from WebSocket). Set `turn_detection: null`. On key down, send `input_audio_buffer.clear`; if the model is talking, also send `response.cancel` and `output_audio_buffer.clear` (the SDK's `session.interrupt()` sends both). On key up, send `input_audio_buffer.commit`, then `response.create`. The SDK routes a manual `response.create` through its sequencer. ARCHITECTURE §4.5's `conversation.item.truncate` is the WebSocket procedure; on WebRTC, `output_audio_buffer.clear` truncates.
- **Switching turn mode at runtime.** A `session.update` carrying only `audio.input.turn_detection` is merged. Transcription and `max_output_tokens` were still set after each switch, as checked in `session.updated`. Instructions and tools are never re-sent.
- **`max_output_tokens`** is an integer 1–4096 or `"inf"` (the default), and it *includes tool calls*. It is not an SDK config key, so it goes through `providerData`. A capped reply ends with `response.status_details.reason: "max_output_tokens"`. Measured here: `output_tokens = audio_tokens + text_tokens` with no hidden reasoning tokens at effort `low`. Audio output runs at **20 tokens per second**, and transcript text adds 0.3–0.8× the audio tokens. So about 8 s of speech is roughly 220 tokens, which is the cap.
- **Input transcription.** `gpt-transcribe` returns `languages: [{code}]` on `conversation.item.input_audio_transcription.completed`. We store it alongside our own script- and word-based tag (`engine/telemetry/lang.py`). Romanised Hinglish speech often comes back in Devanagari, so the report counts languages from the *scripted* line language, not the transcript.
- **TTS for the local preamble clips.** Models are `gpt-4o-mini-tts` (snapshot `-2025-12-15`), `tts-1` and `tts-1-hd`. The voices include `marin` and `cedar`, so the clip matches the realtime voice. `response_format: "pcm"` is 24 kHz 16-bit mono. `speed` is honoured: 1.3 shortened "एक सेकंड" from 1057 to 822 ms.
- **When the SDK runs a tool.** It dispatches on `response.output_item.done` (a completed `function_call`). The earliest client-side signal is `response.output_item.added`, which is where the local clip starts. After `execute()` returns, `response.create` goes out at once; the sequencer holds it only while another response is in flight. Measured: tool result → `response.created` ≈ one network round trip.
- **Chrome pitfall (harness).** An idle `MediaStreamAudioDestinationNode` track sends **zero RTP packets**, so server VAD sees speech start and never sees it stop. The harness mixes in −60 dBFS noise, which a real mic has anyway.
- **Echo cancellation and the local clip.** The docs I could reach don't say whether Chrome's echo canceller uses a plain `<audio src=…>` element as its reference (Chrome's 2018 notes say the software canceller uses an internal loopback of playout). So the clip plays through a local WebRTC loopback, the same kind of path as the model's voice. A plain `<audio>` element is the fallback (`?preamble_path=element`).
- **Harness deviation from CLAUDE.md.** Latency experiments no longer use `--use-file-for-fake-audio-capture`. The page plays each clip into an injected stream only after the previous reply has finished (`apps/web/src/harness.ts`). That gives exact true speech times (the fake-WAV alignment was ±300 ms), no barge-ins from fixed gaps, and exact push-to-talk timing. The fake-WAV runner (`run_latency.py`) still works and still covers the real `getUserMedia` path; the connect benchmark uses that path too.
