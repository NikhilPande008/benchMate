"""The board view's images until real ones exist: a synthetic reference board per pack side, and a
fake microscope that looks at it (ARCHITECTURE.md §5.4, best effort).

- `reference_board(pack, side)`: a drawn phone board (solder mask, chips, passives, connectors,
  vias, traces, silkscreen) with a test pad at each of the pack's points on that side, in the
  pack's own coordinates. It is a stand-in for the reference photo the pack will carry
  (docs/PENDING_RUNS.md); the screen labels it as synthetic.
- `Microscope`: the live view: a zoomed window around where the board sits under the
  microscope, slightly rotated, with sensor noise and the probe tip near the point.
- `align(reference, live)`: ORB features + RANSAC homography from the reference to the live view,
  so the next point can be marked on the live frame. None when it can't be trusted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import cv2
import numpy as np

from mcp_evidence.packs.schema import Pack

BOARD_SIZE = (640, 1040)  # width, height of the reference image (pack coordinates)


@lru_cache(maxsize=16)
def _board(pack_id: str, side: str, points: tuple[tuple[str, int, int], ...], seed: int = 3) -> np.ndarray:
    W, H = BOARD_SIZE
    rng = np.random.default_rng([seed, sum(map(ord, pack_id + side))])
    img = np.full((H, W, 3), (40, 40, 40), np.uint8)
    cv2.rectangle(img, (20, 20), (W - 20, H - 20), (38, 92, 34), -1)  # solder mask
    cv2.rectangle(img, (20, 20), (W - 20, H - 20), (60, 120, 52), 3)
    for _ in range(140):  # traces
        x, y = int(rng.integers(30, W - 30)), int(rng.integers(30, H - 30))
        dx, dy = int(rng.integers(-120, 120)), int(rng.integers(-120, 120))
        pts = np.array([(x, y), (x + dx, y), (x + dx, y + dy)], np.int32)
        cv2.polylines(img, [pts], False, (46, 110, 42), int(rng.integers(1, 4)))
    for _ in range(260):  # vias
        c = (int(rng.integers(30, W - 30)), int(rng.integers(30, H - 30)))
        cv2.circle(img, c, int(rng.integers(2, 5)), (90, 170, 190), -1)
        cv2.circle(img, c, 1, (25, 25, 25), -1)
    for _ in range(18):  # chips
        w, h = int(rng.integers(40, 120)), int(rng.integers(40, 120))
        x, y = int(rng.integers(40, W - 40 - w)), int(rng.integers(40, H - 40 - h))
        cv2.rectangle(img, (x, y), (x + w, y + h), (22, 22, 24), -1)
        for k in range(4, w - 4, 8):
            cv2.rectangle(img, (x + k, y - 5), (x + k + 4, y), (170, 170, 175), -1)
            cv2.rectangle(img, (x + k, y + h), (x + k + 4, y + h + 5), (170, 170, 175), -1)
        cv2.putText(img, f"U{int(rng.integers(100, 999))}", (x + 6, y + h // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (120, 120, 120), 1, cv2.LINE_AA)
    for _ in range(120):  # passives
        w, h = (14, 7) if rng.random() < 0.5 else (7, 14)
        x, y = int(rng.integers(30, W - 30)), int(rng.integers(30, H - 30))
        col = (120, 170, 200) if rng.random() < 0.6 else (40, 40, 40)
        cv2.rectangle(img, (x, y), (x + w, y + h), col, -1)
        cv2.rectangle(img, (x, y), (x + 3, y + h), (200, 200, 205), -1)
    if side == "top":  # battery connector
        cv2.rectangle(img, (360, 180), (470, 220), (28, 28, 28), -1)
        for k in range(8):
            cv2.rectangle(img, (366 + k * 13, 186), (374 + k * 13, 214), (80, 180, 215), -1)
    else:  # charging port
        cv2.rectangle(img, (250, 940), (390, 1000), (180, 180, 185), -1)
        cv2.rectangle(img, (262, 952), (378, 988), (60, 60, 60), -1)
    cv2.putText(img, f"BM-{pack_id.upper()[:10]} {side.upper()} REV A", (40, H - 40), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (225, 225, 220), 1, cv2.LINE_AA)
    for pid, x, y in points:  # test pads at the pack's points
        cv2.circle(img, (x, y), 7, (60, 175, 215), -1)
        cv2.circle(img, (x, y), 7, (30, 60, 80), 1)
        cv2.putText(img, pid[:10], (x + 10, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (230, 230, 225), 1, cv2.LINE_AA)
    return img


def reference_board(pack: Pack, side: str) -> np.ndarray:
    pts = tuple((p.id, int(p.xy[0]), int(p.xy[1])) for p in pack.points if p.side == side and p.xy)
    return _board(pack.pack_id, side, pts).copy()


@dataclass
class Microscope:
    """A fake microscope over the reference board: a window of `view` board pixels around the
    focus, shown at `size`, rotated by `angle` degrees, with noise and the probe tip."""

    size: tuple[int, int] = (800, 600)
    view: tuple[int, int] = (360, 270)
    angle: float = 3.0
    shift: tuple[float, float] = (14.0, -9.0)  # where the board sits relative to the focus
    seed: int = 5
    probe: bool = True
    _n: int = 0
    last_transform: np.ndarray | None = field(default=None, repr=False)

    def transform(self, focus: tuple[float, float]) -> np.ndarray:
        """Reference → live (3×3)."""
        W, H = self.size
        s = W / self.view[0]
        cx, cy = focus[0] + self.shift[0], focus[1] + self.shift[1]
        M = cv2.getRotationMatrix2D((cx, cy), self.angle, s)
        M[0, 2] += W / 2 - cx
        M[1, 2] += H / 2 - cy
        return np.vstack([M, [0, 0, 1]])

    def frame(self, reference: np.ndarray, focus: tuple[float, float] | None, tip: tuple[float, float] | None = None) -> np.ndarray:
        focus = focus or (reference.shape[1] / 2, reference.shape[0] / 2)
        T = self.transform(focus)
        self.last_transform = T
        out = cv2.warpPerspective(reference, T, self.size, flags=cv2.INTER_LINEAR, borderValue=(25, 25, 25))
        if self.probe and tip is not None:
            p = cv2.perspectiveTransform(np.array([[tip]], np.float32), T)[0, 0]
            end = (int(p[0] + 4), int(p[1] + 3))
            cv2.line(out, (self.size[0] - 40, self.size[1] + 40), end, (150, 150, 155), 9, cv2.LINE_AA)
            cv2.circle(out, end, 4, (235, 235, 240), -1, cv2.LINE_AA)
        self._n += 1
        noise = np.random.default_rng([self.seed, self._n]).normal(0, 3.0, out.shape).astype(np.float32)
        return np.clip(out.astype(np.float32) + noise, 0, 255).astype(np.uint8)


@lru_cache(maxsize=8)
def _features(key: int, gray_bytes: bytes, shape: tuple[int, int]):
    gray = np.frombuffer(gray_bytes, np.uint8).reshape(shape)
    return cv2.ORB_create(nfeatures=3000, fastThreshold=12).detectAndCompute(gray, None)


def align(reference: np.ndarray, live: np.ndarray, min_inliers: int = 25) -> np.ndarray | None:
    """Homography reference → live, or None when the match can't be trusted."""
    g0 = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    g1 = cv2.cvtColor(live, cv2.COLOR_BGR2GRAY)
    kp0, d0 = _features(hash(g0.tobytes()), g0.tobytes(), g0.shape)
    kp1, d1 = cv2.ORB_create(nfeatures=1500, fastThreshold=12).detectAndCompute(g1, None)
    if d0 is None or d1 is None or len(kp1) < min_inliers:
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(d1, d0, k=2)
    good = [m for m, n in (p for p in matches if len(p) == 2) if m.distance < 0.8 * n.distance]
    if len(good) < min_inliers:
        return None
    src = np.float32([kp0[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
    if H is None or int(inl.sum()) < min_inliers:
        return None
    return H


def map_point(H: np.ndarray, xy: tuple[float, float]) -> tuple[float, float]:
    p = cv2.perspectiveTransform(np.array([[xy]], np.float32), H)[0, 0]
    return float(p[0]), float(p[1])
