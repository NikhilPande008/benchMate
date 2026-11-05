"""Power gate (ARCHITECTURE.md §6.5, CLAUDE.md rule 2).

The voice model never tells anyone to connect power. It calls request_power_step(step); this
checks the latest camera-read supply and returns either the exact sentence to say (English and
Hindi) or the first reason to block, with the value to set. How long an approval lasts is in
engine/safety/approval.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from engine.safety.approval import APPROVAL_TTL_S, Approval, Approvals  # noqa: F401  (re-exported)
from mcp_evidence.packs.schema import Pack
from mcp_evidence.schema import EvidenceRecord, utcnow

STALE_S = 20.0  # a supply reading must be younger than this
MIN_CONFIDENCE = 0.9  # for both the voltage and the current limit

PowerStep = Literal["connect_boot_cable", "press_power_key", "connect_port_supply"]
Battery = Literal["unknown", "removed", "connected"]

APPROVED = {
    "connect_boot_cable": {"en": "Connect the boot cable now.", "hi": "Ab boot cable lagao."},
    "press_power_key": {"en": "Press the power key now.", "hi": "Ab power button dabao."},
    "connect_port_supply": {"en": "Connect the supply to the charging port now.", "hi": "Ab charging port mein supply lagao."},
}


def _v(x: float) -> str:
    return f"{x:.1f}"


@dataclass(frozen=True)
class GateResult:
    approved: bool
    step: str
    reason: str | None = None
    say: dict[str, str] = field(default_factory=dict)  # exact sentence, "en" and "hi"
    set_values: dict[str, float] = field(default_factory=dict)

    def line(self) -> dict:
        out: dict = {"approved": self.approved, "step": self.step, "say": self.say}
        if self.reason:
            out["reason"] = self.reason
        if self.set_values:
            out["set"] = self.set_values
        return out


def _block(step: str, reason: str, en: str, hi: str, **values: float) -> GateResult:
    return GateResult(False, step, reason, {"en": en, "hi": hi}, values)


def check_power(
    step: str,
    supply: EvidenceRecord | None,
    pack: Pack,
    battery: Battery,
    stopped: str | None = None,
    now: datetime | None = None,
) -> GateResult:
    """First failing rule wins; the order is the order a careful technician would check."""
    if step not in APPROVED:
        return _block(step, "unknown_step", "That is not a power step I can approve.", "Yeh power step main approve nahi kar sakta.")
    if stopped:
        return _block(step, "session_stopped", "The session is stopped for safety.", "Safety ke liye session ruka hua hai.")
    if battery != "removed":
        return _block(
            step,
            "battery_not_removed",
            "Remove the battery first and tell me when it is out.",
            "Pehle battery nikaalo, phir batao.",
        )
    sp = pack.safe_power
    if supply is None or supply.kind != "supply" or not supply.fields:
        return _block(step, "no_supply_reading", "I need to read the supply first.", "Pehle supply padhna hoga.")
    if supply.age_s(now) >= STALE_S:
        return _block(step, "stale_reading", "The supply reading is old; I need to read it again.", "Supply reading purani hai, dobara padhna hoga.")
    volts = supply.fields.get("set_voltage")
    limit = supply.fields.get("current_limit")
    if volts is None or volts.value is None:
        return _block(step, "voltage_not_visible", "I can't see the supply voltage.", "Supply voltage dikh nahi raha.")
    if volts.confidence < MIN_CONFIDENCE:
        return _block(step, "voltage_unclear", "The supply voltage is not clear; tilt the light or read it out.", "Supply voltage saaf nahi dikh raha; light ghumao ya bolke batao.")
    if limit is None or limit.value is None or limit.value <= 0:
        return _block(
            step,
            "limit_not_set",
            f"Set the current limit to {_v(sp.current_limit_target)} amp first.",
            f"Pehle current limit {_v(sp.current_limit_target)} amp pe set karo.",
            current_limit=sp.current_limit_target,
        )
    if limit.confidence < MIN_CONFIDENCE:
        return _block(step, "limit_unclear", "The current limit is not clear; tilt the light or read it out.", "Current limit saaf nahi dikh raha; light ghumao ya bolke batao.")
    if not sp.voltage_min <= volts.value <= sp.voltage_max:
        return _block(
            step,
            "voltage_out_of_band",
            f"Set the supply to {_v(sp.voltage_target)} volts. It reads {_v(volts.value)} volts.",
            f"Supply {_v(sp.voltage_target)} volt pe set karo. Abhi {_v(volts.value)} volt hai.",
            set_voltage=sp.voltage_target,
        )
    if limit.value > sp.current_limit_max:
        return _block(
            step,
            "limit_too_high",
            f"Set the current limit to {_v(sp.current_limit_target)} amp. It reads {_v(limit.value)} amps.",
            f"Current limit {_v(sp.current_limit_target)} amp pe set karo. Abhi {_v(limit.value)} amp hai.",
            current_limit=sp.current_limit_target,
        )
    return GateResult(True, step, None, dict(APPROVED[step]))
