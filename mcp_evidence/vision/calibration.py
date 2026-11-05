"""Display calibration (ARCHITECTURE.md §5.2 step 1) and the "camera moved" check.

Once per setup the user drags four corners around each display and each digit field on the
companion screen. Each region (ROI) stores its homography to a flat, fixed-size crop:

- display ROIs ("supply", "meter") → crops for Reader A (the vision model reads the whole display);
- field ROIs ("supply.set_voltage", "supply.live_current", "supply.current_limit", "meter.main")
  → crops for Reader B (one digit row each, with its number of digit positions).

The calibration also keeps a reference frame. ORB features matched against it tell whether the
camera was moved or bumped (BUILD_GUIDE corner case): then camera reads pause until recalibration.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

FIELD_H = 96  # Reader B crop height
DISPLAY_W = 512  # Reader A crop width (BUILD_GUIDE: "start around 512 px wide")
MOVED_PX = 6.0  # ROI corners displaced more than this → moved
MIN_MATCHES = 12

SUPPLY_FIELDS = ("set_voltage", "live_current", "current_limit")


def _order(corners: np.ndarray) -> np.ndarray:
    """tl, tr, br, bl, whatever order they were dragged in."""
    c = np.asarray(corners, np.float32).reshape(4, 2)
    s, d = c.sum(axis=1), np.diff(c, axis=1).ravel()
    return np.array([c[np.argmin(s)], c[np.argmin(d)], c[np.argmax(s)], c[np.argmax(d)]], np.float32)


@dataclass
class Roi:
    name: str  # "supply", "meter", "supply.set_voltage", "meter.main", ...
    corners: np.ndarray  # 4×2 in camera pixels (tl, tr, br, bl)
    digits: int = 4  # field ROIs: digit positions in the row
    kind: str = "field"  # "field" | "display"

    def __post_init__(self) -> None:
        self.corners = _order(self.corners)

    @property
    def out_size(self) -> tuple[int, int]:
        c = self.corners
        w = (np.linalg.norm(c[1] - c[0]) + np.linalg.norm(c[2] - c[3])) / 2
        h = (np.linalg.norm(c[3] - c[0]) + np.linalg.norm(c[2] - c[1])) / 2
        if self.kind == "display":
            return DISPLAY_W, max(16, int(round(DISPLAY_W * h / max(w, 1))))
        return max(16, int(round(FIELD_H * w / max(h, 1)))), FIELD_H

    def homography(self) -> np.ndarray:
        w, h = self.out_size
        dst = np.array([(0, 0), (w, 0), (w, h), (0, h)], np.float32)
        return cv2.getPerspectiveTransform(self.corners, dst)

    def warp(self, image: np.ndarray) -> np.ndarray:
        return cv2.warpPerspective(image, self.homography(), self.out_size, flags=cv2.INTER_AREA if self.kind == "display" else cv2.INTER_LINEAR,
                                   borderMode=cv2.BORDER_REPLICATE)

    def to_dict(self) -> dict:
        return {"name": self.name, "corners": np.round(self.corners, 2).tolist(), "digits": self.digits, "kind": self.kind,
                "homography": np.round(self.homography(), 6).tolist(), "out_size": list(self.out_size)}


@dataclass
class MoveCheck:
    moved: bool
    shift_px: float  # largest ROI corner displacement
    matches: int
    reason: str | None = None


@dataclass
class Calibration:
    camera: str
    rois: dict[str, Roi]
    reference: np.ndarray | None = None  # gray reference frame for the moved check
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    _ref_features: tuple | None = None

    def roi(self, name: str) -> Roi:
        return self.rois[name]

    def crop(self, image: np.ndarray, name: str) -> np.ndarray:
        return self.rois[name].warp(image)

    def fields_of(self, display: str) -> list[Roi]:
        return [r for n, r in self.rois.items() if n.startswith(display + ".")]

    # --- moved check --------------------------------------------------------------------------

    @staticmethod
    def _features(gray: np.ndarray):
        orb = cv2.ORB_create(nfeatures=1500, fastThreshold=10)
        return orb.detectAndCompute(gray, None)

    def set_reference(self, frame: np.ndarray) -> None:
        self.reference = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        self._ref_features = None

    def moved(self, frame: np.ndarray) -> MoveCheck:
        """Match the frame against the calibration frame; estimate how far the ROIs moved."""
        if self.reference is None:
            return MoveCheck(False, 0.0, 0, "no_reference")
        if self._ref_features is None:
            self._ref_features = self._features(self.reference)
        kp0, d0 = self._ref_features
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        kp1, d1 = self._features(gray)
        if d0 is None or d1 is None or len(kp0) < MIN_MATCHES or len(kp1) < MIN_MATCHES:
            return MoveCheck(True, float("inf"), 0, "too_few_features")
        matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(d0, d1)
        if len(matches) < MIN_MATCHES:
            return MoveCheck(True, float("inf"), len(matches), "too_few_matches")
        src = np.float32([kp0[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
        dst = np.float32([kp1[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
        H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
        n_in = int(inl.sum()) if inl is not None else 0
        if H is None or n_in < MIN_MATCHES:
            return MoveCheck(True, float("inf"), n_in, "no_consistent_motion")
        corners = np.concatenate([r.corners for r in self.rois.values()]).reshape(-1, 1, 2)
        shift = float(np.linalg.norm(cv2.perspectiveTransform(corners, H) - corners, axis=2).max())
        return MoveCheck(shift > MOVED_PX, shift, n_in, "moved" if shift > MOVED_PX else None)

    # --- persistence --------------------------------------------------------------------------

    def to_json(self) -> dict:
        ref = None
        if self.reference is not None:
            ok, buf = cv2.imencode(".png", self.reference)
            ref = base64.b64encode(buf.tobytes()).decode() if ok else None
        return {"camera": self.camera, "created_at": self.created_at, "rois": [r.to_dict() for r in self.rois.values()], "reference_png": ref}

    @classmethod
    def from_json(cls, d: dict) -> "Calibration":
        rois = {r["name"]: Roi(r["name"], np.array(r["corners"], np.float32), r.get("digits", 4), r.get("kind", "field")) for r in d["rois"]}
        ref = None
        if d.get("reference_png"):
            ref = cv2.imdecode(np.frombuffer(base64.b64decode(d["reference_png"]), np.uint8), cv2.IMREAD_GRAYSCALE)
        return cls(d["camera"], rois, ref, d.get("created_at") or datetime.now(timezone.utc).isoformat())


class CalibrationStore:
    """One JSON file per camera under the git-ignored data folder."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, camera: str) -> Path:
        return self.root / f"calibration-{camera}.json"

    def save(self, cal: Calibration) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.path(cal.camera)
        p.write_text(json.dumps(cal.to_json()))
        return p

    def load(self, camera: str) -> Calibration | None:
        p = self.path(camera)
        return Calibration.from_json(json.loads(p.read_text())) if p.exists() else None
