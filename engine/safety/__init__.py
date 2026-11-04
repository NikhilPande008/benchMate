"""Safety gate, scope checks and the at-limit watchdog (CLAUDE.md rule 2)."""
from .gate import APPROVED, MIN_CONFIDENCE, STALE_S, Approvals, GateResult, check_power
from .scope import REFUSE_DEVICE, STOP_MESSAGE, battery_report, device_out_of_scope
from .watchdog import DISCONNECT_NOW, AtLimitWatchdog

__all__ = [
    "APPROVED",
    "DISCONNECT_NOW",
    "MIN_CONFIDENCE",
    "REFUSE_DEVICE",
    "STALE_S",
    "STOP_MESSAGE",
    "Approvals",
    "AtLimitWatchdog",
    "GateResult",
    "battery_report",
    "check_power",
    "device_out_of_scope",
]
