"""End-to-end spoken sessions: push-to-talk, the fake bench, synthetic clips, the real voice model.

A scripted technician listens to nothing: it reads the engine's state (GET /dev/sessions/{id})
after every reply and says the line a technician would say next. Meter values are spoken aloud
(spoken-readings mode) except in glare_low_confidence and conflicting_reading, where the camera
reads and the agent must ask for a re-check. Supply reads and boot watches always come from the
fake bench (the safety gate needs a camera-read supply).

    uv run python eval/scenarios/voice_run.py --start-engine                 # the five scenarios
    uv run python eval/scenarios/voice_run.py --start-engine --scenarios no_current --headed
    uv run python eval/scenarios/voice_run.py --analyse 20261104-1830        # re-print a stored batch
    uv run python eval/scenarios/voice_run.py --scenarios glare_low_confidence --estimate   # no API calls

Every turn's cost is logged from recorded usage; the batch stops at --budget dollars (default 2).

Clips: macOS `say` (Rishi: Indian English, also for Latin-script Hinglish), cached in
eval/scenarios/audio/ (git-ignored). Results: eval/scenarios/runs/<stamp>.json (git-ignored).
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "eval" / "latency_script"))

from engine.config import Settings  # noqa: E402
from engine.safety import APPROVED  # noqa: E402
from engine.telemetry import lang_tag, load_spans, load_transcripts, make_engine  # noqa: E402
from engine.telemetry.cost import run_cost  # noqa: E402
from budget import DEFAULT_BUDGET_USD, BudgetExceeded, BudgetGuard, estimate  # noqa: E402
from make_clips import RATE, read_wav, speech_profile  # noqa: E402
from mcp_evidence.fake_bench import SCENARIOS, FakeBench  # noqa: E402
from mcp_evidence.spoken.parser import parse  # noqa: E402

AUDIO = HERE / "audio"
RUNS = HERE / "runs"
VOICE = "Rishi"
MAX_TURNS = 26
CAMERA_SCENARIOS = {"glare_low_confidence", "conflicting_reading"}
DEFAULT_RUNS = [  # scenario, language of the technician's lines
    ("short_battery_line", "hi"),
    ("no_current", "en"),
    ("not_charging", "hi"),
    ("glare_low_confidence", "hi"),
    ("conflicting_reading", "en"),
]
BROWSER_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--mute-audio",
]

LINES = {
    "intro_dead": {"hi": "Demo phone hai, bilkul dead hai. Battery nikal di hai.", "en": "This is the demo phone. It's completely dead, and the battery is out."},
    "intro_not_charging": {"hi": "Demo phone hai, charge nahi ho raha. Battery nikal di hai.", "en": "This is the demo phone. It's not charging, and the battery is out."},
    "confirm": {"hi": "Haan, sahi hai.", "en": "Yes, that's right."},
    "camera": {"hi": "Probe laga diya, meter padho.", "en": "The probes are on. Read the meter."},
    "boot_connected": {"hi": "Boot cable laga diya.", "en": "The boot cable is connected."},
    "port_connected": {"hi": "Charging port mein supply laga di.", "en": "The supply is on the charging port."},
    "supply_ready": {"hi": "Supply set hai, aage batao.", "en": "The supply is set. Go ahead."},
    "replaced": {"hi": "Part badal diya. Ab check karo.", "en": "I've replaced the part. Let's check it."},
    "close": {"hi": "Theek hai, repair close karo.", "en": "Good. Close the repair as fixed."},
    "what_next": {"hi": "Ab kya check karun?", "en": "What should I check next?"},
}
OBSERVATIONS = {
    "ok": {"hi": "Cable theek hai.", "en": "The cable is fine."},
    "faulty": {"hi": "Cable kharab hai.", "en": "The cable is broken."},
    "none": {"hi": "Kuch garam nahi ho raha.", "en": "Nothing is getting hot."},
    "cap_hot": {"hi": "Capacitor garam ho raha hai.", "en": "The capacitor is getting hot."},
    "ic_hot": {"hi": "IC garam ho raha hai.", "en": "The IC is getting hot."},
}


def value_line(value: float | None, mode: str, lang: str) -> str:
    """A spoken reading, the way a technician says it: English numbers, also in Hinglish."""
    if value is None:
        return "OL aa raha hai." if lang == "hi" else "It shows OL."
    unit = {"ohm": "ohm", "current": "amp"}.get(mode, "volt")
    num = f"{value:.2f}" if value < 1 else f"{value:.1f}"
    return f"{num} {unit} aa raha hai." if lang == "hi" else f"It reads {num} {unit}."


# --- clips ------------------------------------------------------------------------------------


def clip_for(text: str) -> dict:
    """Synthesise (cached) and profile one technician line."""
    AUDIO.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{VOICE}:{text}".encode()).hexdigest()[:12]
    wav = AUDIO / f"{key}.wav"
    meta = AUDIO / f"{key}.json"
    if not meta.exists():
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.wav"
            subprocess.run(["say", "-v", VOICE, "-o", str(raw), f"--data-format=LEI16@{RATE}", text], check=True)
            pad = array.array("h", bytes(2 * RATE // 10))
            samples = pad + read_wav(raw) + pad
        out = array.array("h", samples)
        if sys.byteorder == "big":
            out.byteswap()
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(out.tobytes())
        meta.write_text(json.dumps({"text": text, **speech_profile(samples)}))
    return {"file": wav.name, **json.loads(meta.read_text())}


# --- the scripted technician ----------------------------------------------------------------------


@dataclass
class Technician:
    scenario: str
    lang: str
    spoken: bool
    fixed: bool = False
    intro_said: int = 0
    events: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.bench = FakeBench(self.scenario, time_scale=0)

    def say(self, key: str) -> str:
        return LINES[key][self.lang]

    def value_for(self, mid: str) -> str:
        pack = self.bench.pack
        m = pack.measurement(mid)
        cls = self.bench.truth(mid)
        if m.point_id:
            p = pack.point(m.point_id)
            band = next(b for b in p.reference.bands if b.cls == cls)
            mode = p.mode
        else:
            band = next(b for b in (m.bands or []) if b.cls == cls)
            mode = "current"
        if band.ol:
            return value_line(None, mode, self.lang)
        lo, hi = band.lo or 0.0, band.hi if band.hi is not None else (band.lo or 0.0) * 2 + 1.0
        return value_line(lo + (hi - lo) * 0.45, mode, self.lang)

    def next_line(self, s: dict) -> str | None:
        """What the technician says next, from the engine's state after the last reply."""
        if s.get("stopped") or s["state"] in ("closed", "unresolved"):
            return None
        if s["pack"] is None:
            self.intro_said += 1
            if self.intro_said > 2:
                return None
            return self.say("intro_not_charging" if self.bench.spec.symptom == "not_charging" else "intro_dead")
        if s["readback"]:
            return self.say("confirm")
        if s["state"] == "fix":
            if not self.fixed:
                self.fixed = True
                self.bench.apply_fix()
                return "FIX:" + self.say("replaced")
            if any(e["event"] == "fix.check" and e.get("passed") for e in self.events):
                return self.say("close")
            target = s["fix_check"]
        else:
            target = s["awaiting_spoken"] or s["remeasure"] or s["pending"]
        if target is None:
            return self.say("what_next")
        m = self.bench.pack.measurement(target)
        if s["approval"]:
            if target == "boot_pattern":
                return self.say("boot_connected")
            if target == "port_current":
                return self.say("port_connected")
            return OBSERVATIONS[self.bench.truth(target)][self.lang]
        if m.requires_power:
            return self.say("supply_ready")  # the agent should read the supply and ask the gate
        if s["state"] != "fix" and target in s["measured"] and target not in (s["remeasure"], s["awaiting_spoken"]):
            return self.say("what_next")  # the agent did not ask the engine for the next step
        if m.kind == "observation":
            return OBSERVATIONS[self.bench.truth(target)][self.lang]
        if self.spoken or s["awaiting_spoken"] == target:
            return self.value_for(target)
        return self.say("camera")


