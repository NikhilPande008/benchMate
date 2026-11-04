# Progress log

One entry per finished step, newest last. A new session resumes from here: read the last
entry, run `uv run pytest`, then continue with the next step of the phase prompt.

Phase 2b steps (finishing Phase 2): 1 green suite · 2 untested parts (checkpoint) · 3 pack rule
· 4 code defaults · 5 prompt v3 · 6 dev endpoint · 7 browser work · 8 end-to-end spoken runs ·
9 report and wrap-up.

Note: the phase prompt names `PROMPT_PHASE_2.md`, but that file is not in the repo. The Phase 2
spec used here is the one pasted into the 3 Nov session (packs, ledger, safety gate, 8 tools,
fake bench, spoken parser, prompt v3, end-to-end runs, `eval/REPORT-2026-11-06.md`).

---

## 2026-11-04 · Phase 2b step 1: suite green again

- `engine/tests/test_tools.py` rewritten for the 8-tool router (39 tests):
  - fixed tool list and order; strict specs (no title, default, anyOf or $ref);
  - only real choices are required;
  - browser timeouts outlast the engine's;
  - bad args are rejected before any handler runs;
  - every tool's happy path is one JSON line ≤ 320 chars, Hindi too;
  - router timeout; the vision timeout inside the tool budget;
  - a stopped session refuses everything except explain and close_repair;
  - power measurements need a gate approval before the bench is touched.
- `engine/tests/test_ws.py`: Phase 1 dummy-tool assertions replaced. New socket tests:
  - a repair end to end;
  - scenario chosen in the hello, switched by `{"type":"dev"}` (unknown names rejected);
  - barge-in cancels a pending approval;
  - battery danger injects an urgent stop;
  - a `session.lang` event fires after two Hindi turns.
- `engine/tests/test_client_secret.py`: the session now carries all 8 tools.
- Fake bench delays scale with `fake_time_scale` (`BENCHMATE_FAKE_TIME_SCALE`; tests use 0).
- The WebSocket keeps the bench on the connection. Late UI events after close are dropped, not
  raised.
- Test count: **1857 passed in 1.3 s**.
- Done ahead of step 2 because the socket test needed it: evidence rows store `seq` and
  `ledger_use`, there is a `repair_state` table, and `RepairSession.restore()` exists. Step 2
  adds its own tests.

## 2026-11-04 · Phase 2b step 2: tests for the untested parts (checkpoint)

New tests:
- `engine/tests/test_repair.py` (51): every RepairSession operation's happy path, plus:
  - read-back before the ledger; a powered observation needs the approval at confirm;
  - gate blocks (battery, no supply, stale); approvals single-use and per step;
  - barge-in cancels; at-limit watchdog → urgent disconnect, stops early; "ruko" cancels a watch;
  - battery danger → stop and inject; reassurance doesn't stop;
  - explain; the fix check; degraded camera mode.
- `eval/scenarios/engine_run.py` is a scripted technician that drives the 8 tools against the
  fake bench. It is also a CLI that prints the scenario table. `eval/scenarios/test_engine_run.py`
  (34 tests) runs all 7 scenarios × {camera, spoken} × {en, hi}:
  - fault scenarios reach the scripted root cause and close as fixed;
  - glare → read aloud, no unclear camera reading ranked;
  - conflicting → exactly one re-measure before the fix, and the slipped reading superseded;
  - camera down → degraded event, readings aloud, no power approved;
  - vision timeout → read-aloud fallback;
  - every power measurement follows a gate approval.
- `engine/tests/test_language.py` (19): the two-consecutive-turns rule, Hindi and Hinglish
  grouped, untagged turns ignored, `session.lang` only on change.
- `engine/tests/test_persistence.py` (7): rebuilt from SQLite after a store restart, the session
  has:
  - identical evidence and order;
  - the exact same posterior, history, stop and conflicts;
  - the same summary and the same next step.

  Also: stored snapshots equal the recomputed ledger; power approvals don't survive a restart;
  a WebSocket reconnect after an engine restart resumes the repair.
- `test_ledger.py` (+4) for the narrowed conflict rule; `test_packs.py` (+3) for the healthy class.

Engine changes found by these tests:
- **Ledger conflict rule** (`engine/ledger/ledger.py`): a `leader_drop` needs an earlier reading
  to blame. A leader set only by priors no longer counts, and a re-measure the engine asked for
  (`Observation.remeasure`) settles the conflict instead of raising a new one. `other_high` is
  unchanged. Before this, the first informative reading always flagged a conflict, and
  `conflicting_reading` took two re-checks instead of one.
