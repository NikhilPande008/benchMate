"""Phase 1 latency run: 20 scripted turns per model, played into Chrome's fake microphone.

    uv run python eval/latency_script/make_audio.py           # once: builds audio/session.wav
    uv run python eval/latency_script/run_latency.py          # flagship and mini, one after the other
    uv run python eval/latency_script/run_latency.py --models mini --headed

Needs the engine running with the built web app (`npm run build` in apps/web, then
`uv run python -m engine`), or pass --start-engine. Uses the installed Google Chrome
(Playwright channel "chrome"), so no browser download.

Two views of each turn:
- server turn: client receipt of speech_stopped -> output_audio_buffer.started (engine telemetry)
- user felt:   true end of the scripted line -> audible audio on the remote track
               (true end = mic_open + offset in the WAV; Chrome starts the fake file at getUserMedia)
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

from engine.config import Settings  # noqa: E402
from engine.telemetry import format_summary, load_spans, make_engine, percentile, summarize, turn_metrics  # noqa: E402

HERE = Path(__file__).parent
AUDIO = HERE / "audio"
RUNS = HERE / "runs"


def engine_up(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=2) as r:
            return json.load(r).get("openai_key_configured", False)
    except OSError:
        return False


def run_browser(
    base: str, model: str, run_id: str, delay_s: float, duration_s: float, headed: bool
) -> tuple[list[str], list[dict]]:
    from playwright.sync_api import sync_playwright

    wav = AUDIO / "session.wav"
    args = [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={wav}%noloop",
        "--autoplay-policy=no-user-gesture-required",
        "--mute-audio",
    ]
    console: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not headed, args=args)
        ctx = browser.new_context(permissions=["microphone"])
        page = ctx.new_page()
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        # the fake-WAV runner needs VAD to end turns (push-to-talk is the default since 4 Nov)
        page.goto(f"{base}/?autoconnect=1&model={model}&run={run_id}&delay={delay_s}&turn_mode=semantic_medium")
        page.wait_for_function("window.benchmate && window.benchmate.status.startsWith('connected')", timeout=30_000)
        print(f"  connected; playing {duration_s:.0f} s of scripted audio", flush=True)
        page.wait_for_timeout((duration_s + 12) * 1000)
        transcript = page.evaluate("window.benchmate.lines")
        page.evaluate("window.benchmate.stop()")
        page.wait_for_timeout(1500)
        browser.close()
    return console, transcript


def server_tail(spans: list, session_id: str, speech_end_t: float, line: dict) -> float | None:
    """How much longer the server's VAD says the line lasted than it really did, on the server's
    own audio clock (audio_end_ms - audio_start_ms vs the WAV). Independent of the mic_open
    alignment; audio_start_ms includes the VAD's prefix padding, so this slightly overstates."""
    end = next((s for s in spans if s.session_id == session_id and s.name == "speech_end" and s.t_ms == speech_end_t), None)
    start = max(
        (s for s in spans if s.session_id == session_id and s.name == "speech_started" and s.t_ms < speech_end_t),
        key=lambda s: s.t_ms,
        default=None,
    )
    if not end or not start or not end.attrs or not start.attrs:
        return None
    server_ms = json.loads(end.attrs)["audio_end_ms"] - json.loads(start.attrs)["audio_start_ms"]
    return server_ms - (line["end_ms"] - line["start_ms"])


