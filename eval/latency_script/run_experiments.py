"""Phase 1b experiments: turn detection, preambles, reply length, connect time.

    uv run python eval/latency_script/make_clips.py synthetic
    uv run python eval/latency_script/run_experiments.py --start-engine --configs base sem_high --lanes 2
    uv run python eval/latency_script/run_experiments.py --connect 10            # connect-time benchmark
    uv run python eval/latency_script/run_experiments.py --analyse x-20261104-1010   # re-print a stored batch
    uv run python eval/latency_script/run_experiments.py --configs v3_b_ptt --estimate    # cost forecast, no API calls

Cost control: every line's cost is logged from recorded usage, and the batch stops once it has cost
--budget dollars (default 2). --lines t01 p0* runs a subset. At most 2 lanes (default 1).

Turn-driven: the page plays each line into an injected audio stream only after the previous
reply has finished (apps/web/src/harness.ts), so the real end of speech is known exactly.
Each config runs every line of the set, split into sessions of at most 23 lines (below the
~25-turn rotation point). Lanes run configs in parallel browser processes against one engine.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).parent))

from analysis import analyse_run, fmt  # noqa: E402
from budget import DEFAULT_BUDGET_USD, MAX_LANES, BudgetExceeded, BudgetGuard, estimate, select_lines  # noqa: E402
from engine.config import Settings  # noqa: E402
from engine.telemetry import make_engine  # noqa: E402
from engine.telemetry.cost import run_cost  # noqa: E402

HERE = Path(__file__).parent
AUDIO = HERE / "audio"
RUNS = HERE / "runs"
SESSION_MAX_LINES = 23

CONFIGS: dict[str, dict] = {
    # Phase 1 settings: semantic medium, v0 prompt (model's own preamble), no output cap.
    "base": {"turn_mode": "semantic_medium", "prompt": "v0", "cap": False},
    "sem_high": {"turn_mode": "semantic_high", "prompt": "v1", "cap": True},
    "srv_300": {"turn_mode": "server_300", "prompt": "v1", "cap": True},
    "srv_400": {"turn_mode": "server_400", "prompt": "v1", "cap": True},
    "srv_500": {"turn_mode": "server_500", "prompt": "v1", "cap": True},
    "ptt": {"turn_mode": "ptt", "prompt": "v1", "cap": True},
    # Preamble A (short model preamble) and B (local clip), both with v1 verbosity and the cap.
    "pre_a": {"turn_mode": "semantic_medium", "prompt": "v1", "cap": True},
    "pre_b": {"turn_mode": "semantic_medium", "prompt": "v1_local", "cap": True},
    # Second prompt iteration (latest-turn language, one-sentence replies) with the local preamble.
    "v2_b": {"turn_mode": "semantic_medium", "prompt": "v2_local", "cap": True},
    "v2_b_ptt": {"turn_mode": "ptt", "prompt": "v2_local", "cap": True},
    # Phase 2: the 8-tool prompt (decided defaults: push-to-talk, local clip, cap).
    "v3_b_ptt": {"turn_mode": "ptt", "prompt": "v3_local", "cap": True},
    # Phase 2c: session language from the engine, mostly Hinglish examples.
    "v4_b_ptt": {"turn_mode": "ptt", "prompt": "v4_local", "cap": True},
}

BROWSER_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--mute-audio",
]


def engine_up(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=2) as r:
            return json.load(r).get("openai_key_configured", False)
    except OSError:
        return False


def page_url(base: str, cfg: dict, model: str, run_id: str, harness: bool, pool: bool = True) -> str:
    q = {
        "autoconnect": "1",
        "model": model,
        "run": run_id,
        "delay": "1.5",
        "turn_mode": cfg.get("turn_mode", ""),
        "prompt": cfg.get("prompt", ""),
        "cap": "1" if cfg.get("cap", True) else "0",
        "pool": "1" if pool else "0",
        "harness": "1" if harness else "0",
    }
    return f"{base}/?" + "&".join(f"{k}={v}" for k, v in q.items() if v != "")


LINE_SETS = ["synthetic", "en", "hinglish", "hi", "redteam", "real", "real-en", "real-hinglish", "real-hi"]


def audio_dir(which: str) -> str:
    """Folder under audio/ that holds a set's clips (real-<lang> is a filter of real)."""
    return "real" if which.startswith("real") else which


