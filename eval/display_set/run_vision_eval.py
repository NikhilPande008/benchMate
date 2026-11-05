"""Reader B accuracy, confidence calibration and agreement-rule outcomes (no API calls).

    uv run python eval/display_set/run_vision_eval.py --split tune            # while developing
    uv run python eval/display_set/run_vision_eval.py --split heldout         # once, on the frozen reader
    uv run python eval/display_set/run_vision_eval.py --real-misc             # your phone photos; never tuned on

Results go to eval/display_set/results/<name>.json with the reader's SHA-256, so a held-out number
can be matched to the exact code it ran on. Everything here is synthetic except real_misc.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from eval.display_set import vision_eval as ve  # noqa: E402
from mcp_evidence.synth.dataset import GENERATOR_VERSION, load_labels  # noqa: E402

RESULTS = ve.ROOT / "results"
SPLITS = {"tune": ["tune"], "heldout": ["heldout_distortion", "heldout_style", "heldout_both"]}


def synthetic(split_name: str, jitters: list[float]) -> dict:
    labels = {lab["id"]: lab for lab in load_labels(ve.SYNTH)}
    out: dict = {"split": split_name, "synthetic": True, "reader_b_sha": ve.reader_sha(), "generator_version": GENERATOR_VERSION,
                 "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "fixture_rates": ve.FIXTURE_RATES, "runs": {}}
    for split in SPLITS.get(split_name, [split_name]):
        for jit in jitters:
            t0 = time.time()
            reads = ve.synthetic_reads({split}, frames=(0, 1, 2), jitter=jit)
            outcomes = ve.agreement_outcomes({split}, jitter=jit)
            by = ve.accuracy(reads)
            tot = {k: sum(v[k] for v in by.values()) for k in ("n", "correct", "wrong", "illegible", "unreadable_n", "unreadable_illegible", "unreadable_read")}
            tot["acc"] = round(tot["correct"] / max(tot["n"], 1), 4)
            tot["wrong_rate"] = round(tot["wrong"] / max(tot["n"], 1), 4)
            agree = ve.agreement_table(outcomes)
            atot = {k: sum(v[k] for v in agree.values()) for k in next(iter(agree.values()))} if agree else {}
            out["runs"][f"{split}/jitter{jit:g}"] = {
                "samples": len({r.sample for r in reads}), "seconds": round(time.time() - t0, 1),
                "reader_b": {"total": tot, "by_distortion": by},
                "segment_calibration": ve.segment_calibration(reads, labels),
                "reading_calibration": ve.reading_calibration(reads),
                "agreement": {"total": atot, "by_distortion": agree},
            }
    return out


def real_misc() -> dict:
    labs = ve.real_misc_labels()
    if not labs:
        return {"real_misc": None, "note": f"no labels in {ve.REAL_MISC} (labels.csv or labels.jsonl): nothing to report yet"}
    return {"real_misc": ve.real_misc_summary(ve.real_misc_reads()), "reader_b_sha": ve.reader_sha(),
            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "synthetic": False}


def print_tables(res: dict) -> None:
    for name, run in res.get("runs", {}).items():
        t, a = run["reader_b"]["total"], run["agreement"]["total"]
        print(f"\n## {name}  ({run['samples']} samples; Reader B {res['reader_b_sha']})")
        print(f"Reader B on readable fields: {t['correct']}/{t['n']} correct ({t['acc']:.1%}), {t['wrong']} wrong ({t['wrong_rate']:.2%}), "
              f"{t['illegible']} illegible; unreadable fields read anyway: {t['unreadable_read']}/{t['unreadable_n']}")
        print(f"Segment ECE {run['segment_calibration']['ece']}, reading ECE {run['reading_calibration']['ece']}")
        print(f"Agreement: accepted correct {a.get('accepted_correct')}, accepted WRONG {a.get('accepted_wrong')}, to spoken {a.get('to_spoken')} "
              f"of {a.get('fields')} fields")
        print("| distortion | B acc | B wrong | B illegible | agreed correct | agreed wrong | to spoken |")
        print("|---|---:|---:|---:|---:|---:|---:|")
        for d, v in run["reader_b"]["by_distortion"].items():
            g = run["agreement"]["by_distortion"].get(d, {})
            print(f"| {d} | {v['acc']:.1%} ({v['correct']}/{v['n']}) | {v['wrong']} | {v['illegible']} | {g.get('accepted_correct')} | "
                  f"{g.get('accepted_wrong')} | {g.get('to_spoken')} |")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--split", choices=["tune", "heldout"], help="synthetic split")
    ap.add_argument("--jitter", type=float, nargs="*", default=[0.0, 3.0], help="calibration corner error, pixels")
    ap.add_argument("--real-misc", action="store_true")
    a = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    if a.split:
        res = synthetic(a.split, a.jitter)
        path = RESULTS / f"{a.split}-{stamp}.json"
        path.write_text(json.dumps(res, indent=1) + "\n")
        print_tables(res)
        print(f"\n→ {path}")
    if a.real_misc:
        res = real_misc()
        path = RESULTS / f"real_misc-{stamp}.json"
        path.write_text(json.dumps(res, indent=1) + "\n")
        print(json.dumps({k: v for k, v in (res.get("real_misc") or res).items() if k != "items"}, indent=1))
        print(f"\n→ {path}")


if __name__ == "__main__":
    main()
