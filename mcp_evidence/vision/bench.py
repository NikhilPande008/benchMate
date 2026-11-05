"""VisionBench: the evidence source on the camera pipeline (same interface as the fake bench, so
the engine doesn't change: mcp_evidence/source.py).

Until hardware arrives it runs on the fake bench seen through a camera: the fake bench decides
what the instruments show (its scenario's truth), the scene renders that into overhead-camera
frames, and the pipeline reads it back: calibration crops → Reader B on frames ~150 ms apart and
Reader A (a fixture that answers from the scene: no API calls) → the agreement rule →
plausibility → an EvidenceRecord. Camera loss, a bumped camera, a slow or failing vision API,
glare and a hand over the meter are all injectable.

With real cameras the same class takes an OpenCVCamera, a saved calibration and the real Reader A
(docs/PENDING_RUNS.md).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from mcp_evidence.fake_bench import FakeBench, _boot_curve
from mcp_evidence.schema import EvidenceError, EvidenceRecord, FieldValue
from mcp_evidence.source import OnSample

from .agreement import DisplayResult, DisplaySpec, read_display
from .bootwatch import watch_current
from .calibration import Calibration
from .frames import FakeCamera
from .health import VisionHealth
from .preread import PreReader
from .reader_a import CachedReaderA, HedgedReaderA, ReadA, ReaderA, fields_of
from .scene import BenchScene, InstrumentState

UNIT_OF_MODE = {"diode": "diode_V", "dcv": "V", "ohm": "ohm", "current": "A"}


def _reason(res: DisplayResult) -> str | None:
    """Why a display read failed, from Reader B's reasons (occluded, blank, ...)."""
    if res.ok:
        return None
    return next((f.reason for f in res.fields.values() if f.reason), res.reason)


def meter_display(mode: str, value: float | None, is_open: bool) -> tuple[str, str]:
    """What a 6000-count auto-ranging meter shows for a value in base units: (text, unit)."""
    if is_open or value is None:
        return "OL", ("Mohm" if mode == "ohm" else "diode_V" if mode == "diode" else "V")
    if mode == "diode":
        return f"{min(value, 2.999):.3f}", "diode_V"
    if mode == "ohm":
        if value < 600:
            return f"{value:.1f}", "ohm"
        if value < 6000:
            return f"{value / 1e3:.3f}", "kohm"
        if value < 60000:
            return f"{value / 1e3:.2f}", "kohm"
        return f"{min(value / 1e6, 5.999):.3f}", "Mohm"
    v = value
    if mode == "current":
        return (f"{v * 1e3:.2f}", "mA") if abs(v) < 0.06 else (f"{v:.3f}", "A")
    if abs(v) < 0.6:
        return f"{v * 1e3:.1f}", "mV"
    return (f"{v:.3f}", "V") if abs(v) < 6 else (f"{v:.2f}", "V")


@dataclass
class SceneOracleA:
    """Reader A fixture for the live fake scene: answers from what the scene shows, with optional
    failures. `down` raises (vision API down), `slow` answers after the hard timeout; a hand or
    glare over the meter makes the meter illegible."""

    state: Callable[[], InstrumentState]
    latency_s: float = 1.2
    time_scale: float = 1.0
    down: bool = False
    slow: bool = False
    calls: int = 0

    async def read(self, display: str, crop: np.ndarray, attempt: int = 0) -> dict[str, ReadA]:
        self.calls += 1
        s = self.state()
        lat = 5.0 if self.slow else self.latency_s
        if lat * self.time_scale > 0:
            await asyncio.sleep(lat * self.time_scale)
        if self.down:
            raise RuntimeError("vision API down (fault injection)")
        if display == "supply":
            out = {}
            for f in fields_of("supply"):
                t = getattr(s, f)
                out[f] = ReadA(True, t, float(t), "V" if f == "set_voltage" else "A", None, len(t.split(".")[1]) if "." in t else 0, 0.95)
            return out
        if s.hand or s.glare or not s.meter_on:
            return {"main": ReadA(False, None, None, "unknown", None, 0, 0.2)}
        if s.knob:  # the mode icon shows the knob's mode
            return {"main": ReadA(True, s.meter_text, None if s.meter_text == "OL" else float(s.meter_text), s.meter_unit, s.knob,
                                  len(s.meter_text.split(".")[1]) if "." in s.meter_text else 0, 0.95)}
        t = s.meter_text
        if t == "OL":
            return {"main": ReadA(True, "OL", None, "OL", s.meter_mode, 0, 0.95)}
        return {"main": ReadA(True, t, float(t), s.meter_unit, s.meter_mode, len(t.split(".")[1]) if "." in t else 0, 0.95)}


