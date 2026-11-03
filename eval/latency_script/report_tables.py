"""Markdown tables for eval/REPORT-*.md from stored experiment runs.

    uv run python eval/latency_script/report_tables.py runs/x-20261104-b1 runs/x-20261104-b2 runs/connect-...

Every number is p50 / p95 (n), in milliseconds unless stated.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from engine.telemetry import percentile  # noqa: E402

HERE = Path(__file__).parent


def cell(vals: list[float | None]) -> str:
    v = [x for x in vals if x is not None]
    if not v:
        return "–"
    return f"{percentile(v, 50):.0f} / {percentile(v, 95):.0f} ({len(v)})"


def load(dirs: list[str]) -> dict[str, dict]:
    runs = {}
    for d in dirs:
        for f in sorted((HERE / d if not Path(d).is_absolute() else Path(d)).glob("*.json")):
            if f.name != "summary.json":
                runs[f.stem] = json.loads(f.read_text())
    return runs


def label(res: dict) -> str:
    model = "flagship" if res["model"] and "mini" not in res["model"] else "mini"
    return f"{res['turn_mode']} · {res['prompt']} · {model}"


def rows_of(res: dict, kind: str | None = None, observed_ok: bool = True) -> list[dict]:
    rows = res["rows"]
    if kind:
        rows = [r for r in rows if r["expected"] == kind]
    if observed_ok:
        rows = [r for r in rows if r["observed"] == r["expected"]]
    return rows


def turn_table(runs: dict) -> str:
    out = [
        "| Config | n lines | Real end → server end-of-turn (all lines) | Real end → audible reply (plain) | False cut-offs: paused lines | False cut-offs: other lines |",
        "| --- | ---: | --- | --- | --- | --- |",
    ]
    for name, res in runs.items():
        if res.get("turn_mode") is None:
            continue
        all_rows = rows_of(res)
        plain = rows_of(res, "plain")
        fc = res["summary"]["false_cutoffs"]
        out.append(
            f"| {label(res)} | {res['summary']['lines']} | {cell([r['end_to_server'] for r in all_rows])} | "
            f"{cell([r['end_to_audible'] for r in plain])} | {fc['paused_lines'][0]}/{fc['paused_lines'][1]} | {fc['other_lines'][0]}/{fc['other_lines'][1]} |"
        )
    return "\n".join(out)


def preamble_table(runs: dict) -> str:
    out = [
        "| Config | Real end → preamble heard | Real end → value spoken | Model spoke before result | Turn end → tool call | Result → next response |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for name, res in runs.items():
        if res.get("turn_mode") is None:
            continue
        tool = rows_of(res, "tool")
        spoke = sum(1 for r in tool if r["model_spoke_before_result"])
        out.append(
            f"| {label(res)} | {cell([r['end_to_preamble'] for r in tool])} | {cell([r['end_to_value'] for r in tool])} | "
            f"{spoke}/{len(tool)} | {cell([r.get('turn_to_call') for r in tool])} | {cell([r['result_to_created'] for r in tool])} |"
        )
    return "\n".join(out)


def reply_table(runs: dict) -> str:
    out = [
        "| Config | Replies | Over 6 s | Reply length p50 / p95 (s) | Cut by the cap |",
        "| --- | ---: | ---: | --- | ---: |",
    ]
    for name, res in runs.items():
        if res.get("turn_mode") is None:
            continue
        r = res["summary"]["replies"]
        share = f"{r['over_6s']} ({100 * r['over_6s'] / r['n']:.0f}%)" if r["n"] else "–"
        out.append(
            f"| {label(res)} | {r['n']} | {share} | {(r['p50_ms'] or 0) / 1000:.1f} / {(r['p95_ms'] or 0) / 1000:.1f} | {r['truncated_by_cap']} |"
        )
    return "\n".join(out)


def language_table(runs: dict) -> str:
    out = [
        "| Config | Hindi lines → Hindi/Hinglish reply | Hinglish lines → Hindi/Hinglish reply | English lines → English reply | Local clip in the line's language |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, res in runs.items():
        if res.get("turn_mode") is None:
            continue
        lang = res["summary"]["language"]
        t = res["summary"]["tool"]
        clip = f"{t['local_clip_lang_match']}/{t['local_clips']}" if t["local_clips"] else "–"
        f = lambda k: f"{lang[k]['matching']}/{lang[k]['lines']}"  # noqa: E731
        out.append(f"| {label(res)} | {f('hi_lines_hindi_reply')} | {f('hinglish_lines_hindi_reply')} | {f('en_lines_english_reply')} | {clip} |")
    return "\n".join(out)


def connect_table(runs: dict) -> str:
    stages = ["early_start", "secret_fetched", "mic_granted", "pc_offer", "sdp_exchange", "pc_connected", "session_created", "ready"]
    out = ["| Run | Sessions | Secret | " + " | ".join(stages) + " | SDP POST | RTT |", "| --- | ---: | --- |" + " --- |" * (len(stages) + 2)]
    for name, res in runs.items():
        cs = res["connects"]
        sources = ",".join(sorted({c.get("secret_source", "?") for c in cs}))
        vals = [cell([c.get(s) for c in cs]) for s in stages]
        out.append(f"| {name} | {len(cs)} | {sources} | " + " | ".join(vals) + f" | {cell([c.get('sdp_ms') for c in cs])} | {cell([c.get('rtt') for c in cs])} |")
    return "\n".join(out)


def main() -> None:
    runs = load(sys.argv[1:])
    exp = {k: v for k, v in runs.items() if k.startswith("x-")}
    con = {k: v for k, v in runs.items() if k.startswith("connect-")}
    print("## Turn detection\n\n" + turn_table(exp))
    print("\n## Preambles (tool lines)\n\n" + preamble_table(exp))
    print("\n## Reply length\n\n" + reply_table(exp))
    print("\n## Language\n\n" + language_table(exp))
    print("\n## Connect (ms after page load)\n\n" + connect_table({**exp, **con}))


if __name__ == "__main__":
    main()