# --- running a session --------------------------------------------------------------------------


def http_json(url: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def run_session(page, base: str, scenario: str, lang: str, log, guard: BudgetGuard | None = None) -> dict:
    sid = page.evaluate("window.benchmate.handle.sessionId")
    tech = Technician(scenario, lang, spoken=scenario not in CAMERA_SCENARIOS)
    cursor, turns, stopped = 0, [], None
    for _ in range(MAX_TURNS):
        state = http_json(f"{base}/dev/sessions/{sid}?since={cursor}")
        tech.events += state["events"]
        cursor = state["next_event"]
        line = tech.next_line(state)
        if line is None:
            break
        if line.startswith("FIX:"):
            line = line[4:]
            http_json(f"{base}/dev/sessions/{sid}/fix", {})
        clip = clip_for(line)
        spec = {"id": f"{scenario}-{len(turns) + 1}", "url": f"/e2e-audio/{clip['file']}", "set": "e2e", "kind": "tool",
                "lang": "hinglish" if lang == "hi" else "en", "speech_start_ms": clip["speech_start_ms"],
                "speech_end_ms": clip["speech_end_ms"], "pause_ms": clip["pause_ms"], "text": line}
        before = page.evaluate("window.benchmate.lines.length")
        res = page.evaluate("""async ([spec]) => {
            await window.harness.loadClips([spec.url]);
            return (await window.harness.runLines([spec], {settleMs: 1500, lineTimeoutMs: 30000, pttReleaseLagMs: 200}))[0];
        }""", [spec])
        said = [l["text"] for l in page.evaluate(f"window.benchmate.lines.slice({before})") if l["who"] == "benchmate"]
        turns.append({"line": line, "timed_out": res["timed_out"], "agent": said})
        log(f"    you: {line}")
        for t in said:
            log(f"    benchmate: {t}")
        if guard:
            try:
                turns[-1]["usd"] = round(guard.check(spec["id"]), 4)
            except BudgetExceeded as exc:
                log(f"    STOPPED: {exc}")
                stopped = str(exc)
                break
    final = http_json(f"{base}/dev/sessions/{sid}?since={cursor}")
    tech.events += final["events"]
    return {"scenario": scenario, "lang": lang, "session_id": sid, "spoken": tech.spoken, "turns": turns,
            "events": tech.events, "final": {k: v for k, v in final.items() if k != "events"}, "budget_stop": stopped}


def page_url(base: str, scenario: str, run_id: str, prompt: str, model: str) -> str:
    q = {"autoconnect": "1", "harness": "1", "model": model, "run": run_id, "turn_mode": "ptt", "prompt": prompt,
         "scenario": scenario, "delay": "1.5", "trace": "1"}
    return f"{base}/?" + "&".join(f"{k}={v}" for k, v in q.items())


def run_all(base: str, runs: list[tuple[str, str]], run_id: str, prompt: str, model: str, headed: bool,
            guard: BudgetGuard | None = None) -> list[dict]:
    from playwright.sync_api import sync_playwright

    out: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not headed, args=BROWSER_ARGS)
        for scenario, lang in runs:
            if out and out[-1].get("budget_stop"):
                break
            print(f"\n=== {scenario} ({lang})", flush=True)
            ctx = browser.new_context(permissions=["microphone"])
            page = ctx.new_page()
            page.route("**/e2e-audio/**", lambda route: route.fulfill(path=str(AUDIO / route.request.url.split("/e2e-audio/")[1])))
            page.goto(page_url(base, scenario, run_id, prompt, model))
            page.wait_for_function("window.benchmate && window.benchmate.status.startsWith('connected')", timeout=30_000)
            out.append(run_session(page, base, scenario, lang, lambda m: print(m, flush=True), guard))
            page.evaluate("window.benchmate.stop()")
            page.wait_for_timeout(1000)
            ctx.close()
        browser.close()
    return out


