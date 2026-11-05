"""Cameras and the frame ring buffer (BUILD_GUIDE, Latency engineering: "Always-on frame ring buffer").

Every camera keeps its last ~2 s of frames, so a reading never waits for a camera to open or
focus: the two frames ~150 ms apart are already there. One interface for all sources:

- `FakeCamera`: plays an image sequence (files or arrays), loops it, or shows one still image;
  or renders frames from a callable (the fake bench scene). Used until hardware arrives.
- `OpenCVCamera`: a real device (or a video file) through cv2.VideoCapture on a worker thread.
  Tested here on video files only; not yet on a real camera (docs/PENDING_RUNS.md).

Capture runs off the event loop (CLAUDE.md: OpenCV on worker threads).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

import cv2
import numpy as np

RING_MS = 2000.0
PAIR_GAP_MS = 150.0


def now_ms() -> float:
    return time.monotonic() * 1000.0


@dataclass(frozen=True)
class Frame:
    camera: str
    seq: int
    t_ms: float  # monotonic clock
    image: np.ndarray  # BGR uint8


class FrameRing:
    """The last `keep_ms` of frames of one camera."""

    def __init__(self, keep_ms: float = RING_MS, max_frames: int = 240):
        self.keep_ms = keep_ms
        self._frames: deque[Frame] = deque(maxlen=max_frames)

    def push(self, f: Frame) -> None:
        self._frames.append(f)
        while self._frames and f.t_ms - self._frames[0].t_ms > self.keep_ms:
            self._frames.popleft()

    def __len__(self) -> int:
        return len(self._frames)

    def latest(self) -> Frame | None:
        return self._frames[-1] if self._frames else None

    def since(self, t_ms: float) -> list[Frame]:
        return [f for f in self._frames if f.t_ms >= t_ms]

    def before(self, f: Frame, gap_ms: float = PAIR_GAP_MS) -> Frame | None:
        """The frame closest to `gap_ms` before f (at least half the gap before it)."""
        older = [g for g in self._frames if g.t_ms <= f.t_ms - gap_ms / 2]
        return min(older, key=lambda g: abs((f.t_ms - g.t_ms) - gap_ms)) if older else None

    def clear(self) -> None:
        self._frames.clear()


class Camera(Protocol):
    name: str
    ring: FrameRing

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    @property
    def healthy(self) -> bool: ...

    async def next_frame(self, after_seq: int = -1, timeout_s: float = 2.0) -> Frame | None: ...


class CameraBase:
    """Shared loop: grab on a worker thread at `fps`, push into the ring, wake waiters."""

    def __init__(self, name: str, fps: float = 10.0, clock: Callable[[], float] = now_ms):
        self.name = name
        self.fps = fps
        self.clock = clock
        self.ring = FrameRing()
        self.errors = 0
        self._seq = 0
        self._task: asyncio.Task | None = None
        self._new = asyncio.Event()

    def _grab(self) -> np.ndarray | None:  # pragma: no cover - overridden
        raise NotImplementedError

    def grab_once(self) -> Frame | None:
        """One frame now, synchronously (tests and the still mode)."""
        img = self._grab()
        if img is None:
            self.errors += 1
            return None
        f = Frame(self.name, self._seq, self.clock(), img)
        self._seq += 1
        self.ring.push(f)
        self._new.set()
        return f

    async def _loop(self) -> None:
        period = 1.0 / self.fps
        while True:
            t0 = time.monotonic()
            f = await asyncio.to_thread(self._grab)
            if f is None:
                self.errors += 1
            else:
                self.ring.push(Frame(self.name, self._seq, self.clock(), f))
                self._seq += 1
                self._new.set()
            await asyncio.sleep(max(0.0, period - (time.monotonic() - t0)))

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name=f"camera:{self.name}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def healthy(self) -> bool:
        f = self.ring.latest()
        return f is not None and self.clock() - f.t_ms < 1000.0

    async def next_frame(self, after_seq: int = -1, timeout_s: float = 2.0) -> Frame | None:
        deadline = time.monotonic() + timeout_s
        while True:
            f = self.ring.latest()
            if f is not None and f.seq > after_seq:
                return f
            self._new.clear()
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                await asyncio.wait_for(self._new.wait(), left)
            except asyncio.TimeoutError:
                return None


class FakeCamera(CameraBase):
    """Frames from files, arrays or a render function, behind the real camera's interface.

    - `FakeCamera.sequence(name, images, loop=True)`: plays images in order (a recorded clip).
    - `FakeCamera.still(name, image)`: the same image every frame (plus nothing else).
    - `FakeCamera(name, render=fn)`: fn() returns the next image (the fake bench scene).
    `fail()` makes it stop delivering, as an unplugged camera would.
    """

    def __init__(self, name: str, render: Callable[[], np.ndarray | None], fps: float = 10.0, clock: Callable[[], float] = now_ms):
        super().__init__(name, fps, clock)
        self._render = render
        self.failed = False

    def _grab(self) -> np.ndarray | None:
        return None if self.failed else self._render()

    def fail(self, failed: bool = True) -> None:
        self.failed = failed

    @classmethod
    def sequence(cls, name: str, images: Sequence[np.ndarray | str | Path], loop: bool = True, fps: float = 10.0,
                 clock: Callable[[], float] = now_ms) -> "FakeCamera":
        frames = [img if isinstance(img, np.ndarray) else cv2.imread(str(img)) for img in images]
        if any(f is None for f in frames):
            raise FileNotFoundError("an image in the sequence could not be read")
        state = {"i": 0}

        def render() -> np.ndarray | None:
            i = state["i"]
            if i >= len(frames):
                if not loop:
                    return frames[-1]
                i = 0
            state["i"] = i + 1
            return frames[i]

        return cls(name, render, fps, clock)

    @classmethod
    def still(cls, name: str, image: np.ndarray | str | Path, fps: float = 10.0, clock: Callable[[], float] = now_ms) -> "FakeCamera":
        img = image if isinstance(image, np.ndarray) else cv2.imread(str(image))
        if img is None:
            raise FileNotFoundError(str(image))
        return cls(name, lambda: img, fps, clock)


class OpenCVCamera(CameraBase):
    """A camera device index or a video file/URL via cv2.VideoCapture. The capture object lives
    on the worker thread that grabs; a failed read counts as an error and is retried."""

    def __init__(self, name: str, source: int | str, fps: float = 15.0, size: tuple[int, int] | None = None,
                 clock: Callable[[], float] = now_ms, loop_file: bool = False):
        super().__init__(name, fps, clock)
        self.source = source
        self.size = size
        self.loop_file = loop_file
        self._cap: cv2.VideoCapture | None = None

    def _open(self) -> cv2.VideoCapture | None:
        cap = cv2.VideoCapture(self.source)
        if not cap.isOpened():
            return None
        if self.size:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
        return cap

    def _grab(self) -> np.ndarray | None:
        if self._cap is None:
            self._cap = self._open()
            if self._cap is None:
                return None
        ok, img = self._cap.read()
        if not ok and self.loop_file:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, img = self._cap.read()
        if not ok:
            self._cap.release()
            self._cap = None
            return None
        return img

    async def stop(self) -> None:
        await super().stop()
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def frames_from(images: Iterable[np.ndarray], camera: str = "test", start_ms: float = 0.0, gap_ms: float = PAIR_GAP_MS) -> list[Frame]:
    """Frames at fixed spacing (for the agreement rule on a labelled sample)."""
    return [Frame(camera, i, start_ms + i * gap_ms, img) for i, img in enumerate(images)]
