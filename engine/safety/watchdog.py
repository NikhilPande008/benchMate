"""At-limit watchdog: the supply sitting at its current limit for more than `hold_s` means a hard
short; the engine pushes an urgent "disconnect now" (ARCHITECTURE.md §6.5)."""
from __future__ import annotations

from dataclasses import dataclass

DISCONNECT_NOW = {
    "en": "Disconnect the supply now. The board is holding the current limit.",
    "hi": "Abhi supply disconnect karo. Board current limit pe atka hai.",
}


@dataclass
class AtLimitWatchdog:
    limit_a: float
    fraction: float = 0.95
    hold_s: float = 3.0
    _since: float | None = None
    tripped: bool = False

    def feed(self, t_s: float, current_a: float) -> bool:
        """True exactly once: the first sample after more than hold_s continuously at the limit."""
        if self.tripped:
            return False
        if current_a >= self.fraction * self.limit_a:
            self._since = t_s if self._since is None else self._since
            if t_s - self._since > self.hold_s:
                self.tripped = True
                return True
        else:
            self._since = None
        return False