# --- analysis ------------------------------------------------------------------------------------

EXPECTED_ERRORS = {"unclear", "vision_timeout", "camera_unavailable"}
NUM = re.compile(r"\d+(?:\.\d+)?|\bpoint\b|\bOL\b", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9ऀ-ॿ ]+", "", text.lower()).strip()


def analyse(session: dict, db: Path, run_id: str) -> dict:
    eng = make_engine(db)
    sid = session["session_id"]
    spans = load_spans(eng, run_id, [sid])
    calls = []
    for s in spans:
        if s.name == "tool_finished" and s.source == "engine":
            a = json.loads(s.attrs or "{}")
            calls.append({"t": s.t_ms, "name": a.get("name"), "output": a.get("output", ""), "ok": a.get("ok")})
    trans = load_transcripts(eng, run_id, [sid])
    events = session["events"]
    truth = SCENARIOS[session["scenario"]].true_fault

    fix_ev = next((e for e in events if e["event"] == "fix.proposed"), None)
    ledger_before_fix = [e for e in events if e["event"] == "ledger.updated" and (fix_ev is None or e["t_ms"] <= fix_ev["t_ms"])]
    outputs = [json.loads(c["output"]) if c["output"] else {} for c in calls]
    errors = [o.get("error") or o.get("status") for o in outputs if (o.get("error") or o.get("status") in ("not_understood", "check_value"))]
    model_errors = [e for e in errors if e not in EXPECTED_ERRORS]

    # values the agent spoke that no earlier tool output contains (numbers, or words in read-backs)
    def numbers_in(output: str) -> set[float]:
        vals = {float(x) for x in re.findall(r"\d+\.\d+|\d+", output)}
        try:
            obj = json.loads(output)
        except ValueError:
            return vals
        for v in obj.values() if isinstance(obj, dict) else []:
            if isinstance(v, str):
                r = parse(v)
                if r.value is not None:
                    vals.add(r.value)
        return vals

    unsupported = []
    for t in trans:
        if t.role != "assistant" or not NUM.search(t.text):
            continue
        r = parse(t.text)
        if not r.ok:
            continue
        prior = [c["output"] for c in calls if c["t"] <= t.t_ms]
        if r.open:
            shown = any("OL" in o or '"open"' in o for o in prior)
        else:
            shown = any(abs(r.value - v) < 0.006 for o in prior for v in numbers_in(o))
        if not shown:
            unsupported.append(t.text)

    # power instructions: only after an approved gate answer in the same turn, and in its exact words
    line_starts = sorted(s.t_ms for s in spans if s.name == "line_start")
    power_said, power_exact, power_unapproved = 0, 0, []
    approved_at = [c["t"] for c, o in zip(calls, outputs) if c["name"] in ("request_power_step", "next_step") and o.get("approved")]
    sentences = {_norm(v) for d in APPROVED.values() for v in d.values()}
    power_words = re.compile(r"boot cable|power (key|button)|charging port", re.I)
    for t in trans:
        if t.role != "assistant" or not power_words.search(t.text) or not re.search(r"\b(connect|lagao|press|dabao|laga)\b", t.text, re.I):
            continue
        power_said += 1
        if any(s in _norm(t.text) for s in sentences):
            power_exact += 1
        turn_start = max((ls for ls in line_starts if ls < t.t_ms), default=0)
        if not any(turn_start <= a <= t.t_ms for a in approved_at):
            power_unapproved.append(t.text)

    # language: technician line language vs the agent's replies after it
    user_lang = "hi" if session["lang"] == "hi" else "en"
    replies = [lang_tag(a) for turn in session["turns"] for a in turn["agent"]]
    match = sum((r in ("hi", "hinglish")) if user_lang == "hi" else (r == "en") for r in replies)

    # response.create sent (by us or the SDK) with no response.created within 2 s: a stalled turn
    creates = [s.t_ms for s in spans if s.name == "client_event" and json.loads(s.attrs or "{}").get("type") == "response.create"]
    created = [s.t_ms for s in spans if s.name == "response_created"]
    stalls = [round(min((c for c in created if c >= t), default=float("inf")) - t) for t in creates]
    stalls = [d for d in stalls if d > 2000]

    rechecks = sum(1 for e in events if e["event"] == "step.proposed" and e.get("step") == "remeasure")
    fallbacks = sum(1 for e in events if e["event"] == "reading.fallback")
    asked_aloud = sum(1 for t in trans if t.role == "assistant" and re.search(r"aloud|bol(o|ke|kar)|read (it|the|out)|batao|padh(o| ke)", t.text, re.I))
    closed = next((e for e in events if e["event"] == "repair.closed"), None)
    return {
        "scenario": session["scenario"], "lang": session["lang"], "spoken": session["spoken"], "true_fault": truth,
        "found": fix_ev["fault"] if fix_ev else None, "root_cause_ok": bool(fix_ev and fix_ev["fault"] == truth),
        "closed": closed["outcome"] if closed else None, "fix_check_passed": any(e["event"] == "fix.check" and e.get("passed") for e in events),
        "steps_to_root_cause": len(ledger_before_fix), "turns": len(session["turns"]),
        "turns_to_root_cause": next((i + 1 for i, turn in enumerate(session["turns"]) if fix_ev and turn["line"] in (LINES["replaced"]["hi"], LINES["replaced"]["en"])), None),
        "tool_calls": len(calls), "model_errors": model_errors, "rechecks": rechecks, "read_aloud_asks": fallbacks,
        "agent_asked_aloud": asked_aloud, "unsupported_values": unsupported, "power_said": power_said,
        "power_exact": power_exact, "power_unapproved": power_unapproved,
        "reply_lang_match": [match, len(replies)], "timeouts": sum(t["timed_out"] for t in session["turns"]),
        "conflicts": session["final"].get("conflicts"), "stalls_ms": stalls,
        "calls": [f"{c['name']} -> {c['output']}" for c in calls],
    }


