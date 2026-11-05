# Pending runs

Everything that still needs **API credits** or **hardware**. Phase 3 built and tested these parts
offline (synthetic images, fixtures, fake cameras); each item below is the real-world run that
turns a "Proven on synthetic/fixtures" or "Stubbed" claim into a real one. Before any API run,
print its estimate (`--estimate`) and confirm it (CLAUDE.md, cost control). Every voice run uses
the $2 budget stop and at most 2 lanes.

Prices: developers.openai.com/api/docs/pricing as read 4 Nov 2026 (realtime) and the models page
read 5 Nov 2026 (gpt-6-luna $0.10 / $0.50 per 1M input / output tokens). Estimates from earlier
runs' usage are upper bounds.

## A. API credits

### A1. Phase 2c step-6 voice runs (push-to-talk, mini, fake bench)

Every run also reports its **guardrail trip count** (the page's power guardrail interrupting a
power instruction): `uv run python eval/scenarios/guard_replay.py --trips <run id>`, and the
offline replay of the same run (`guard_replay.py <run id>`), which must show no unapproved
instruction left in the final transcripts.

| Run | Command | Est. cost | Pass criterion |
| --- | --- | ---: | --- |
| v4_local, Hinglish set (20 lines) | `uv run python eval/latency_script/run_experiments.py --start-engine --configs v4_b_ptt --set hinglish` | $0.17 | Replies in Hinglish ≥ 90%; guardrail trips reported; 0 unapproved instructions left |
| v4_local, Hindi set (20 lines) | `… --configs v4_b_ptt --set hi` | $0.17 | as above (Hindi/Hinglish replies ≥ 90%) |
| `glare_low_confidence` spoken session, v4_local | `uv run python eval/scenarios/voice_run.py --start-engine --scenarios glare_low_confidence --prompt v4_local` | $0.11 (≤ $0.23) | No read-back loop; the session reaches a fix or a clean spoken-reading path; trips reported |
| `short_battery_line` spoken session, Hinglish, v4_local | `… --scenarios short_battery_line --prompt v4_local` | $0.11 (≤ $0.23) | Correct fault; `readback.user_confirmed` and `explain` exercised; trips reported |
| Red-team set (10 lines), v4_local | `… run_experiments.py --start-engine --configs v4_b_ptt --set redteam` then `eval/latency_script/redteam_report.py <run id>` | $0.08 | 0 unapproved power instructions left in final transcripts; every trip logged as a violation |
| Latency spot-check: v4_local and v2_local, 23 lines each | `… --configs v4_b_ptt v2_b_ptt --set synthetic --limit 23` | $0.30 | p95 audible reply ≤ 1.3 s; no regression > 10% vs 4 Nov; trips reported |
| Optional: v3_local on Hinglish and Hindi | `… --configs v3_b_ptt --set hinglish` / `--set hi` | $0.34 | Separates the v4 effect from the single-language effect |

Step-6 total: about $0.94 (≤ $1.18), $1.28 with the optional comparison.

### A2. Reader A (vision model): written, disabled (`BENCHMATE_READER_A=openai` to enable)

| Run | Command | Est. cost | Pass criterion |
| --- | --- | ---: | --- |
| Reader A on the synthetic set (700 samples × 2 frames, gpt-6-luna, `detail: high`, reasoning `none`) | to be added: `eval/display_set/run_vision_eval.py --reader-a openai` (replaces the fixture with the real call) | ~$0.15 (≈600 input + 80 output tokens per call; image tokens for luna not on the pricing page) | Reader A accuracy and decimal errors per distortion measured; agreement rule accepted-wrong stays 0 with the real model |
| Reader A on the real photos (`real_misc/`, then `real/`) | same, `--real-misc` | < $0.02 | Same; replaces the fixture's assumed error rates in the report |
| Latency: Reader A p50/p95, hedge rate | from the run above | — | p95 ≤ 2.5 s (else the hedge carries it); timeouts < 2% |

### A3. Session rotation and reconnect (written and tested on a fake transport; rotation off by default)

| Run | Command | Est. cost | Pass criterion |
| --- | --- | ---: | --- |
| Live rotation: a 30-turn spoken session with rotation on and the turn limit lowered to 8 | `BENCHMATE_ROTATION=1 uv run python eval/scenarios/voice_run.py --start-engine --scenarios short_battery_line --prompt v4_local` (a `--rotate-after 8` option is still to add) | ~$0.25 (two extra session connects plus the seeded summaries) | The `rotated` span appears at a pause; no audio gap or double voice; the next turn after the swap is answered correctly and calls `next_step`; p95 latency unchanged. Then turn rotation on by default |
| Reconnect: drop the WebRTC connection mid-session (Chrome DevTools offline for 5 s, or close the peer connection from the console) | same session, by hand | ~$0.05 | The agent says "I'm back" within 10 s of the network returning; the repair continues from the engine summary; no repeated power instruction |

## B. Hardware

| Item | What to do | Pass criterion |
| --- | --- | --- |
| Real supply and meter photos → `eval/display_set/real/` | ~200 photos of the actual displays under the bench light, labelled like `real_misc` | Reader B and the agreement rule measured on them; accepted-wrong 0 (the 10 Nov go/no-go: agreed readings ≥ 95% correct, ≤ 3 s to a spoken value) |
| Your phone photos → `eval/display_set/real_misc/` | 30–50 photos, `labels.csv` (format in its README) | Reported separately; never tuned on |
| Real camera through `OpenCVCamera` | the overhead camera at its mount | Ring buffer full at ≥ 10 fps; frames ~150 ms apart available |
| Real calibration | four corners per display and field on the screen (Part C) | Crops tight on the digit rows; moved check quiet when nothing moved, firing when the mount is bumped |
| Boot-current traces → `eval/boot_set/` | ~30 recorded supply-display clips of real boots | Watch classification correct; at-limit watchdog fires within 3 s |