def analyse(run_id: str, manifest: dict, db_path: Path) -> dict:
    spans = load_spans(make_engine(db_path), run_id)
    mic = [s for s in spans if s.name == "mic_open"]
    connected = [s for s in spans if s.name == "session_connected"]
    phases = {s.name: s.t_ms for s in spans if s.name in ("client_secret", "engine_ready", "session_connected")}
    if not mic:
        raise SystemExit(f"no spans for run {run_id}")
    t0 = mic[0].t_ms
    turns = turn_metrics(spans)
    script = manifest["turns"]

    rows = []
    for i, line in enumerate(script):
        start = t0 + line["start_ms"]
        end = t0 + line["end_ms"]
        nxt = t0 + script[i + 1]["start_ms"] if i + 1 < len(script) else float("inf")
        # The VAD may split a line; the last speech_end within the line (plus a grace period) ends it.
        cands = [t for t in turns if start <= t.speech_end_t < min(end + 4000, nxt)]
        row = {"id": line["id"], "expected": line["kind"], "lang": line["lang"]}
        if not cands:
            row["problem"] = "no speech_end detected"
            rows.append(row)
            continue
        t = cands[-1]
        heard = t.speech_end_t + t.audio_onset if t.audio_onset is not None else None
        row.update(
            observed=t.kind,
            vad_splits=len(cands) - 1,
            vad_lag=t.speech_end_t - end,
            vad_tail=server_tail(spans, t.session_id, t.speech_end_t, line) if len(cands) == 1 else None,
            first_audio=t.first_audio,
            audio_onset=t.audio_onset,
            value_audio=t.value_audio,
            user_felt=(heard - end) if heard is not None else None,
            tool_ms=t.tool_ms,
        )
        if t.kind != line["kind"]:
            row["problem"] = f"expected {line['kind']}, model did {t.kind}"
        elif t.first_audio is None:
            row["problem"] = "no audio"
        rows.append(row)

    def stats(kind: str, metric: str) -> dict:
        vals = [r[metric] for r in rows if r.get("observed") == kind and r.get(metric) is not None]
        return {"n": len(vals), "p50": percentile(vals, 50), "p95": percentile(vals, 95), "max": max(vals, default=None)}

    summary = {
        kind: {m: stats(kind, m) for m in ("first_audio", "audio_onset", "user_felt", "vad_lag", "vad_tail", "value_audio", "tool_ms")}
        for kind in ("plain", "tool")
    }
    return {
        "run_id": run_id,
        "connect_ms": connected[0].t_ms - t0 if connected else None,
        "connect_phases_ms": {k: v - t0 for k, v in phases.items()},
        "rows": rows,
        "summary": summary,
        "telemetry_summary": summarize(turns),
        "telemetry_text": format_summary(f"telemetry view ({len(turns)} turns)", summarize(turns)),
    }


def print_result(model: str, res: dict) -> None:
    print(f"\n=== {model}  run {res['run_id']}  (connect {res['connect_ms']:.0f} ms after mic open)")
    print(f"  {'turn':<6} {'metric':<12} {'n':>3} {'p50 ms':>8} {'p95 ms':>8} {'max ms':>8}")
    for kind, metrics in res["summary"].items():
        for metric, s in metrics.items():
            if s["n"]:
                print(f"  {kind:<6} {metric:<12} {s['n']:>3} {s['p50']:>8.0f} {s['p95']:>8.0f} {s['max']:>8.0f}")
    problems = [r for r in res["rows"] if "problem" in r]
    for r in problems:
        print(f"  ! {r['id']}: {r['problem']}")
    print(f"  {len(res['rows']) - len(problems)}/{len(res['rows'])} turns clean")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", nargs="+", default=["flagship", "mini"])
    parser.add_argument("--delay", type=float, default=1.5, help="dummy get_reading delay (s)")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--start-engine", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--analyse", metavar="RUN_ID", help="only re-analyse a stored run")
    args = parser.parse_args()

    settings = Settings.from_env()
    manifest = json.loads((AUDIO / "manifest.json").read_text())
    RUNS.mkdir(exist_ok=True)

    if args.analyse:
        print_result("(re-analysis)", analyse(args.analyse, manifest, settings.db_path))
        return

    proc = None
    if not engine_up(args.base):
        if not args.start_engine:
            raise SystemExit(f"engine not reachable (or no OPENAI_API_KEY) at {args.base}; start it or pass --start-engine")
        proc = subprocess.Popen([sys.executable, "-m", "engine"], cwd=REPO)
        for _ in range(40):
            if engine_up(args.base):
                break
            time.sleep(0.25)
        else:
            proc.terminate()
            raise SystemExit("engine did not start (is OPENAI_API_KEY set in .env?)")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    results = {}
    try:
        for model in args.models:
            run_id = f"lat-{stamp}-{model}"
            print(f"\n>>> {model}: run {run_id}", flush=True)
            console, transcript = run_browser(args.base, model, run_id, args.delay, manifest["duration_s"], args.headed)
            time.sleep(1)
            res = analyse(run_id, manifest, settings.db_path)
            res["console"] = console[-50:]
            res["transcript"] = transcript
            results[model] = res
            (RUNS / f"{run_id}.json").write_text(json.dumps(res, indent=1))
            print_result(model, res)
    finally:
        if proc:
            proc.terminate()
    print(f"\nraw results: {RUNS}/lat-{stamp}-*.json")


if __name__ == "__main__":
    main()
