"""Degradation ladder for camera readings (BUILD_GUIDE, Reliability): full → Reader B only →
spoken only. Each change is an event for the engine (an engine note to the voice model) and the
screen ("Camera reading limited").

- Reader A failing (timeout or error on every field) twice in a row → `reader_b_only`. While
  there, Reader A is probed again every `probe_s`; one good answer → back to `full`.
- No camera frames, or the camera moved since calibration → `spoken_only` until frames are back
  and the view matches the calibration again.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

LEVELS = ("full", "reader_b_only", "spoken_only")
SAY = {
    "full": {"en": "Camera readings are back to normal.", "hi": "Camera readings phir se normal hain."},
    "reader_b_only": {"en": "Camera reading is limited: only the local reader works, so I will read each value back to you.",
                      "hi": "Camera reading limited hai: sirf local reader chal raha hai, har value main aapko bolke confirm karunga."},
    "spoken_only": {"en": "The camera can't read the displays now; please read the values aloud.",
                    "hi": "Camera abhi display nahi padh pa raha; values bolke batao."},
}

OnLevel = Callable[[str, str | None], None]


@dataclass
class VisionHealth:
    a_fail_limit: int = 2
    probe_s: float = 30.0
    level: str = "full"
    reason: str | None = None
    a_failures: int = 0
    camera_ok: bool = True
    listeners: list[OnLevel] = field(default_factory=list)
    clock: Callable[[], float] = time.monotonic
    _last_probe: float = 0.0
    history: list[tuple[str, str | None]] = field(default_factory=list)

    def _set(self, level: str, reason: str | None) -> None:
        if level == self.level:
            return
        self.level, self.reason = level, reason
        self.history.append((level, reason))
        if level == "reader_b_only":
            self._last_probe = self.clock()  # the next Reader A probe is one interval from now
        for fn in self.listeners:
            fn(level, reason)

    def _settle(self) -> None:
        if not self.camera_ok:
            self._set("spoken_only", self.reason if self.level == "spoken_only" else "camera")
        elif self.a_failures >= self.a_fail_limit:
            self._set("reader_b_only", "vision_api")
        else:
            self._set("full", None)

    def a_result(self, calls: int, errors: int, fields: int) -> None:
        """After a read: `errors` failed fields out of `fields` per call."""
        if calls == 0:
            return
        if errors >= calls * fields:
            self.a_failures += 1
        else:
            self.a_failures = 0
        self._settle()

    def camera(self, ok: bool, reason: str | None = None) -> None:
        if ok == self.camera_ok and (ok or self.reason == reason):
            return
        self.camera_ok = ok
        if not ok:
            self._set("spoken_only", reason or "camera")
        else:
            self._settle()

    def use_reader_a(self) -> bool:
        """In `reader_b_only`, try Reader A again once per probe interval."""
        if self.level == "full":
            return True
        if self.level == "reader_b_only" and self.clock() - self._last_probe >= self.probe_s:
            self._last_probe = self.clock()
            return True
        return False
