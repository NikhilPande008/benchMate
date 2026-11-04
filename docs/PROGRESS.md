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