- **Fix check:** readings after a fix is proposed never move the diagnosis. The fix names the
  reading that must recover (`then_check`), and `close_repair("fixed")` needs that reading to
  come back healthy at ≥ 0.85. The old rule ("any healthy reading") passed not-charging fixes on
  a NORMAL_BOOT that was normal before the fix.
- **Pack:** `Measurement.healthy` (defaults by kind; observations must name it). The JSON
  Schemas were re-exported.
- **Degraded mode:** camera unavailable → `degraded` event, and `next_step` offers no power
  steps (the gate needs a camera supply read). If only a powered check could separate the faults,
  the result is "unresolved", not a guess.
- **Persistence:**
  - evidence rows carry `seq` and `ledger_use`;
  - a new `repair_state` table;
  - `RepairSession.restore()` replays stored readings;
  - the WebSocket restores a stored session on hello (`ready.restored`);
  - approvals and read-backs are never restored.
- **Fake bench:** `apply_fix()` (the board reads healthy afterwards) and `fake_time_scale`.
- **UI events:** `RepairSession._event` keeps a reference to each send task and is safe outside
  the event loop.
- **Flaky test fixed:** the socket tests read SQLite before the writer had flushed
  (`client.close()` doesn't run lifespan shutdown). They now call `conftest.flush_telemetry`.

Scenario table (camera readings, `uv run python eval/scenarios/engine_run.py`):

| scenario | true fault | found | outcome | steps | re-checks | read aloud | degraded |
|---|---|---|---|---|---|---|---|
| short_battery_line | vbat_cap_short | vbat_cap_short | fixed | 2 | 0 | 0 | - |
| no_current | open_vbat_path | open_vbat_path | fixed | 1 | 0 | 0 | - |
| not_charging | charge_path_part | charge_path_part | fixed | 2 | 0 | 0 | - |
| glare_low_confidence | vbat_cap_short | vbat_cap_short | fixed | 2 | 0 | 2 | - |
| conflicting_reading | power_key_line | power_key_line | fixed | 5 | 1 | 0 | - |
| camera_unavailable | open_vbat_path | open_vbat_path | fixed | 2 | 0 | 3 | camera_unavailable |
| vision_timeout | open_vbat_path | open_vbat_path | fixed | 2 | 0 | 3 | vision_timeout |

Test count: **1968 passed in 3.8 s** (5 full runs clean after the flush fix).

Open:
- The fake-bench numbers are rough placeholders. These results show the engine logic works;
  they say nothing about real boards.
- A failed fix check returns `fix_check: fail` and refuses "fixed". It does not yet send the
  ledger back to probing.
- Next: step 3 (write down the pack rule). Waiting for the user to commit and say "continue".
  (Committed by the user as `f9bb8ad`.)

## 2026-11-04 · Phase 2b step 3: pack completeness rule written down

- The rule is now in:
  - the docstring at the top of `mcp_evidence/packs/schema.py`;
  - a `$comment` at the top of `packs/schema/pack.v1.schema.json` (from `scripts/export_schemas.py`);
  - `packs/README.md` (rewritten: schema, placeholders, healthy class, the rule);
  - a new Phase 2 section in `docs/NOTES.md`.
- Tests (+3, `engine/tests/test_packs.py`):
  - `test_only_port_current_may_be_left_without_a_prediction`: the rule beyond points; the one
    deliberate gap is port current for dead-phone faults.
  - `test_a_gap_in_the_predictions_picks_the_wrong_fault`: removing `power_key_line`'s normal
    predictions makes five normal readings on a power-key board lead to `bad_boot_cable` (0.61).
- Test count: **1971 passed in 3.8 s**.
- Open: nothing.

## 2026-11-04 · Phase 2b step 4: code defaults

- `engine/config.py`:
  - `turn_mode` defaults to `"ptt"`;
  - new `default_model="mini"` (`BENCHMATE_DEFAULT_MODEL`), so a request without a model gets
    `gpt-realtime-2.1-mini`;
  - the pool pre-warms that config.
- Unchanged, already as decided: the talk key defaults to `Space` (a page setting, `?ptt_key=`, or
  remembered from the box); the local preamble clip (`v2_local`, which becomes `v3_local` after
  step 8); `max_output_tokens=220`. Semantic VAD stays in the page's turn-mode menu and
  `?turn_mode=`.
- `eval/latency_script/run_latency.py` (the fake-WAV runner) now passes
  `turn_mode=semantic_medium`, because it needs VAD to end turns.
- `.env.example`: the new defaults, plus the fake-bench, vision-timeout and dev-endpoint variables.
  The local `.env` sets none of the turn-mode, prompt or model keys, so the code defaults apply.
- Tests:
  - `test_decided_defaults`, `test_flagship_on_request_and_by_config` and
    `test_semantic_vad_stays_selectable` are new;
  - `test_default_model_is_flagship` is replaced;
  - the main client-secret test now expects push-to-talk.
- Test count: **1973 passed in 4.1 s**.
- Open: `CLAUDE.md` on disk still lists "semantic_vad, eagerness medium; push-to-talk fallback".
  The user's 4 Nov decision (push-to-talk default, mini, local clip, v2_local) isn't written
  there. I left the file for the user to change.

## 2026-11-04 · Phase 2b step 5: prompt v3

- `engine/session/prompts/realtime_v3_local.md` (local preamble clip) and `realtime_v3.md`
  (spoken "Checking." / "Ek second." before get_reading and watch_boot_current only). They are
  identical outside Preambles, and a test checks that.
- Kept from v2: one sentence ≤ 12 words, one instruction at a time, three English and three Hindi
  examples, the latest-turn language rule, the Unclear Audio section. New: a reading plus the next
  instruction may take two short sentences.
- New sections and rules:
  - **Tools**: one bullet per tool saying when to use it, and the flow: next_step after every
    recorded reading; for "power", supply read then request_power_step; read-back then confirm;
    watch after the cable is connected; close only after fix_check "pass".
  - **Language** (§7): Hinglish for mixed turns; numbers, units and trade terms in English; three
    Hinglish reading examples, including "Point three five volt, yeh normal hai."
  - Never state a value, class, fault or probability that isn't in a tool result.
  - Power instructions only with request_power_step's exact sentence (Tools and Safety).
  - **Engine Messages**: "[engine]" lines come from the engine; SAFETY STOP is said word for word.
- More transcription keywords: boot cable, OL, capacitor.
- Length: 565 → 1058 words (v2_local → v3_local).
- Tests (+13): all 8 tools covered; the language rules; values and power only from tool results;
  engine messages; the variants differ only in Preambles.
- The default is still `v2_local`. It becomes `v3_local` once the step 8 runs pass.
- Test count: **1991 passed in 3.9 s**.

## 2026-11-04 · Phase 2b step 6: engine dev endpoint

- `engine/api/dev.py` (on with `BENCHMATE_DEV_ENDPOINTS`, default 1; the engine binds to
  127.0.0.1):
  - `GET /dev/scenarios`: names, symptom, true fault, robustness flags, default.
  - `GET /dev/sessions/{id}?since=n`: live repair summary, scenario, connected flag, the last 50
    engine events, and a `next_event` cursor. The end-to-end driver polls this.
  - `POST /dev/sessions/{id}/scenario`: switches a live connection's fake bench.
- `engine/api/ws.py`:
  - `Connections` registry (`app.state.connections`, newest 50 kept);
  - `Connection.set_scenario`;
  - the `{"type":"dev"}` message is honoured only with dev endpoints on.
- The browser config (`/realtime/client-secret` → `config`) now carries `tool_timeouts_ms`
  (engine timeout + 2 s per tool: get_reading 9 s, watch 14 s, others 5 s) and
  `dev: {scenarios, scenario}` (null when dev endpoints are off).
- **Engine notes** (`RepairSession.note`): `{"type":"inject","urgent":bool,"text":"[engine] …"}`.
  - Urgent: the safety stop.
  - Non-urgent: camera lost or back, and a session resumed after a reconnect.
  - The browser handles them in step 7. Urgent notes interrupt and get a reply; non-urgent ones
    are context for the next turn.
- Tests: `engine/tests/test_dev.py` (6). The persistence reconnect test now also checks the
  resume note.
- Test count: **1997 passed in 4.0 s**.

## 2026-11-04 · Phase 2b step 7: browser work

Checked against the installed `openai` 7.47.0 types: a mid-session engine note is a
`conversation.item.create` with
`{type:"message", role:"system", content:[{type:"input_text", text}]}`. Instructions and tools are
never re-sent (rule 7).

- `apps/web/src/realtime/engine.ts`:
  - `scenario` in the hello, tracked from `ready` and `dev.scenario`;
  - `inject` messages go to a note handler and are queued until the voice session exists;
  - `bargeIn()` sends `{"type":"event","name":"barge_in"}`; `devScenario()`.
- `apps/web/src/realtime/voice.ts`:
  - **Per-tool SDK timeouts** from `config.tool_timeouts_ms` (was a flat 8 s; get_reading is
    now 9 s, the boot watch 14 s).
  - **Engine notes:** urgent → `session.interrupt()` if busy, then the system item, then
    `response.create`. Non-urgent → the system item only (context for the next turn).
  - **Barge-in:** on push-to-talk key-down, or VAD `speech_started`, while the agent is speaking
    or a response is in flight. The engine then cancels any pending power approval.
  - **Preamble clip** speaks the engine's `session.lang` (`state.sessionLang`), falling back to
    the turn's tag before the first one. It plays at most once per user turn, so chained tool
    calls don't repeat it.
  - Handle: `scenario()`, `setScenario()`.
- `apps/web/src/App.tsx`: a "Fake bench" scenario picker in the dev controls (`?scenario=`
  also works); a small ledger panel (top 3, current step or fix, from `ledger.updated`,
  `step.proposed`, `fix.proposed`); readable engine log lines.
- Engine side, for tracing the voice runs: `tool_started` spans carry the args and
  `tool_finished` spans the one-line output; `POST /dev/sessions/{id}/fix` applies the fix on
  the fake bench. Tests (+2, `test_dev.py`).
- `npm run typecheck` and `npm run build` pass. The browser behaviour has no TypeScript unit
  tests; step 8's end-to-end runs exercise it.
- Test count: **1999 passed in 4.0 s**.

## 2026-11-04 · Phase 2b step 8: end-to-end spoken runs

- `eval/scenarios/voice_run.py`:
  - push-to-talk, mini, v3_local, Chrome with an injected microphone;
  - a scripted technician reads `GET /dev/sessions/{id}` after each reply and plays the next line
    (macOS `say`, Rishi; clips cached in `eval/scenarios/audio/`, git-ignored);
  - stored analysis: root cause, steps, turns, tool calls, re-checks, model tool errors, values not
    found in any tool output, power lines vs gate approvals, reply language, stalls.
- The first runs found 9 bugs, each fixed with tests before the next run. Details are in
  `docs/NOTES.md` "Phase 2b" and `eval/REPORT-2026-11-06.md` §5:
  - the SDK's `conversation.item.retrieve` stalled tool chains for 2–6 s (the page now drops it);
  - "आगे" matched "आग" (fire), causing a false safety stop;
  - the model gave a power instruction without the gate (next_step now carries the gate's
    answer; request_power_step reads the supply itself);
  - point ids that were invented, stale or wrong; arguments sent to next_step; an approval spent
    on the wrong check; a missing phone model;
  - observation phrasing ("Nothing is getting hot" vs "nothing hot");
  - a hot capacitor treated as a danger;
  - a read-back loop.
- Last two complete batches (13:52 and 14:04): **10/10 sessions reached the scripted root cause,
  9/10 closed as fixed.** In both batches: no unsupported values, no unapproved power lines,
  0 stalls. glare 14:04 got stuck in a read-back loop, now fixed in the engine and unit-tested.
- No real clips exist in `eval/intent_set/real/`, so there is no real-speech run.
- `v3_local` is now the default prompt (`engine/config.py`, `CURRENT_PROMPT`, `.env.example`).
- **Blocked:** at 14:19 the OpenAI account ran out of credits (`/v1/realtime/calls` returns 429
  "You have no credits remaining"). The final batch on the final code did not run, so three engine
  changes are unit-tested only:
  - the user's "haan" confirms a read-back;
  - the Devanagari danda in word matching;
  - bilingual `explain`.
- Test count: **2036 passed in 4.0 s**.

## 2026-11-04 · Phase 2b step 9: report and wrap-up

- `eval/REPORT-2026-11-06.md`: test counts per module, the 7 engine-level scenarios, the 10 spoken
  sessions with wrong turns, language under v3, the bugs found, and the latency spot-check.
- **The latency spot-check is incomplete** (credits): only about 13 of 45 lines per config.
  Medians are within 0.1–0.15 s of 4 Nov, at n = 4–9.
- `CLAIMS.md` (Proven / Stubbed / Assumed), `docs/NOTES.md`, `README.md` updated.
- Open, for the next session:
  1. Add API credits, then rerun `uv run python eval/scenarios/voice_run.py --start-engine` and
     `uv run python eval/latency_script/run_experiments.py --start-engine --lanes 2 --configs v2_b_ptt v3_b_ptt`.
     Fill the numbers into the report's §3, §4 and §6.
  2. Hindi replies under v3 are poor (28% of replies in Hinglish sessions). Needs a prompt
     iteration and real recordings.
  3. `CLAUDE.md` "Settings that are already decided" is still the pre-4 Nov text.
  4. No git hook runs `scripts/secret_scan.py`.
  5. A failed fix check refuses "fixed" but doesn't send the repair back to probing.
