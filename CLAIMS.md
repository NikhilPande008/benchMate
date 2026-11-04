# Claims

Every outward-facing claim about Benchmate, with its evidence. Updated at the end of each build phase.

**Proven** = measured or tested in this repo, with the evidence named. **Stubbed** = the interface exists but behind it is a placeholder. **Assumed** = believed, or a target, not yet shown.

## Proven
| Claim | Evidence |
| --- | --- |
| The OpenAI API key never reaches the browser; the browser gets a short-lived client secret | `engine/tests/test_client_secret.py` (the key never appears in a response); the SDK refuses non-`ek_` keys in browsers |
| Session settings: turn detection mode from a fixed list, reasoning `low`, `retention_ratio` 0.8 truncation with an 8000-token cap, `max_output_tokens` 220 | `engine/tests/test_client_secret.py` |
| Exactly the 8 tools of CLAUDE.md rule 4; strict specs; bad arguments rejected before any handler runs (`next_step` ignores arguments); every output is one JSON line of at most 320 characters, in English and Hindi | `engine/tests/test_tools.py`, `test_repair.py`, `eval/scenarios/test_engine_run.py` |
| The language model never ranks faults: a deterministic ledger (confidence-softened Bayes, value of information per cost, stop at top > 0.85 and second < 0.10) ranks them and picks every step | `engine/tests/test_ledger.py` (77 tests, thresholds exact, determinism) |
| Every power step goes through the safety gate: fresh camera supply reading (< 20 s, both confidences ≥ 0.9), voltage in band, limit set and ≤ max, battery removed; else the exact block reason and the value to set | `engine/tests/test_safety.py` (1512-case table + rules), `test_repair.py` |
| A power measurement never runs without a single-use gate approval; a barge-in cancels it; it never survives a restart | `test_tools.py`, `test_repair.py`, `test_ws.py`, `test_persistence.py` |
| A swollen, hot or smoking battery stops the session with an urgent spoken stop; non-phone devices are refused; current at the limit for > 3 s gives "disconnect now" | `test_safety.py`, `test_repair.py`, `test_ws.py` |
| Spoken readings are parsed (English, romanised Hindi, Devanagari, mixed), read back with the point, and count only after confirmation (0.7 → 0.85) | `test_spoken.py` (70), `test_repair.py` |
| All 7 fake-bench scenarios reach the scripted root cause through the 8 tools; glare and conflicting readings produce a re-check, never a confident wrong answer; camera loss gives degraded mode with no power steps | `eval/scenarios/test_engine_run.py` (camera/spoken × en/hi), `eval/REPORT-2026-11-06.md` §2 |
| "Fixed" is accepted only after the reading that pointed at the fault comes back healthy | `test_repair.py`, `test_engine_run.py` |
| A repair session is rebuilt from SQLite after a restart with exactly the same ledger and next step | `engine/tests/test_persistence.py` |
| In 10 spoken sessions (mini, push-to-talk, synthetic voices, fake bench) the voice agent reached the scripted root cause 10 times and closed 9 as fixed; it spoke no value that wasn't in a tool result and no power instruction without a gate approval | `eval/REPORT-2026-11-06.md` §3 (batches 13:52 and 14:04, 4 Nov) |
| A tool call goes browser → engine → browser over the WebSocket, and a crashing tool still answers | `engine/tests/test_ws.py` |
| Per-turn spans, connect stages, transcripts with language tags and response token usage are stored | `engine/tests/test_ws.py`, `test_telemetry.py`, `test_lang_and_migration.py` |
| The turn mode switches live without touching instructions, tools, transcription or the cap | `session.updated` after each switch, 4 Nov (`docs/NOTES.md`) |
| Push-to-talk: the reply is heard 1.14–1.20 s after the end of speech (p50; p95 ≤ 1.32 s), with no false cut-offs | `eval/REPORT-2026-11-04.md` §1: 45 synthetic lines per config, simulated pedal released 200 ms after speech |
| Server VAD cuts people off mid-sentence: 8–9 of 10 lines with a ~1 s pause | `eval/REPORT-2026-11-04.md` §1 |
| The local preamble clip is audible 83 ms (p50) after the model's tool call event, and 0.99 s after the end of speech with push-to-talk | `eval/REPORT-2026-11-04.md` §2 |
| The tool result starts the next response with no client-side wait (result → `response.created` ≈ one network round trip), once the page drops the SDK's `conversation.item.retrieve` (which stalled tool chains for 2–6 s) | `eval/REPORT-2026-11-04.md` §2; `eval/REPORT-2026-11-06.md` §5 (0 stalls after the fix) |
| The v2 prompt cuts replies longer than 6 s from 59% to 9–18% (mini) | `eval/REPORT-2026-11-04.md` §3 |
| With the secret pool, the page is ready 3.9 s after load (p50; p95 5.1 s), against 4.6 / 9.8 s without the pool | `eval/REPORT-2026-11-04.md` §6: 10 page loads each |
| The secret scan flags keys, client secrets, `.env`, `.claude/`, `data/` and `*.sqlite` | `scripts/tests/test_secret_scan.py`. **No git hook runs it** (`.githooks/` is gone and none is installed in `.git/hooks`), so nothing enforces it before a commit |

## Stubbed
| Claim | What is behind it today |
| --- | --- |
| `get_reading` / `watch_boot_current` read the supply, meter and boot current through the camera | The **fake bench**: scripted scenarios with rough placeholder numbers. No camera, no Reader A/B, no agreement check yet (Phase 3, CLAUDE.md rule 3) |
| Model data for a real phone | `packs/demo_model` is placeholder throughout (structure real, numbers invented) |
| Evidence server over MCP | In-process `EvidenceSource` behind the same interface; the MCP Python SDK is not installed yet |

## Assumed (or not met yet)
| Claim | Status |
| --- | --- |
| A reply within 0.8 s of the end of speech (p95 ≤ 1.2 s) | **Not met.** Best is push-to-talk at 1.14 s p50 (synthetic voices) |
| The spoken value within 3.0 s of the request | **Not met.** Best is 3.4 s p50 with a 1.5 s stand-in tool |
| Ready within 2 s of page load | **Not met.** 3.9 s p50; OpenAI's WebRTC setup alone takes about 3.8 s at the ~300 ms round trip from here |
| Hindi questions get Hindi replies | **Not met under v3.** In spoken Hinglish sessions 20 of 71 replies were Hindi/Hinglish (28%); English 28/30. v2 (no repair tools) got 26/33. Readings stay English values (`eval/REPORT-2026-11-06.md` §4) |
| The spoken value within 3.0 s of the request, under v3 | **Not met / thin data.** 3.3 s p50 on 4 lines before the API credits ran out (`eval/REPORT-2026-11-06.md` §6) |
| The last three engine fixes (user's "haan" confirms a read-back, Devanagari danda, bilingual `explain`) work in a voice session | Unit-tested only; the spoken rerun was blocked by exhausted API credits |
| Chrome's echo canceller covers the local preamble clip | By design (WebRTC loopback, same path as the model's voice); not tested acoustically |
| Synthetic-voice results carry over to real Hindi/Hinglish speech in a shop | Real recordings not yet made (`eval/intent_set/real/`) |
| A headset prevents echo barge-in | First manual session with a headset |
