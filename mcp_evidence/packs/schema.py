"""Model pack schema (ARCHITECTURE.md §7.2), version 1.

A pack is data, never instructions: points to measure, the outcome classes of each measurement,
faults with the outcome each one predicts, safe power settings and boot-pattern thresholds.
Every object that carries a number has a `placeholder` flag: true means the number was invented
for development and must be replaced by measurements from the reference phone.

COMPLETENESS RULE: a fault must predict "normal" (the measurement's healthy class) at every
point where it leaves the board healthy. A missing prediction means "no opinion" and scores 1/K
for any reading, while a spelled-out "normal" scores c + (1-c)/K on a normal reading. So on
readings that say nothing about the real cause, the fault that spells out more normal readings
wins. Leave a prediction out only when the fault really says nothing about the measurement
(here: charging-port current for a dead-phone fault).
Enforced by engine/tests/test_packs.py::test_every_fault_predicts_every_point and
::test_only_port_current_may_be_left_without_a_prediction; the failure it prevents is shown in
::test_a_gap_in_the_predictions_picks_the_wrong_fault.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1
BOOT_PATTERNS = ("NO_CURRENT", "SHORT_IMMEDIATE", "RISE_THEN_DROP", "NORMAL_BOOT", "AT_LIMIT")
POWER_STEPS = ("connect_boot_cable", "press_power_key", "connect_port_supply")
Symptom = Literal["dead", "not_charging"]
HEALTHY_BY_KIND = {"point": "normal", "boot_pattern": "NORMAL_BOOT", "port_current": "normal"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Localized(Strict):
    en: str
    hi: str  # Hinglish in Latin script, as technicians speak


class Sourced(Strict):
    placeholder: bool = Field(description="true = invented for development; replace with reference measurements")


class Band(Strict):
    """Reading class for value in [lo, hi). `ol` matches an OL (open) display."""

    cls: str = Field(alias="class")
    lo: float | None = None
    hi: float | None = None
    ol: bool = False

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Reference(Sourced):
    value: float
    tolerance: float
    unit: Literal["diode_V", "V", "ohm", "A"]
    bands: list[Band]


class Point(Strict):
    id: str
    label: Localized
    side: Literal["top", "bottom"]
    xy: tuple[int, int] | None = None  # on the reference photo; None in the general flow
    mode: Literal["diode", "dcv", "ohm"]
    probe_note: Localized
    reference: Reference


class Measurement(Sourced):
    """Something the ledger can learn from. `placeholder` covers `cost`."""

    id: str
    kind: Literal["boot_pattern", "point", "observation", "port_current"]
    label: Localized
    classes: list[str] = Field(min_length=2)
    cost: float = Field(gt=0)  # effort: re-power 3, exposed diode check 1, under a shield 2
    point_id: str | None = None
    requires_power: Literal["connect_boot_cable", "press_power_key", "connect_port_supply"] | None = None
    # observation / port_current: spoken phrases that mean each class (lower case, any language)
    keywords: dict[str, list[str]] | None = None
    # port_current: current bands in amps
    bands: list[Band] | None = None
    # the class a healthy board gives; checks a fix. Defaults by kind; observations must name it.
    healthy: str | None = None

    @model_validator(mode="after")
    def _healthy(self) -> "Measurement":
        if self.healthy is None:
            self.healthy = HEALTHY_BY_KIND.get(self.kind)
        if self.healthy is None:
            raise ValueError(f"measurement {self.id}: an observation must name its healthy class")
        if self.healthy not in self.classes:
            raise ValueError(f"measurement {self.id}: healthy class {self.healthy!r} is not one of its classes")
        return self


class Fault(Sourced):
    """`placeholder` covers `priors`."""

    id: str
    family: Literal["short_battery_line", "no_current", "not_charging", "general"]
    name: Localized
    priors: dict[str, float]  # symptom -> prior weight (normalised by the ledger)
    predicts: dict[str, str]  # measurement id -> predicted class
    fix: Localized
    explain: dict[Literal["trainee", "expert"], Localized]


class SafePower(Sourced):
    voltage_min: float
    voltage_max: float
    voltage_target: float
    current_limit_max: float
    current_limit_target: float

    @model_validator(mode="after")
    def _band(self) -> "SafePower":
        if not self.voltage_min <= self.voltage_target <= self.voltage_max:
            raise ValueError("voltage_target must lie inside the safe band")
        if not 0 < self.current_limit_target <= self.current_limit_max:
            raise ValueError("current_limit_target must be > 0 and <= current_limit_max")
        return self


class BootThresholds(Sourced):
    no_current_a: float  # peak below this -> NO_CURRENT
    short_connect_a: float  # current at connect (before power press) above this -> SHORT_IMMEDIATE
    at_limit_fraction: float  # current >= this fraction of the limit counts as "at limit"
    at_limit_s: float  # longer than this at the limit -> AT_LIMIT
    drop_fraction: float  # steady below this fraction of the peak -> RISE_THEN_DROP
    watch_s: float  # how long a watch samples


class BootPattern(Strict):
    id: Literal["NO_CURRENT", "SHORT_IMMEDIATE", "RISE_THEN_DROP", "NORMAL_BOOT", "AT_LIMIT"]
    meaning: Localized
    next_checks: list[str]  # measurement ids, in the order an expert would take them


class Boot(Strict):
    thresholds: BootThresholds
    patterns: list[BootPattern]


class LedgerParams(Sourced):
    other_prior: float = Field(gt=0, lt=0.5)  # catch-all "other" fault
    planning_confidence: float = Field(gt=0, le=1)  # assumed confidence when scoring next steps


class Pack(Strict):
    schema_version: Literal[1]
    pack_id: str
    general: bool  # the general dead-phone flow (no model data)
    name: Localized
    aliases: list[str] = Field(default_factory=list)
    placeholder: bool  # true while any number in the pack is a placeholder
    sources: list[str]
    symptoms: list[str]
    safe_power: SafePower
    boot: Boot
    ledger: LedgerParams
    points: list[Point]
    measurements: list[Measurement]  # list order = expert order (tie-break for next step)
    faults: list[Fault]
    glossary: dict[str, Localized] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> "Pack":
        def unique(ids: list[str], what: str) -> None:
            dupes = {i for i in ids if ids.count(i) > 1}
            if dupes:
                raise ValueError(f"duplicate {what} ids: {sorted(dupes)}")

        unique([p.id for p in self.points], "point")
        unique([m.id for m in self.measurements], "measurement")
        unique([f.id for f in self.faults], "fault")
        points = {p.id: p for p in self.points}
        measures = {m.id: m for m in self.measurements}
        for m in self.measurements:
            if m.kind == "point":
                if m.point_id not in points:
                    raise ValueError(f"measurement {m.id}: unknown point {m.point_id}")
                band_classes = {b.cls for b in points[m.point_id].reference.bands}
                if band_classes != set(m.classes):
                    raise ValueError(f"measurement {m.id}: classes must match point bands {sorted(band_classes)}")
            if m.kind == "boot_pattern" and tuple(m.classes) != BOOT_PATTERNS:
                raise ValueError(f"boot measurement {m.id} must list the five boot patterns in order")
            if m.kind == "observation" and (not m.keywords or set(m.keywords) != set(m.classes)):
                raise ValueError(f"observation {m.id} needs keywords for every class")
            if m.kind == "port_current" and (not m.bands or {b.cls for b in m.bands} != set(m.classes)):
                raise ValueError(f"port_current {m.id} needs a band for every class")
        for f in self.faults:
            for mid, cls in f.predicts.items():
                if mid not in measures:
                    raise ValueError(f"fault {f.id}: unknown measurement {mid}")
                if cls not in measures[mid].classes:
                    raise ValueError(f"fault {f.id}: {cls!r} is not a class of {mid}")
            if any(p <= 0 for p in f.priors.values()):
                raise ValueError(f"fault {f.id}: priors must be positive")
            if set(f.priors) - set(self.symptoms):
                raise ValueError(f"fault {f.id}: priors for unknown symptoms")
        if {p.id for p in self.boot.patterns} != set(BOOT_PATTERNS):
            raise ValueError("boot.patterns must define all five patterns")
        for pat in self.boot.patterns:
            for mid in pat.next_checks:
                if mid not in measures:
                    raise ValueError(f"boot pattern {pat.id}: unknown next check {mid}")
        return self

    def measurement(self, mid: str) -> Measurement:
        return next(m for m in self.measurements if m.id == mid)

    def point(self, pid: str) -> Point:
        return next(p for p in self.points if p.id == pid)

    def measurement_for_point(self, pid: str) -> Measurement | None:
        return next((m for m in self.measurements if m.point_id == pid), None)
