"""Context manager (BUILD_GUIDE, Latency engineering 11–12): keep the voice session short and fast.

The engine holds the real state (ledger, evidence), so old audio can be dropped without losing
anything that matters:

- the session config truncates with `retention_ratio` 0.8 and a `post_instructions` token cap
  (engine/api/realtime.py);
- every few user turns the engine injects a compact summary of the session as an engine note;
- the page rotates the realtime session after ~25 turns or ~30 minutes, at a natural pause, into
  a session pre-opened in the background and seeded with the same summary; a dropped connection
  reconnects the same way (apps/web/src/realtime/rotation.ts). The engine says when a pause is
  natural: no power approval live, no read-back waiting, no boot watch running.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from engine.session.repair import RepairSession

SUMMARY_EVERY_TURNS = 6
ROTATE_AFTER_TURNS = 25
ROTATE_AFTER_S = 30 * 60
MAX_SUMMARY_CHARS = 700


def _num(v: float | None, unit: str | None, is_open: bool = False) -> str:
    if is_open:
        return "OL"
    if v is None:
        return "?"
    u = {"diode_V": "V", "ohm": "ohm"}.get(unit or "", unit or "")
    return f"{v:.3f} {u}".strip() if abs(v) < 10 else f"{v:.2f} {u}".strip()


def summary_text(r: RepairSession) -> str:
    """The session in a few lines, for the voice model. Facts only, from the engine's state."""
    if r.pack is None or r.ledger is None:
        return "Session summary: no repair started yet. Ask for the phone model and the symptom, then call start_repair."
    parts = [f"Session summary (the engine holds the full state). Phone: {r.pack.name.en}; symptom: {r.symptom or 'unknown'}; "
             f"battery: {r.battery}; reply language: {'Hinglish' if r.lng == 'hi' else 'English'}; state: {r.state}."]
    top = r.ledger.ranked()[:3]
    names = {f.id: f.name.en for f in r.pack.faults}
    parts.append("Top faults: " + ", ".join(f"{names.get(h, 'unknown fault')} {round(p * 100)}%" for h, p in top) + ".")
    readings = []
    for e in r.evidence[-6:]:
        if e.kind == "supply" and e.fields:
            v, lim = e.fields.get("set_voltage"), e.fields.get("current_limit")
            readings.append(f"supply {_num(v.value if v else None, 'V')}, limit {_num(lim.value if lim else None, 'A')}")
        elif e.kind == "boot_trace":
            readings.append(f"boot current {e.outcome or (e.trace.pattern if e.trace else '?')}")
        elif e.measurement_id:
            readings.append(f"{e.measurement_id} {_num(e.value, e.unit, e.open) if e.kind != 'observation' else ''} {e.outcome or ''} ({e.source})".replace("  ", " "))
    if readings:
        parts.append("Recent readings: " + "; ".join(readings) + ".")
    if r.state == "fix" and r.fix_fault:
        parts.append(f"Fix proposed: {names.get(r.fix_fault, r.fix_fault)}; confirm with {r.fix_check} before closing.")
    elif r.pending_step:
        m = r.pack.measurement(r.pending_step.measurement_id)
        step = f"Next step: {m.label.en}"
        if m.requires_power:
            step += " (needs a power step: only the safety gate's sentence may be said)"
        parts.append(step + ".")
    if r.stopped:
        parts.append(f"SAFETY STOP: {r.stopped}. Give no further steps.")
    parts.append("Call next_step before the next instruction.")
    text = " ".join(parts)
    return text if len(text) <= MAX_SUMMARY_CHARS else text[: MAX_SUMMARY_CHARS - 1] + "…"


def pause_ok(r: RepairSession) -> tuple[bool, str | None]:
    """Whether now is a natural pause for swapping the voice session."""
    if r.approvals.pending is not None:
        return False, "power_approval_live"
    if r.readback_pending is not None:
        return False, "readback_waiting"
    if r._watch is not None and not r._watch.done():
        return False, "watch_running"
    return True, None


@dataclass
class ContextManager:
    summary_every: int = SUMMARY_EVERY_TURNS
    turns: int = 0
    last_summary_turn: int = 0
    rotations: int = 0
    reconnects: int = 0

    def on_user_turn(self) -> bool:
        """Count a user turn; True when it's time for a summary note."""
        self.turns += 1
        if self.turns - self.last_summary_turn >= self.summary_every:
            self.last_summary_turn = self.turns
            return True
        return False

    def on_rotated(self, reason: str) -> None:
        if reason == "reconnect":
            self.reconnects += 1
        else:
            self.rotations += 1
        self.last_summary_turn = self.turns  # the new session was seeded with a summary just now

    def line(self) -> dict[str, Any]:
        return {"turns": self.turns, "rotations": self.rotations, "reconnects": self.reconnects}
