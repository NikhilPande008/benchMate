"""Pre-read on stability (CLAUDE.md decided settings; BUILD_GUIDE latency technique 3).

While the camera runs, each new frame's field crops are compared with the last ones. Nothing
changed → no work. A change → the display is unstable; once it has been unchanged for two frames,
both readers run on the ring buffer's frames (the full agreement rule) and the agreed value is
cached. `get_reading` then answers from the cache in milliseconds, as long as nothing changed
since. Change detection uses the largest local difference (blurred), not the mean, so a decimal
point appearing is a change even though it is a few pixels.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from .agreement import DisplayResult, DisplaySpec, read_display
from .frames import CameraBase, Frame
from .reader_a import ReaderA

CHANGE_THRESHOLD = 25.0  # gray levels, after a light blur
CACHE_MAX_AGE_MS = 5000.0


def change(a: np.ndarray | None, b: np.ndarray) -> float:
    if a is None or a.shape != b.shape:
        return float("inf")
    d = cv2.GaussianBlur(cv2.absdiff(a, b).astype(np.float32), (0, 0), 1.5)
    return float(d.max())


def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


@dataclass
class PreReader:
    spec: DisplaySpec
    camera: CameraBase
    reader_a: ReaderA | None
    level: str = "full"
    threshold: float = CHANGE_THRESHOLD
    cache: DisplayResult | None = None
    cache_t: float = 0.0
    last: dict[str, np.ndarray] = field(default_factory=dict)
    stable_frames: int = 0
    changed_at: float = 0.0
    reads: int = 0
    skipped: int = 0
    _task: asyncio.Task | None = None
    _seq: int = -1

    def _crops(self, f: Frame) -> dict[str, np.ndarray]:
        return {k: _gray(v) for k, v in self.spec.crops(f.image).items() if k != "display"}

    def observe(self, f: Frame) -> bool:
        """Feed one frame; True if the display changed."""
        crops = self._crops(f)
        changed = any(change(self.last.get(k), v) > self.threshold for k, v in crops.items())
        self.last = crops
        if changed:
            self.stable_frames = 0
            self.changed_at = time.monotonic()
            self.cache = None
        else:
            self.stable_frames += 1
            self.skipped += 1
        return changed

    def get(self, max_age_ms: float = CACHE_MAX_AGE_MS) -> DisplayResult | None:
        if self.cache is None or (time.monotonic() - self.cache_t) * 1000 > max_age_ms:
            return None
        return self.cache

    async def step(self) -> None:
        """Process the newest frame; read when the display has just become stable."""
        f = await self.camera.next_frame(self._seq, timeout_s=1.0)
        if f is None:
            return
        self._seq = f.seq
        self.observe(f)
        if self.stable_frames >= 2 and self.cache is None:
            frames = [g for g in self.camera.ring.since(f.t_ms - 400)]
            pick = frames[-3:] if len(frames) >= 3 else frames

            async def nxt(i: int) -> np.ndarray | None:
                if i < len(pick):
                    return pick[i].image
                g = await self.camera.next_frame(pick[-1].seq if pick else -1, timeout_s=1.0)
                return g.image if g else None

            res = await read_display(self.spec, nxt, self.reader_a, self.level)
            self.reads += 1
            if res.ok and self.stable_frames >= 2:
                self.cache, self.cache_t = res, time.monotonic()

    async def run(self) -> None:
        while True:
            await self.step()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name=f"preread:{self.spec.display}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
