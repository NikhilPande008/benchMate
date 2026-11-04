"""Fake bench: scripted evidence scenarios behind the EvidenceSource interface.

Each scenario has a true fault in the demo pack; readings are drawn from the band that fault
predicts (rough numbers inside the placeholder bands, never tuned). Confidences and delays look
like camera reads. Records are marked `simulated`.

Scenarios: short_battery_line, no_current, not_charging, glare_low_confidence,
conflicting_reading, camera_unavailable, vision_timeout.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from .boot import analyse
from .packs import load_pack
from .packs.schema import Band, Pack
from .schema import BootSample, BootTrace, EvidenceError, EvidenceRecord, FieldValue
from .source import OnSample

DEFAULTS = {"point": "normal", "boot_pattern": "NORMAL_BOOT", "port_current": "normal"}


@dataclass(frozen=True)
class Scenario:
    name: str
    true_fault: str
    symptom: str
    meter_glare: bool = False  # meter reads come back illegible / low confidence
    camera_down: bool = False
    vision_timeout: bool = False
    # first camera read of these points returns the wrong class (a probe slip); re-reads are right
    glitch: dict[str, str] = field(default_factory=dict)
    supply: tuple[float, float] = (4.0, 1.0)  # set voltage, current limit


SCENARIOS = {
    s.name: s
    for s in [
        Scenario("short_battery_line", "vbat_cap_short", "dead"),
        Scenario("no_current", "open_vbat_path", "dead"),
        Scenario("not_charging", "charge_path_part", "not_charging"),
        Scenario("glare_low_confidence", "vbat_cap_short", "dead", meter_glare=True),
        Scenario("conflicting_reading", "power_key_line", "dead", glitch={"vbat_conn": "short"}),
        Scenario("camera_unavailable", "open_vbat_path", "dead", camera_down=True),
        Scenario("vision_timeout", "open_vbat_path", "dead", vision_timeout=True),
    ]
}


def _band_value(bands: list[Band], cls: str, rng: random.Random) -> tuple[float | None, bool]:
    band = next(b for b in bands if b.cls == cls)
    if band.ol:
        return None, True
    lo = band.lo if band.lo is not None else 0.0
    hi = band.hi if band.hi is not None else lo * 2 + 1.0
    return round(rng.uniform(lo + 0.2 * (hi - lo), hi - 0.2 * (hi - lo)), 3), False


@dataclass
class FakeBench:
    scenario: str = "short_battery_line"
    pack_id: str = "demo_model"
    seed: int = 7
    time_scale: float = 1.0  # tests run at 0
    supply_delay_s: float = 1.2
    meter_delay_s: float = 1.5
    sample_s: float = 0.5
    timeout_delay_s: float = 6.0  # vision_timeout: longer than the engine's 4 s budget
    fixed: bool = False  # after apply_fix() the board reads healthy everywhere
    _reads: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.set_scenario(self.scenario)

    def set_scenario(self, name: str) -> None:
        if name not in SCENARIOS:
            raise KeyError(f"unknown scenario {name!r}; choose from {sorted(SCENARIOS)}")
        self.scenario = name
        self.fixed = False
        self._reads = {}
        self._rng = random.Random(f"{self.seed}:{name}")

    def apply_fix(self) -> None:
        """The technician replaced the faulty part."""
        self.fixed = True

    @property
    def spec(self) -> Scenario:
        return SCENARIOS[self.scenario]

    @property
    def pack(self) -> Pack:
        return load_pack(self.pack_id)

    def truth(self, measurement_id: str) -> str:
        """The class the true fault produces for a measurement."""
        m = self.pack.measurement(measurement_id)
        fault = next(f for f in self.pack.faults if f.id == self.spec.true_fault)
        if measurement_id in fault.predicts and not self.fixed:
            return fault.predicts[measurement_id]
        return DEFAULTS.get(m.kind) or {"cable_check": "ok", "heat_check": "none"}.get(measurement_id, m.classes[0])

    async def _wait(self, seconds: float) -> None:
        if self.time_scale > 0:
            await asyncio.sleep(seconds * self.time_scale)

    def _camera_failure(self) -> EvidenceError | None:
        if self.spec.camera_down:
            return EvidenceError(error="camera_unavailable", message="overhead camera not responding", retryable=False)
        return None

    def _conf(self) -> float:
        return round(self._rng.uniform(0.93, 0.97), 2)

    async def read_supply(self, session_id: str) -> EvidenceRecord | EvidenceError:
        if err := self._camera_failure():
            return err
        await self._wait(self.supply_delay_s)
        volts, limit = self.spec.supply
        cv, cl = self._conf(), self._conf()
        return EvidenceRecord(
            session_id=session_id,
            kind="supply",
            source="camera",
            simulated=True,
            confidence=min(cv, cl),
            fields={
                "set_voltage": FieldValue(value=volts, unit="V", confidence=cv),
                "current_limit": FieldValue(value=limit, unit="A", confidence=cl),
                "live_current": FieldValue(value=0.0, unit="A", confidence=self._conf()),
            },
        )

    async def read_port_current(self, session_id: str) -> EvidenceRecord | EvidenceError:
        """The supply's live current while it feeds the charging port."""
        record = await self.read_supply(session_id)
        if isinstance(record, EvidenceError):
            return record
        m = self.pack.measurement("port_current")
        value, _ = _band_value(m.bands or [], self.truth("port_current"), self._rng)
        assert record.fields is not None
        record.fields["live_current"] = FieldValue(value=value or 0.0, unit="A", confidence=self._conf())
        record.measurement_id = "port_current"
        record.value, record.unit = value or 0.0, "A"
        return record

    async def read_meter(self, session_id: str, point_id: str | None, expected_mode: str | None) -> EvidenceRecord | EvidenceError:
        if err := self._camera_failure():
            return err
        if self.spec.vision_timeout:
            await self._wait(self.timeout_delay_s)
        await self._wait(self.meter_delay_s)
        point = self.pack.point(point_id) if point_id else None
        mode = point.mode if point else (expected_mode or "diode")
        if point is None:
            value, is_open = round(self._rng.uniform(0.3, 0.5), 3), False
        else:
            n = self._reads.get(point.id, 0)
            self._reads[point.id] = n + 1
            m = self.pack.measurement_for_point(point.id)
            cls = self.spec.glitch.get(point.id) if n == 0 and point.id in self.spec.glitch else self.truth(m.id)
            value, is_open = _band_value(point.reference.bands, cls, self._rng)
        glare = self.spec.meter_glare
        return EvidenceRecord(
            session_id=session_id,
            kind="meter",
            point_id=point_id,
            mode=mode,
            value=value,
            unit={"diode": "diode_V", "ohm": "ohm", "dcv": "V"}.get(mode, "V"),
            open=is_open,
            source="camera",
            simulated=True,
            confidence=round(self._rng.uniform(0.45, 0.6), 2) if glare else self._conf(),
            legible=not glare,
        )

    async def watch_boot_current(self, session_id: str, trigger: str, limit_a: float, on_sample: OnSample) -> EvidenceRecord | EvidenceError:
        if err := self._camera_failure():
            return err
        t = self.pack.boot.thresholds
        pattern = self.truth("boot_pattern")
        n = int(t.watch_s / self.sample_s) + 1
        samples: list[BootSample] = []
        for i in range(n):
            ts = i * self.sample_s
            sample = BootSample(t_s=ts, current_a=round(_boot_curve(pattern, ts, limit_a) + self._rng.uniform(0, 0.002), 4))
            samples.append(sample)
            if await on_sample(sample):
                break
            await self._wait(self.sample_s)
        trace = analyse(BootTrace(trigger=trigger, limit_a=limit_a, samples=samples), t)  # type: ignore[arg-type]
        return EvidenceRecord(
            session_id=session_id,
            kind="boot_trace",
            measurement_id="boot_pattern",
            trace=trace,
            source="camera",
            simulated=True,
            confidence=self._conf(),
        )


def _boot_curve(pattern: str, t: float, limit: float) -> float:
    if pattern == "NO_CURRENT":
        return 0.0
    if pattern == "SHORT_IMMEDIATE":
        return 0.8 * limit
    if pattern == "AT_LIMIT":
        return 0.99 * limit
    if pattern == "RISE_THEN_DROP":
        return 0.25 * min(t / 1.5, 1.0) if t < 2.0 else 0.02
    return 0.2 * min(t / 1.0, 1.0)  # NORMAL_BOOT