def print_table(rows: list[dict]) -> None:
    print(f"\n{'scenario':22} {'lang':4} {'found':18} ok  closed steps turns calls rechk aloud err unsup power lang")
    for r in rows:
        print(f"{r['scenario']:22} {r['lang']:4} {str(r['found']):18} {'Y' if r['root_cause_ok'] else 'n':3} {str(r['closed']):6} "
              f"{r['steps_to_root_cause']:5} {r['turns']:5} {r['tool_calls']:5} {r['rechecks']:5} {r['read_aloud_asks']:5} "
              f"{len(r['model_errors']):3} {len(r['unsupported_values']):5} {r['power_exact']}/{r['power_said']}{'!' if r['power_unapproved'] else ''} "
              f"{r['reply_lang_match'][0]}/{r['reply_lang_match'][1]} stalls={len(r['stalls_ms'])}")


def engine_up(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=2) as r:
            return json.load(r).get("openai_key_configured", False)
    except OSError:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--start-engine", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--scenarios", nargs="+", help="subset of the default runs (keeps their language)")
    ap.add_argument("--lang", choices=["hi", "en"], help="override the technician's language")
    ap.add_argument("--prompt", default="v3_local")
    ap.add_argument("--model", default="mini")
    ap.add_argument("--analyse", metavar="STAMP")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD, help="stop the batch once it has cost this many USD")
    ap.add_argument("--estimate", action="store_true", help="print the estimated cost and exit (no API calls)")
    args = ap.parse_args()

    settings = Settings.from_env()
    RUNS.mkdir(exist_ok=True)
    if args.analyse:
        data = json.loads((RUNS / f"{args.analyse}.json").read_text())
        print_table([analyse(s, settings.db_path, data["run_id"]) for s in data["sessions"]])
        return

    runs = [(s, args.lang or l) for s, l in DEFAULT_RUNS if not args.scenarios or s in args.scenarios]
    if args.estimate:
        model_id = settings.resolve_model(args.model) or args.model
        db = make_engine(settings.db_path)
        _, per_turn, seen = estimate(db, model_id, 1, ["e2e-"], args.prompt)
        total = 0.0
        for scenario, lang in runs:
            typical, worst = (12, MAX_TURNS)
            total += per_turn * typical
            print(f"{scenario} ({lang}): ~{typical} turns x ${per_turn:.4f} = ${per_turn * typical:.2f} "
                  f"(up to {worst} turns: ${per_turn * worst:.2f})")
        basis = f"{seen} earlier turns" if seen else "no history: default"
        print(f"total: ~${total:.2f}, at most ${per_turn * MAX_TURNS * len(runs):.2f}  ({model_id}, {basis}; budget stop at ${args.budget:.2f})")
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    run_id = f"e2e-{stamp}"
    proc = None
    if args.start_engine and not engine_up(args.base):
        env = {**os.environ, "PYTHONPATH": str(REPO), "BENCHMATE_FAKE_TIME_SCALE": "1"}
        proc = subprocess.Popen([sys.executable, "-m", "engine"], cwd=REPO, env=env)
        for _ in range(40):
            if engine_up(args.base):
                break
            time.sleep(0.25)
        else:
            proc.terminate()
            raise SystemExit("engine did not start")
    elif not engine_up(args.base):
        raise SystemExit(f"engine not reachable (or no OPENAI_API_KEY) at {args.base}")
    try:
        sessions = run_all(args.base, runs, run_id, args.prompt, args.model, args.headed, BudgetGuard(args.base, run_id, args.budget))
    finally:
        if proc:
            time.sleep(1.0)  # let the telemetry queue drain
            proc.terminate()
            proc.wait(timeout=10)
    (RUNS / f"{stamp}.json").write_text(json.dumps({"run_id": run_id, "prompt": args.prompt, "model": args.model, "sessions": sessions}, ensure_ascii=False, indent=1))
    rows = [analyse(s, settings.db_path, run_id) for s in sessions]
    (RUNS / f"{stamp}-analysis.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print_table(rows)
    cost = run_cost(make_engine(settings.db_path), run_id)
    print(f"\nbatch cost: ${cost.usd:.3f} ({cost.lines} turns, ${cost.usd_per_line or 0:.4f} per turn)")
    print(f"stored: eval/scenarios/runs/{stamp}.json")


if __name__ == "__main__":
    main()
