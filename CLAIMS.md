# Claims

Every outward-facing claim about Benchmate, with its evidence. Updated at the end of each build phase.

**Proven** = measured or tested in this repo, with the evidence named. **Stubbed** = the interface exists but behind it is a placeholder. **Assumed** = believed, or a target, not yet shown.

## Proven
| Claim | Evidence |
| --- | --- |
| The OpenAI API key never reaches the browser; the browser gets a short-lived client secret | `engine/tests/test_client_secret.py` (the key never appears in a response); the SDK refuses non-`ek_` keys in browsers |
| Session settings: turn detection mode from a fixed list, reasoning `low`, `retention_ratio` 0.8 truncation with an 8000-token cap, `max_output_tokens` 220 | `engine/tests/test_client_secret.py` |
| At most 8 tools, from the fixed list; unknown arguments rejected; outputs are one short JSON line | `engine/tests/test_tools.py`, `test_ws.py` |
| A tool call goes browser → engine → browser over the WebSocket, and a crashing tool still answers | `engine/tests/test_ws.py` |
| Per-turn spans, connect stages, transcripts with language tags and response token usage are stored | `engine/tests/test_ws.py`, `test_telemetry.py`, `test_lang_and_migration.py` |
| The turn mode switches live without touching instructions, tools, transcription or the cap | `session.updated` after each switch, 4 Nov (`docs/NOTES.md`) |
| Push-to-talk: the reply is heard 1.14–1.20 s after the end of speech (p50; p95 ≤ 1.32 s), with no false cut-offs | `eval/REPORT-2026-11-04.md` §1: 45 synthetic lines per config, simulated pedal released 200 ms after speech |
| Server VAD cuts people off mid-sentence: 8–9 of 10 lines with a ~1 s pause | `eval/REPORT-2026-11-04.md` §1 |
| The local preamble clip is audible 83 ms (p50) after the model's tool call event, and 0.99 s after the end of speech with push-to-talk | `eval/REPORT-2026-11-04.md` §2 |
| The tool result starts the next response with no client-side wait (result → `response.created` ≈ one network round trip) | `eval/REPORT-2026-11-04.md` §2; SDK sequencer code |
| The v2 prompt cuts replies longer than 6 s from 59% to 9–18% (mini) | `eval/REPORT-2026-11-04.md` §3 |
| With the secret pool, the page is ready 3.9 s after load (p50; p95 5.1 s), against 4.6 / 9.8 s without the pool | `eval/REPORT-2026-11-04.md` §6: 10 page loads each |
| The secret scan flags keys, client secrets, `.env`, `.claude/`, `data/` and `*.sqlite` | `scripts/tests/test_secret_scan.py`. **The hooks that run it (`.githooks/`) and `.git/` were removed on 3 Nov**, so nothing enforces it until they are restored |

## Stubbed
| Claim | What is behind it today |
| --- | --- |
| `get_reading` reads the supply or the meter | Fixed placeholder values after 1.5 s. No camera, no Reader A/B yet (Phase 3) |
| Diagnosis, safety gate, spoken readings | Not built (Phase 2). The prompt tells the model it cannot diagnose or give power steps |

## Assumed (or not met yet)
| Claim | Status |
| --- | --- |
| A reply within 0.8 s of the end of speech (p95 ≤ 1.2 s) | **Not met.** Best is push-to-talk at 1.14 s p50 (synthetic voices) |
| The spoken value within 3.0 s of the request | **Not met.** Best is 3.4 s p50 with a 1.5 s stand-in tool |
| Ready within 2 s of page load | **Not met.** 3.9 s p50; OpenAI's WebRTC setup alone takes about 3.8 s at the ~300 ms round trip from here |
| Hindi questions get Hindi replies | **Partly.** With the v2 prompt, 12/15 Hindi and 14/18 Hinglish plain questions (synthetic voices); readings come back as English values |
| Chrome's echo canceller covers the local preamble clip | By design (WebRTC loopback, same path as the model's voice); not tested acoustically |
| Synthetic-voice results carry over to real Hindi/Hinglish speech in a shop | Real recordings not yet made (`eval/intent_set/real/`) |
| A headset prevents echo barge-in | First manual session with a headset |
