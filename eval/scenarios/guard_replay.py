"""Offline replay of the power guardrail over logged spoken runs (CLAUDE.md rule 2 backstop).

    uv run python eval/scenarios/guard_replay.py                     # every e2e-* run in the telemetry DB
    uv run python eval/scenarios/guard_replay.py e2e-20261104-1330 e2e-20261104-1334

No API calls. For each session it rebuilds the timeline from telemetry (the gate's tool results,
supply readings, user turns and transcripts, barge-ins, measurements that used an approval, and
what the agent said), then:

1. runs the engine's approval lifetime (engine/safety/approval.py) on it to get the endings only
   the engine knows (user confirmation, measurement started, supply settings changed);
2. feeds the timeline to the page's own guardrail (apps/web/src/realtime/powerGuard.ts, in node)
   on a replayed clock, and reports every sentence it would have interrupted.

Two rules are compared: "lifetime" (the current rule) and "turn" (the Phase 2c rule, where every
user turn ended an approval).
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from engine.config import Settings  # noqa: E402
from engine.safety.approval import Approvals  # noqa: E402
from engine.telemetry import load_spans, load_transcripts, make_engine  # noqa: E402

WEB = REPO / "apps" / "web"
GATE_TOOLS = ("request_power_step", "next_step")
USES_APPROVAL = {("watch_boot_current", "on_connect"): "connect_boot_cable", ("watch_boot_current", "on_power_press"): "press_power_key",
                 ("get_reading", "port_current"): "connect_port_supply"}
ENGINE_ONLY = ("confirmed", "used", "supply_changed", "violation", "stopped")  # the page handles the rest itself

NODE = """
import { PowerApprovals } from './src/realtime/powerGuard.ts';
const { events, mode } = JSON.parse(process.argv[1]);
const clock = { ms: 0 };
const g = new PowerApprovals(() => clock.ms);
const trips = [];
for (const e of events) {
  clock.ms = e.t;
  if (e.type === 'tool') g.noteToolOutput(e.tool, e.output);
  else if (e.type === 'ended') g.onEngineEvent({ event: 'power.ended', id: null, reason: e.reason });
  else if (e.type === 'turn') {
    g.userTurn(e.barge_in);
    if (mode === 'turn') g.end('barge_in');  // the old rule: any user turn ended it
  } else if (e.type === 'say') {
    const v = g.check(e.text);
    if (v) trips.push({ t: e.t, kind: v.kind, sentence: v.sentence, text: e.text, live: g.current?.step ?? null });
  }
}
console.log(JSON.stringify(trips));
"""


def guard(events: list[dict], mode: str) -> list[dict]:
    out = subprocess.run(["node", "--input-type=module", "-e", NODE, json.dumps({"events": events, "mode": mode})],
                         cwd=WEB, capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def _attrs(s) -> dict:
    return json.loads(s.attrs or "{}")


def timeline(spans, transcripts) -> list[dict]:
    """Ordered events of one session, in the shape the node guard replay takes (t in ms)."""
    ev: list[dict] = []
    for s in spans:
        a = _attrs(s)
        if s.source == "browser" and s.name in ("ptt_down", "speech_started"):
            ev.append({"t": s.t_ms, "type": "turn", "barge_in": False})
        elif s.source == "browser" and s.name == "barge_in":
            ev.append({"t": s.t_ms, "type": "barge"})
        elif s.source == "engine" and s.name == "tool_started":
            args = a.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            key = (a.get("name"), args.get("trigger", "on_connect") if a.get("name") == "watch_boot_current" else args.get("kind"))
            if key in USES_APPROVAL:
                ev.append({"t": s.t_ms, "type": "use", "step": USES_APPROVAL[key]})
        elif s.source == "engine" and s.name == "tool_finished" and a.get("output"):
            ev.append({"t": s.t_ms, "type": "tool", "tool": a.get("name"), "output": a["output"]})
    for t in transcripts:
        ev.append({"t": t.t_ms, "type": "say" if t.role == "assistant" else "user", "text": t.text})
    ev.sort(key=lambda e: (e["t"], e["type"] != "tool"))
    # a barge-in span marks the turn it starts as a barge-in
    for i, e in enumerate(ev):
        if e["type"] == "barge":
            nxt = next((x for x in ev[i + 1 :] if x["type"] == "turn" and x["t"] - e["t"] < 50), None)
            if nxt:
                nxt["barge_in"] = True
            else:
                e.update(type="turn", barge_in=True)
    return [e for e in ev if e["type"] != "barge"]


def engine_endings(events: list[dict]) -> list[dict]:
    """The approval lifetime the engine runs, on the replayed clock: adds the endings only the
    engine knows (as "ended" events) to the timeline."""
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = lambda ms: t0 + timedelta(milliseconds=ms)  # noqa: E731
    a = Approvals()
    supply: tuple[float | None, float | None] = (None, None)
    out: list[dict] = []
    for e in events:
        now = clock(e["t"])
        before = len(a.ended)
        a.expire(now)
        if e["type"] == "tool":
            o = json.loads(e["output"]) if e["output"].startswith("{") else {}
            if e["tool"] == "get_reading" and o.get("kind") == "supply":
                supply = (o.get("volts"), o.get("limit_a"))
                a.on_supply(supply)
            elif e["tool"] in GATE_TOOLS and o.get("approved") is True:
                a.grant(o.get("power_step") or o.get("step"), supply, now)
        elif e["type"] == "user":
            a.confirm(e["text"], now)
        elif e["type"] == "turn" and e["barge_in"]:
            a.end("barge_in")
        elif e["type"] == "use":
            a.consume(e["step"], now)
        out.append(e)
        out += [{"t": e["t"], "type": "ended", "reason": r, "step": ap.step} for ap, r in a.ended[before:] if r in ENGINE_ONLY]
    return out


def replay_session(spans, transcripts, mode: str = "lifetime") -> list[dict]:
    return guard(engine_endings(timeline(spans, transcripts)), mode)


def replay_run(run_id: str, db=None) -> dict[str, dict[str, list[dict]]]:
    db = db or make_engine(Settings.from_env().db_path)
    spans, trans = load_spans(db, run_id), load_transcripts(db, run_id)
    out: dict[str, dict[str, list[dict]]] = {}
    for sid in sorted({s.session_id for s in spans if s.session_id}):
        ss = [s for s in spans if s.session_id == sid]
        tt = [t for t in trans if t.session_id == sid]
        out[sid] = {mode: replay_session(ss, tt, mode) for mode in ("lifetime", "turn")}
    return out


def main() -> None:
    db = make_engine(Settings.from_env().db_path)
    runs = sys.argv[1:]
    if not runs:
        import sqlalchemy as sa  # comes with sqlmodel

        with db.connect() as c:
            runs = [r[0] for r in c.execute(sa.text("select distinct run_id from spans where run_id like 'e2e-%' order by run_id"))]
    totals = {"lifetime": 0, "turn": 0}
    for run in runs:
        for sid, res in replay_run(run, db).items():
            for mode in ("lifetime", "turn"):
                totals[mode] += len(res[mode])
            if not (res["lifetime"] or res["turn"]):
                continue
            print(f"{run} {sid}: lifetime {len(res['lifetime'])}, turn {len(res['turn'])}")
            turn = {(x["t"], x["sentence"]) for x in res["turn"]}
            for x in res["lifetime"]:
                why = f"paraphrase while {x['live']} was approved" if x["live"] else "no live approval"
                print(f"    [{'both rules' if (x['t'], x['sentence']) in turn else 'lifetime rule only'}] {x['kind']}: {x['sentence']}  ({why})")
            life = {(x["t"], x["sentence"]) for x in res["lifetime"]}
            for x in res["turn"]:
                if (x["t"], x["sentence"]) not in life:
                    print(f"    [per-turn rule only] {x['kind']}: {x['sentence']}")
    print(f"\n{len(runs)} runs; sentences the guardrail would interrupt: lifetime rule {totals['lifetime']}, per-turn rule {totals['turn']}")


if __name__ == "__main__":
    main()