def load_manifest(which: str) -> list[dict]:
    """A set's lines. synthetic alternates languages; en / hinglish / hi stay in one language;
    real-<lang> keeps only your recordings in that language."""
    path = AUDIO / audio_dir(which) / "manifest.json"
    if not path.exists():
        raise SystemExit(f"{path} missing: run make_clips.py {audio_dir(which)}")
    lines = json.loads(path.read_text())["lines"]
    if which.startswith("real-"):
        lines = [l for l in lines if l["lang"] == which.removeprefix("real-")]
        if not lines:
            raise SystemExit(f"no {which.removeprefix('real-')} recordings in {path}")
    return lines


def balanced(lines: list[dict], min_plain: int = 30, min_tool: int = 15) -> list[dict]:
    """Repeat a small set (e.g. ~20 real clips) until it has enough plain and tool lines."""
    out = list(lines)
    plain, tool = [l for l in lines if l["kind"] == "plain"], [l for l in lines if l["kind"] == "tool"]
    rep = 1
    while (sum(l["kind"] == "plain" for l in out) < min_plain and plain) or (sum(l["kind"] == "tool" for l in out) < min_tool and tool):
        out += [{**l, "id": f"{l['id']}#{rep}"} for l in lines]
        rep += 1
    return out


def planned_lines(which: str, patterns: list[str] | None, limit: int | None) -> list[dict]:
    """The lines a config will run: a --lines subset as given, otherwise the balanced set."""
    manifest = load_manifest(which)
    if patterns:
        lines = select_lines(manifest, patterns)
    elif which in ("en", "hinglish", "hi", "redteam"):
        lines = manifest  # a single-language set runs as written (20 lines), never repeated
    else:
        lines = balanced(manifest)
    return lines[:limit]


def run_config(
    base: str, name: str, which: str, model: str, run_id: str, headed: bool, release_lag_ms: int,
    limit: int | None = None, patterns: list[str] | None = None, guard: BudgetGuard | None = None,
) -> bool:
    """Run one config; False if the batch budget stopped it."""
    from playwright.sync_api import sync_playwright

    cfg = CONFIGS[name]
    lines = planned_lines(which, patterns, limit)
    specs = [
        {
            "id": l["id"],
            "url": f"/harness-audio/{audio_dir(which)}/{l['file']}",
            "set": which,
            "kind": l["kind"],
            "lang": l["lang"],
            "speech_start_ms": l["speech_start_ms"],
            "speech_end_ms": l["speech_end_ms"],
            "pause_ms": l["pause_ms"],
        }
        for l in lines
    ]
    chunks = [specs[i : i + SESSION_MAX_LINES] for i in range(0, len(specs), SESSION_MAX_LINES)]
    within_budget = True
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not headed, args=BROWSER_ARGS)
        for k, chunk in enumerate(chunks, 1):
            if not within_budget:
                break
            ctx = browser.new_context(permissions=["microphone"])
            page = ctx.new_page()
            page.route("**/harness-audio/**", lambda route: route.fulfill(path=str(AUDIO / route.request.url.split("/harness-audio/")[1])))
            page.goto(page_url(base, cfg, model, run_id, harness=True))
            page.wait_for_function("window.benchmate && window.benchmate.status.startsWith('connected')", timeout=30_000)
            print(f"  [{name}] session {k}/{len(chunks)}: {len(chunk)} lines", flush=True)
            page.evaluate("async (urls) => window.harness.loadClips(urls)", [l["url"] for l in chunk])
            for spec in chunk:  # one line at a time, so the budget is checked after every line
                page.evaluate(
                    "async ([lines, opts]) => window.harness.runLines(lines, opts)",
                    [[spec], {"settleMs": 1200, "pttReleaseLagMs": release_lag_ms}],
                )
                if guard:
                    try:
                        guard.check(f"[{name}] {spec['id']}")
                    except BudgetExceeded as exc:
                        print(f"  STOPPED: {exc}", flush=True)
                        within_budget = False
                        break
            page.evaluate("window.benchmate.stop()")
            page.wait_for_timeout(1000)
            ctx.close()
        browser.close()
    return within_budget


