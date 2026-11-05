"""Red-team report for the power guardrail (CLAUDE.md rule 2 backstop).

    uv run python eval/latency_script/redteam_report.py x-20261104-1500-v4_b_ptt-redteam-mini

For every red-team line: what the agent said (final transcripts, which are cut short when the
guardrail interrupts), the guardrail trips, the gate answers in that turn, and whether any spoken
text still holds a power instruction the gate did not approve, checked with the page's own matcher
(apps/web/src/realtime/powerGuard.ts, run in node).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from engine.config import Settings  # noqa: E402
from engine.telemetry import load_spans, load_transcripts, make_engine  # noqa: E402

WEB = REPO / "apps" / "web"

NODE_CHECK = """
import { findViolation } from './src/realtime/powerGuard.ts';
const items = JSON.parse(process.argv[1]);
console.log(JSON.stringify(items.map(([text, approved, gate]) => findViolation(text, gate))));
"""


def check(items: list[tuple[str, list[str], list[str]]]) -> list[dict | None]:
    run = subprocess.run(["node", "--input-type=module", "-e", NODE_CHECK, json.dumps(items)], cwd=WEB,
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(run.stdout)


def report(run_id: str) -> list[dict]:
    db = make_engine(Settings.from_env().db_path)
    spans = load_spans(db, run_id)
    trans = load_transcripts(db, run_id)
    starts = [(s.t_ms, json.loads(s.attrs or "{}").get("id")) for s in spans if s.name == "line_start"]
    rows = []
    for i, (t0, line_id) in enumerate(starts):
        t1 = starts[i + 1][0] if i + 1 < len(starts) else float("inf")
        window = [s for s in spans if t0 <= s.t_ms < t1]
        said = [t.text for t in trans if t.role == "assistant" and t0 <= t.t_ms < t1]
        trips = [json.loads(s.attrs or "{}") for s in window if s.name == "safety_violation" and s.source == "browser"]
        approved, gate = [], []
        for s in window:
            a = json.loads(s.attrs or "{}")
            if s.name == "tool_finished" and a.get("name") in ("request_power_step", "next_step") and a.get("output"):
                o = json.loads(a["output"])
                if o.get("approved"):
                    approved.append(o.get("power_step") or o.get("step"))
                gate += list((o.get("say") or {}).values()) if isinstance(o.get("say"), dict) else []
        rows.append({"line": line_id, "said": said, "trips": trips, "approved": approved, "gate": gate})
    flat = [(text, r["approved"], r["gate"]) for r in rows for text in r["said"]]
    verdicts = iter(check(flat)) if flat else iter([])
    for r in rows:
        r["uncaught"] = [v for v in (next(verdicts) for _ in r["said"]) if v]
    return rows


def main() -> None:
    rows = report(sys.argv[1])
    for r in rows:
        print(f"{r['line']}: trips={len(r['trips'])} uncaught={len(r['uncaught'])} approved={r['approved']}")
        for t in r["trips"]:
            print(f"    tripped: {t.get('kind')}: {t.get('sentence')}")
        for s in r["said"]:
            print(f"    said: {s}")
    total_trips = sum(len(r["trips"]) for r in rows)
    uncaught = sum(len(r["uncaught"]) for r in rows)
    print(f"\n{len(rows)} lines, {total_trips} guardrail trips, {uncaught} unapproved instructions left in final transcripts")


if __name__ == "__main__":
    main()
