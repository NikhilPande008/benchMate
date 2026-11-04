"""Evidence contracts shared by the engine and the evidence server (ARCHITECTURE.md §7.1).

Every reading - camera, spoken or fake bench - becomes an EvidenceRecord. The engine classifies
it against the model pack (`outcome`) and only then lets it into the diagnosis ledger.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EvidenceKind = Literal["supply", "meter", "boot_trace", "probe_check", "spoken", "observation"]
EvidenceSource = Literal["camera", "spoken"]
MeterMode = Literal["diode", "dcv", "ohm", "current"]


def new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000):x}{secrets.token_hex(3)}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FieldValue(BaseModel):
    """One number on a multi-field display (the bench supply shows three)."""

    model_config = ConfigDict(extra="forbid")

    value: float | None  # None = not visible / not set
    unit: str
    confidence: float = Field(ge=0, le=1)


class BootSample(BaseModel):
    t_s: float
    current_a: float


class BootFeatures(BaseModel):
    i_connect: float  # current within 1 s of connect, before power press
    i_peak: float
    i_steady: float  # median of the last samples
    t_drop: float | None  # peak -> near zero, if it dropped
    at_limit_s: float  # longest run at the current limit


class BootTrace(BaseModel):
    trigger: Literal["on_connect", "on_power_press"]
    limit_a: float
    samples: list[BootSample]
    features: BootFeatures | None = None
    pattern: str | None = None  # NO_CURRENT | SHORT_IMMEDIATE | RISE_THEN_DROP | NORMAL_BOOT | AT_LIMIT


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: new_id("ev"))
    session_id: str
    kind: EvidenceKind
    measurement_id: str | None = None  # the pack measurement this answers, if any
    point_id: str | None = None
    mode: MeterMode | None = None
    value: float | None = None  # in the base unit (V, A, ohm)
    unit: str | None = None  # "V", "diode_V", "A", "ohm"
    open: bool = False  # the display showed OL
    fields: dict[str, FieldValue] | None = None  # supply: set_voltage, current_limit, live_current
    trace: BootTrace | None = None
    outcome: str | None = None  # class, set by the engine from the pack
    source: EvidenceSource
    simulated: bool = False  # produced by the fake bench, not a real instrument
    confidence: float = Field(ge=0, le=1)
    legible: bool = True
    resolution: float | None = None
    needs_confirmation: bool = False
    confirmed: bool = False
    superseded_by: str | None = None
    frame_paths: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    def age_s(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.created_at).total_seconds()


class EvidenceError(BaseModel):
    """What an evidence tool returns instead of a record (ARCHITECTURE.md §5.1)."""

    error: Literal["camera_unavailable", "vision_timeout", "unreadable", "mode_mismatch", "no_display"]
    message: str
    retryable: bool
