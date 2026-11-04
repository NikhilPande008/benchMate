"""Engine-level scenario runs: a scripted technician drives the 8 tools against the fake bench.

No voice, no browser. The technician does what `next_step` says, reads the meter through the
camera (or aloud, when asked or in spoken-only mode), confirms read-backs, asks the safety gate
before every power step, applies the fix and closes with a confirming boot watch.

    uv run python eval/scenarios/engine_run.py            # all seven scenarios, camera readings
    uv run python eval/scenarios/engine_run.py --spoken   # meter readings spoken aloud
    uv run python eval/scenarios/engine_run.py --json     # machine-readable, for the report
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.session.repair import RepairSession  # noqa: E402
from engine.tools import ToolContext, run_tool  # noqa: E402
from mcp_evidence.fake_bench import SCENARIOS, FakeBench  # noqa: E402
from mcp_evidence.packs.schema import Band  # noqa: E402

SYMPTOM_TEXT = {"dead": {"en": "phone is dead", "hi": "phone dead hai"},
                "not_charging": {"en": "not charging", "hi": "charge nahi ho raha"}}
OBSERVATION_TEXT = {
    "en": {"ok": "the cable is fine", "faulty": "the cable is broken", "none": "nothing hot",
           "cap_hot": "the capacitor is hot", "ic_hot": "the IC is hot"},
    "hi": {"ok": "cable theek hai", "faulty": "cable kharab hai", "none": "kuch garam nahi",
           "cap_hot": "capacitor garam ho raha", "ic_hot": "IC garam hai"},
}
UNIT_WORD = {"diode": "volt", "dcv": "volt", "ohm": "ohm", "current": "amp"}


def band_middle(bands: list[Band], cls: str) -> float | None:
    """A rough value inside a class band (None = OL)."""
    b = next(b for b in bands if b.cls == cls)
    if b.ol:
        return None
    lo = b.lo or 0.0
    hi = b.hi if b.hi is not None else lo * 2 + 1.0
    return round(lo + (hi - lo) * 0.45, 3)


def say_value(value: float | None, mode: str, lang: str) -> str:
    """What a technician says for a reading, e.g. "0.42 volt" or "OL aa raha"."""
    if value is None:
        return "OL aa raha hai" if lang == "hi" else "it shows OL"
    text = f"{value:g} {UNIT_WORD.get(mode, 'volt')}"
    return f"{text} aa raha hai" if lang == "hi" else text


@dataclass
class Call:
    tool: str
    args: dict[str, Any]
    output: str


@dataclass
class RunResult:
    scenario: str
    true_fault: str
    found: str | None = None  # fault in the engine's first fix proposal
    outcome: str = "incomplete"  # fixed | unresolved | blocked | stopped | incomplete
    steps: int = 0  # measurement instructions followed before the fix (power steps count once)
    rechecks: int = 0  # engine asked to re-measure an earlier reading
    read_aloud: int = 0  # engine asked the technician to read a value aloud
    wrong_fix: int = 0  # fix proposals for a fault other than the scripted one
    degraded: list[str] = field(default_factory=list)  # degraded-mode reasons the engine reported
    confident_wrong: bool = False  # a fix proposed for the wrong fault (must never happen)
    max_output: int = 0
    calls: list[Call] = field(default_factory=list)

    @property
    def root_cause_ok(self) -> bool:
        return self.found == self.true_fault


class Technician:
    def __init__(self, scenario: str, *, spoken: bool = False, lang: str = "en", time_scale: float | None = None,
                 vision_timeout_s: float = 0.15, bench: FakeBench | None = None):
        if time_scale is None:  # instant, except where the camera budget itself is under test
            time_scale = 0.04 if SCENARIOS[scenario].vision_timeout else 0.0
        self.bench = bench or FakeBench(scenario, time_scale=time_scale)
        self.repair = RepairSession(f"se_run_{scenario}", self.bench, vision_timeout_s=vision_timeout_s)
        self.ctx = ToolContext(repair=self.repair)
        self.spoken, self.lang = spoken, lang
        self.result = RunResult(scenario, self.bench.spec.true_fault)

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        out, _ = await run_tool(tool, args, self.ctx)
        self.result.calls.append(Call(tool, args, out))
        self.result.max_output = max(self.result.max_output, len(out))
        return json.loads(out)

    def _truth_value(self, measurement_id: str) -> tuple[float | None, str]:
        pack = self.bench.pack
        m = pack.measurement(measurement_id)
        cls = self.bench.truth(measurement_id)
        if m.point_id:
            p = pack.point(m.point_id)
            return band_middle(p.reference.bands, cls), p.mode
        return band_middle(m.bands or [], cls), "current"

    async def speak_reading(self, measurement_id: str) -> dict[str, Any]:
        """Say the value aloud, then confirm the read-back."""
        value, mode = self._truth_value(measurement_id)
        rb = await self.call("record_spoken_reading", text=say_value(value, mode, self.lang), point_id=measurement_id)
        if rb.get("status") != "readback":
            return rb
        return await self.call("record_spoken_reading", text="haan" if self.lang == "hi" else "yes", confirm=True)

    async def speak_observation(self, measurement_id: str) -> dict[str, Any]:
        text = OBSERVATION_TEXT[self.lang][self.bench.truth(measurement_id)]
        rb = await self.call("record_spoken_reading", text=text, point_id=measurement_id)
        if rb.get("status") != "readback":
            return rb
        return await self.call("record_spoken_reading", confirm=True)

    async def read_meter(self, measurement_id: str, point_id: str) -> None:
        if self.spoken:
            await self.speak_reading(measurement_id)
            return
        r = await self.call("get_reading", kind="meter", point_id=point_id)
        if r.get("ask") == "read_aloud":
            self.result.read_aloud += 1
            if r.get("error") == "camera_unavailable" and "camera_unavailable" not in self.result.degraded:
                self.result.degraded.append("camera_unavailable")
            elif r.get("error") == "vision_timeout" and "vision_timeout" not in self.result.degraded:
                self.result.degraded.append("vision_timeout")
            await self.speak_reading(measurement_id)

    async def power(self, step: str) -> bool:
        gate = await self.call("request_power_step", step=step)
        if gate.get("approved"):
            return True
        if gate.get("reason") in ("no_supply_reading", "stale_reading"):
            sup = await self.call("get_reading", kind="supply")
            if "error" in sup:
                return False
            gate = await self.call("request_power_step", step=step)
        return bool(gate.get("approved"))

    async def measure_powered(self, measurement_id: str, power_step: str) -> bool:
        if not await self.power(power_step):
            return False
        if measurement_id == "boot_pattern":
            await self.call("watch_boot_current", trigger="on_connect")
        elif measurement_id == "port_current":
            if self.spoken:
                await self.speak_reading("port_current")
            else:
                r = await self.call("get_reading", kind="port_current")
                if r.get("ask") == "read_aloud":
                    self.result.read_aloud += 1
                    await self.speak_reading("port_current")
        else:
            await self.speak_observation(measurement_id)
        return True

    async def run(self, max_steps: int = 20) -> RunResult:
        res = self.result
        spec = self.bench.spec
        start = await self.call("start_repair", model="demo phone", symptom=SYMPTOM_TEXT[spec.symptom][self.lang], battery="removed")
        if start.get("refused"):
            res.outcome = "refused"
            return res
        sup = await self.call("get_reading", kind="supply")
        if sup.get("error") == "camera_unavailable":
            res.degraded.append("camera_unavailable")
        for _ in range(max_steps):
            step = await self.call("next_step")
            kind = step.get("step")
            if step.get("degraded") and step["degraded"] not in res.degraded:
                res.degraded.append(step["degraded"])
            if kind == "fix":
                res.found = step["fault"]
                if step["fault"] != res.true_fault:
                    res.wrong_fix += 1
                    res.confident_wrong = True
                break
            if kind == "unresolved" or kind is None:
                res.outcome = "unresolved" if kind else step.get("error", "error")
                return res
            if kind == "confirm_readback":
                await self.call("record_spoken_reading", confirm=True)
                continue
            mid = step["measurement"]
            if step.get("then") == "remeasure" or kind == "remeasure":
                res.rechecks += 1
            res.steps += 1
            if kind == "power":
                if not await self.measure_powered(mid, step["power_step"]):
                    res.outcome = "blocked"
                    return res
            elif step.get("point"):
                await self.read_meter(mid, step["point"])
            else:
                await self.speak_observation(mid)
        else:
            return res

        # the fix, then the confirming reading the engine asks for before "fixed" is accepted
        self.bench.apply_fix()
        closed = await self.call("close_repair", result="fixed")
        if closed.get("status") == "confirm_first":
            await self.check_fix(closed)
            closed = await self.call("close_repair", result="fixed")
        res.outcome = closed.get("outcome", closed.get("status", "error"))
        return res

    async def check_fix(self, ask: dict[str, Any]) -> None:
        mid = ask["measure"]
        if ask.get("power_step"):
            await self.measure_powered(mid, ask["power_step"])
        elif ask.get("point"):
            await self.read_meter(mid, ask["point"])
        else:
            await self.speak_observation(mid)


async def run_all(spoken: bool = False, lang: str = "en") -> list[RunResult]:
    return [await Technician(name, spoken=spoken, lang=lang).run() for name in SCENARIOS]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spoken", action="store_true", help="speak every meter reading instead of the camera")
    ap.add_argument("--lang", choices=["en", "hi"], default="en")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--calls", action="store_true", help="print every tool call")
    args = ap.parse_args()
    results = asyncio.run(run_all(args.spoken, args.lang))
    if args.json:
        print(json.dumps([{k: v for k, v in asdict(r).items() if k != "calls"} | {"root_cause_ok": r.root_cause_ok} for r in results], indent=1))
        return
    print(f"{'scenario':22} {'true fault':18} {'found':18} {'outcome':10} steps rechk aloud degraded")
    for r in results:
        print(f"{r.scenario:22} {r.true_fault:18} {str(r.found):18} {r.outcome:10} {r.steps:5} {r.rechecks:5} {r.read_aloud:5} {','.join(r.degraded) or '-'}")
        if args.calls:
            for c in r.calls:
                print(f"    {c.tool}({json.dumps(c.args, ensure_ascii=False)}) -> {c.output}")


if __name__ == "__main__":
    main()