@dataclass
class VisionBench:
    truth: FakeBench = field(default_factory=lambda: FakeBench(time_scale=0))
    scene: BenchScene = field(default_factory=BenchScene)
    calibration: Calibration | None = None
    reader_a: ReaderA | None = None
    camera: FakeCamera | None = None
    time_scale: float = 1.0
    frames_dir: Path | None = None
    state: InstrumentState = field(default_factory=InstrumentState)
    health: VisionHealth = field(default_factory=VisionHealth)
    oracle: SceneOracleA | None = None
    last: dict[str, DisplayResult] = field(default_factory=dict)
    prereaders: dict[str, PreReader] = field(default_factory=dict)
    moved_checks: bool = True
    blank_reads: int = 0  # consecutive blank or dim meter reads (a dying meter battery)
    blocked_retry_s: float = 0.5

    def __post_init__(self) -> None:
        if self.camera is None:
            self.camera = FakeCamera("overhead", render=lambda: self.scene.render(self.state), fps=15)
        if self.calibration is None:
            self.calibration = self.scene.calibration()
        if self.reader_a is None:
            self.oracle = SceneOracleA(lambda: self.state, time_scale=self.time_scale)
            self.reader_a = CachedReaderA(HedgedReaderA(self.oracle, time_scale=self.time_scale))
        self._apply_scenario()

    # --- the fake bench's interface the engine also uses ----------------------------------------

    @property
    def scenario(self) -> str:
        return self.truth.scenario

    def set_scenario(self, name: str) -> None:
        self.truth.set_scenario(name)
        self._apply_scenario()

    def apply_fix(self) -> None:
        self.truth.apply_fix()

    @property
    def spec(self):
        return self.truth.spec

    @property
    def pack(self):
        return self.truth.pack

    def _apply_scenario(self) -> None:
        sp = self.truth.spec
        v, lim = sp.supply
        self.state = replace(self.state, set_voltage=f"{v:.2f}", current_limit=f"{lim:.2f}", live_current="0.000", glare=sp.meter_glare)
        assert self.camera is not None
        self.camera.fail(sp.camera_down)
        if self.oracle:
            self.oracle.slow = sp.vision_timeout

    # --- plumbing ---------------------------------------------------------------------------

    def spec_for(self, display: str) -> DisplaySpec:
        cal = self.calibration
        assert cal is not None

        def crops(img: np.ndarray) -> dict[str, np.ndarray]:
            out = {"display": cal.crop(img, display)}
            for f in fields_of(display):
                out[f] = cal.crop(img, f"{display}.{f}")
            return out

        return DisplaySpec(display, crops)

    async def _frame(self, i: int) -> np.ndarray | None:
        assert self.camera is not None
        if self.camera._task is not None and not self.camera._task.done():
            f = await self.camera.next_frame(-1 if i == 0 else self._seq, timeout_s=2.0)
            if f is None:
                return None
            if i > 0:
                f = await self.camera.next_frame(self._seq, timeout_s=2.0) or f
            self._seq = f.seq
            return f.image
        f = self.camera.grab_once()
        return f.image if f else None

    _seq: int = -1

    async def _read(self, display: str, expected_mode: str | None = None) -> DisplayResult | EvidenceError:
        assert self.camera is not None and self.calibration is not None
        probe = await self._frame(0)
        if probe is None:
            self.health.camera(False, "no_frames")
            return EvidenceError(error="camera_unavailable", message="overhead camera not responding", retryable=True)
        if self.moved_checks:
            mv = await asyncio.to_thread(self.calibration.moved, probe)
            if mv.moved:
                self.health.camera(False, "camera_moved")
                return EvidenceError(error="camera_unavailable", message="camera moved since calibration: recalibrate", retryable=False)
        self.health.camera(True)
        use_a = self.health.use_reader_a()
        level = "full" if use_a else self.health.level
        frames = [probe]

        async def nxt(i: int) -> np.ndarray | None:
            while len(frames) <= i:
                f = await self._frame(len(frames))
                if f is None:
                    return None
                frames.append(f)
            return frames[i]

        res = await read_display(self.spec_for(display), nxt, self.reader_a if use_a else None, level, expected_mode)
        if use_a:
            self.health.a_result(res.a_calls, res.a_errors, len(fields_of(display)))
        if res.status == "implausible" and display == "supply":  # re-read once (BUILD_GUIDE: supply current above its limit)
            frames.clear()
            again = await read_display(self.spec_for(display), nxt, self.reader_a if use_a else None, level, expected_mode)
            res = again if again.status != "implausible" else replace(again, reason="reader_error")
        self.last[display] = res
        if self.frames_dir is not None and frames:
            self._last_frame = frames[min(1, len(frames) - 1)]
        return res

    _last_frame: np.ndarray | None = None

    def _save_frame(self, session_id: str, rec: EvidenceRecord, display: str) -> None:
        if self.frames_dir is None or self._last_frame is None or self.calibration is None:
            return
        d = self.frames_dir / session_id
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{rec.id}.jpg"
        cv2.imwrite(str(p), self.calibration.crop(self._last_frame, display), [cv2.IMWRITE_JPEG_QUALITY, 85])
        rec.frame_paths = [str(p)]

    # --- EvidenceSource ---------------------------------------------------------------------------

    def _supply_record(self, session_id: str, res: DisplayResult) -> EvidenceRecord:
        fields = {}
        reader_error = res.status == "implausible"  # current above its own limit twice: don't trust any of it
        for f, unit in (("set_voltage", "V"), ("current_limit", "A"), ("live_current", "A")):
            fr = res.fields[f]
            conf = min(fr.confidence, 0.3) if reader_error else fr.confidence
            fields[f] = FieldValue(value=fr.si_value if fr.accepted else None, unit=unit, confidence=round(conf, 3))
        conf = min(fv.confidence for fv in fields.values())
        return EvidenceRecord(session_id=session_id, kind="supply", source="camera", simulated=True, confidence=conf,
                              fields=fields, legible=res.ok, needs_confirmation=any(fr.needs_confirmation for fr in res.fields.values()))

    async def read_supply(self, session_id: str) -> EvidenceRecord | EvidenceError:
        pre = self.prereaders.get("supply")
        res = pre.get() if pre else None
        if res is None:
            got = await self._read("supply")
            if isinstance(got, EvidenceError):
                return got
            res = got
        rec = self._supply_record(session_id, res)
        self._save_frame(session_id, rec, "supply")
        return rec

    async def read_port_current(self, session_id: str) -> EvidenceRecord | EvidenceError:
        truth = await self.truth.read_port_current(session_id)
        if isinstance(truth, EvidenceError):
            return truth
        self.state = replace(self.state, live_current=f"{truth.value or 0.0:.3f}")
        got = await self._read("supply")
        if isinstance(got, EvidenceError):
            return got
        rec = self._supply_record(session_id, got)
        live = got.fields["live_current"]
        rec.measurement_id = "port_current"
        rec.value, rec.unit = (live.si_value if live.accepted else None), "A"
        rec.legible = live.accepted
        rec.confidence = live.confidence
        self.state = replace(self.state, live_current="0.000")
        return rec

    async def read_meter(self, session_id: str, point_id: str | None, expected_mode: str | None) -> EvidenceRecord | EvidenceError:
        truth = await self.truth.read_meter(session_id, point_id, expected_mode)
        if isinstance(truth, EvidenceError):
            return truth
        mode = truth.mode or expected_mode or "diode"
        text, unit = meter_display(mode, truth.value, truth.open)
        self.state = replace(self.state, meter_text=text, meter_unit=unit, meter_mode=mode)
        got = await self._read("meter", mode)
        if isinstance(got, EvidenceError):
            return got
        if _reason(got) == "occluded":  # a hand or probe over the display: wait, then ask to clear it
            if self.time_scale > 0:
                await asyncio.sleep(self.blocked_retry_s * self.time_scale)
            got = await self._read("meter", mode)
            if isinstance(got, EvidenceError):
                return got
            if _reason(got) == "occluded":
                return EvidenceError(error="unreadable", message="view_blocked", retryable=True)
        if _reason(got) in ("blank", "low_contrast", "no_digits"):
            self.blank_reads += 1
            if self.blank_reads >= 2:
                return EvidenceError(error="unreadable", message="meter_blank", retryable=True)
        elif got.ok:
            self.blank_reads = 0
        if got.status == "wrong_mode":
            return EvidenceError(error="mode_mismatch", message=json.dumps({"on": got.mode, "expected": mode}), retryable=True)
        fr = got.fields["main"]
        rec = EvidenceRecord(
            session_id=session_id, kind="meter", point_id=point_id, mode=mode,  # type: ignore[arg-type]
            value=fr.si_value if fr.accepted else None, unit=UNIT_OF_MODE.get(mode, "V"), open=fr.ol and fr.accepted,
            source="camera", simulated=True, confidence=round(fr.confidence, 3), legible=got.ok,
            needs_confirmation=fr.needs_confirmation,
            resolution=(10 ** -fr.decimals) * (1e-3 if (fr.unit or "").startswith("m") else 1.0) if fr.accepted else None,
        )
        self._save_frame(session_id, rec, "meter")
        return rec

    async def watch_boot_current(self, session_id: str, trigger: str, limit_a: float, on_sample: OnSample) -> EvidenceRecord | EvidenceError:
        if self.truth.spec.camera_down:
            self.health.camera(False, "no_frames")
            return EvidenceError(error="camera_unavailable", message="overhead camera not responding", retryable=True)
        pack = self.truth.pack
        pattern = self.truth.truth("boot_pattern")
        rng = np.random.default_rng(self.truth.seed)
        cal = self.calibration
        assert cal is not None

        async def frame(t: float) -> np.ndarray | None:
            i = _boot_curve(pattern, t, limit_a) + float(rng.uniform(0, 0.002))
            i = min(i, limit_a)
            self.state = replace(self.state, live_current=f"{i:.3f}", cc=i >= 0.99 * limit_a)
            if t > 0 and self.time_scale > 0:
                await asyncio.sleep(0.5 * self.time_scale)
            return await self._frame(1)

        res = await watch_current(frame, lambda img: cal.crop(img, "supply.live_current"), trigger, limit_a, pack.boot.thresholds, on_sample)
        self.state = replace(self.state, live_current="0.000", cc=False)
        if not res.samples:
            return EvidenceError(error="unreadable", message="supply current could not be read during the watch", retryable=True)
        return EvidenceRecord(session_id=session_id, kind="boot_trace", measurement_id="boot_pattern", trace=res.trace, source="camera",
                              simulated=True, confidence=round(max(0.5, res.confidence), 3), legible=res.gaps <= res.samples)

    # --- pre-reads (when the camera runs continuously) ----------------------------------------

    async def start(self) -> None:
        assert self.camera is not None
        await self.camera.start()
        for d in ("supply", "meter"):
            self.prereaders[d] = PreReader(self.spec_for(d), self.camera, self.reader_a)
            self.prereaders[d].start()

    async def stop(self) -> None:
        for p in self.prereaders.values():
            await p.stop()
        self.prereaders.clear()
        if self.camera:
            await self.camera.stop()