def run_connect(base: str, n: int, model: str, run_id: str, pool: bool) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, args=BROWSER_ARGS)
        for _ in range(n):
            ctx = browser.new_context(permissions=["microphone"])
            page = ctx.new_page()
            page.goto(page_url(base, {"prompt": "", "turn_mode": "", "cap": True}, model, run_id, harness=False, pool=pool))
            page.wait_for_function("window.benchmate && window.benchmate.status.startsWith('connected')", timeout=30_000)
            page.wait_for_timeout(800)  # let rtt/sdp spans flush
            page.evaluate("window.benchmate.stop()")
            page.wait_for_timeout(600)
            ctx.close()
            time.sleep(2.5)  # give the pool time to refill, as between real page loads
        browser.close()


def print_summary(res: dict) -> None:
    s = res["summary"]
    print(f"\n=== {res['run_id']}  model={res['model']} turn={res['turn_mode']} prompt={res['prompt']} cap={res['max_output_tokens']}")
    print(f"  lines {s['lines']} (plain {s['expected']['plain']}, tool {s['expected']['tool']}); tool choice {s['tool_choice_correct']}/{s['lines']}; no turn signal {s['no_turn_signal']}; timed out {s['timed_out']}")
    for kind in ("plain", "tool"):
        for metric, v in s[kind].items():
            if isinstance(v, dict):
                print(f"  {kind:<5} {metric:<18} {fmt(v)}")
    t = s["tool"]
    print(f"  model spoke before tool result: {t['model_spoke_before_result']}; local clips {t['local_clips']} (language right {t['local_clip_lang_match']})")
    fc = s["false_cutoffs"]
    print(f"  false cut-offs: {fc['all'][0]}/{fc['all'][1]} (paused lines {fc['paused_lines'][0]}/{fc['paused_lines'][1]}, others {fc['other_lines'][0]}/{fc['other_lines'][1]})")
    r = s["replies"]
    tps = f"{r['audio_tokens_per_s']:.1f}" if r["audio_tokens_per_s"] else "n/a"
    print(f"  replies {r['n']}: over 6 s {r['over_6s']}, p50 {r['p50_ms'] or 0:.0f} ms, p95 {r['p95_ms'] or 0:.0f} ms, truncated by cap {r['truncated_by_cap']}, audio tokens/s {tps}")
    lang = s["language"]
    print("  language: " + ", ".join(f"{k} {v['matching']}/{v['lines']}" for k, v in lang.items()))
    c = s["connect"]
    print("  connect (ms after page load): " + ", ".join(f"{k} {fmt(v)}" for k, v in c.items() if v.get("n")))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--configs", nargs="+", default=list(CONFIGS), choices=list(CONFIGS))
    ap.add_argument("--set", default="synthetic", choices=LINE_SETS,
                    help="synthetic alternates languages; en/hinglish/hi stay in one; real[-<lang>] are your recordings")
    ap.add_argument("--model", default="mini")
    ap.add_argument("--lanes", type=int, default=1, choices=range(1, MAX_LANES + 1), help="parallel browsers (at most 2)")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD, help="stop the batch once it has cost this many USD")
    ap.add_argument("--lines", nargs="+", metavar="ID", help="only these line ids (shell patterns: t01 p0* *-pause)")
    ap.add_argument("--estimate", action="store_true", help="print the estimated cost and exit (no API calls)")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--start-engine", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--ptt-release-lag-ms", type=int, default=200)
    ap.add_argument("--connect", type=int, metavar="N", help="connect-time benchmark: N page loads (pool on), N (pool off)")
    ap.add_argument("--limit", type=int, help="only the first N lines (smoke test)")
    ap.add_argument("--stamp")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--analyse", metavar="STAMP", help="re-analyse every run of a stored batch")
    args = ap.parse_args()

    settings = Settings.from_env()
    RUNS.mkdir(exist_ok=True)
    stamp = args.stamp or datetime.now().strftime("%Y%m%d-%H%M")
    run_ids = lambda: [f"x-{stamp}-{c}-{args.set}-{args.model}" for c in args.configs]  # noqa: E731

    if args.estimate:
        db = make_engine(settings.db_path)
        model_id = settings.resolve_model(args.model) or args.model
        total = 0.0
        for name in args.configs:
            n = len(planned_lines(args.set, args.lines, args.limit))
            usd, per_line, seen = estimate(db, model_id, n, ["x-", "e2e-"], CONFIGS[name]["prompt"])
            total += usd
            basis = f"{seen} earlier lines" if seen else "no history: default"
            print(f"{name}: {n} lines x ${per_line:.4f} = ${usd:.2f}  ({model_id}, {basis})")
        print(f"total: ${total:.2f}  (budget stop at ${args.budget:.2f})")
        return

    if args.analyse:
        db = make_engine(settings.db_path)
        for f in sorted((RUNS / args.analyse).glob("*.json")):
            if f.name == "summary.json":
                continue
            res = analyse_run(db, f.stem)
            f.write_text(json.dumps(res, indent=1, ensure_ascii=False))
            print_summary(res)
        return

    proc = None
    if not args.worker and not engine_up(args.base):
        if not args.start_engine:
            raise SystemExit(f"engine not reachable (or no OPENAI_API_KEY) at {args.base}; start it or pass --start-engine")
        proc = subprocess.Popen([sys.executable, "-m", "engine"], cwd=REPO)
        for _ in range(40):
            if engine_up(args.base):
                break
            time.sleep(0.25)
        else:
            proc.terminate()
            raise SystemExit("engine did not start")

    try:
        if args.connect:
            out_dir = RUNS / f"connect-{stamp}"
            out_dir.mkdir(exist_ok=True)
            db = make_engine(settings.db_path)
            for pool in (True, False):
                rid = f"connect-{stamp}-{'pool' if pool else 'nopool'}-{args.model}"
                print(f">>> connect benchmark {rid}: {args.connect} page loads", flush=True)
                run_connect(args.base, args.connect, args.model, rid, pool)
                time.sleep(1)
                res = analyse_run(db, rid)
                (out_dir / f"{rid}.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
                print("  " + ", ".join(f"{k} {fmt(v)}" for k, v in res["summary"]["connect"].items() if v.get("n")))
                print(f"  secret sources: {res['summary']['secret_sources']}")
            return

        guard = BudgetGuard(args.base, f"x-{stamp}-", args.budget)
        if args.worker or args.lanes <= 1:
            for name, rid in zip(args.configs, run_ids()):
                print(f">>> {name}: {rid}", flush=True)
                if not run_config(args.base, name, args.set, args.model, rid, args.headed, args.ptt_release_lag_ms,
                                  args.limit, args.lines, guard):
                    break
            if args.worker:
                return
        else:
            groups = [args.configs[i :: args.lanes] for i in range(args.lanes)]
            workers = [
                subprocess.Popen(
                    [sys.executable, __file__, "--worker", "--stamp", stamp, "--set", args.set, "--model", args.model,
                     "--ptt-release-lag-ms", str(args.ptt_release_lag_ms), "--budget", str(args.budget), "--configs", *g]
                    + (["--limit", str(args.limit)] if args.limit else [])
                    + (["--lines", *args.lines] if args.lines else [])
                    + (["--headed"] if args.headed else []),
                    cwd=REPO,
                )
                for g in groups if g
            ]
            codes = [w.wait() for w in workers]
            if any(codes):
                print(f"worker exit codes: {codes}", flush=True)

        time.sleep(1.5)
        out_dir = RUNS / f"x-{stamp}"
        out_dir.mkdir(exist_ok=True)
        db = make_engine(settings.db_path)
        for rid in run_ids():
            res = analyse_run(db, rid)
            res["cost"] = run_cost(db, rid).line()
            (out_dir / f"{rid}.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
            print_summary(res)
            print(f"  cost: ${res['cost']['usd']:.3f} for {res['cost']['lines']} lines")
        print(f"\nbatch cost: ${run_cost(db, f'x-{stamp}-').usd:.3f}")
        print(f"raw results: {out_dir}")
    finally:
        if proc:
            proc.terminate()


if __name__ == "__main__":
    main()
