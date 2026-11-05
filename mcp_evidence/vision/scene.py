"""The fake overhead camera's view of the bench: the supply and the meter, in perspective, on a
textured bench, rendered from the instruments' live state. Used until the real cameras arrive
(docs/PENDING_RUNS.md) so the whole vision path (calibration, readers, agreement, pre-read,
boot watch) runs end to end without hardware.

The display styles and layout are fixed per scene seed; only the displayed values change, plus
sensor noise in every frame. `offset` moves the whole view (a bumped camera); `hand` puts a hand
over the meter; `glare` puts a highlight on it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from mcp_evidence.synth import distort
from mcp_evidence.synth.displays import FieldSpec, MeterReading, random_style, render_meter, render_supply
from mcp_evidence.synth.segments import layout

from .calibration import Calibration, Roi

SIZE = (1280, 720)


@dataclass
class InstrumentState:
    set_voltage: str = "4.00"
    live_current: str = "0.000"
    current_limit: str = "1.00"
    cc: bool = False
    meter_text: str = "0.000"  # as displayed; "OL" for open
    meter_unit: str = "diode_V"
    meter_mode: str = "diode"
    meter_on: bool = True  # False: blank (dead meter battery)
    knob: str | None = None  # the meter's knob on another mode than the point needs
    hand: bool = False
    glare: bool = False
    offset: tuple[int, int] = (0, 0)  # camera bumped by this many pixels

    def key(self) -> tuple:
        return (self.set_voltage, self.live_current, self.current_limit, self.cc, self.meter_text, self.meter_unit, self.meter_mode,
                self.meter_on, self.knob, self.hand, self.glare, self.offset)


def _place(canvas: np.ndarray, img: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Warp img onto the canvas at the quad dst (tl, tr, br, bl); returns the homography."""
    h, w = img.shape[:2]
    src = np.array([(0, 0), (w, 0), (w, h), (0, h)], np.float32)
    M = cv2.getPerspectiveTransform(src, dst.astype(np.float32))
    warped = cv2.warpPerspective(img, M, (canvas.shape[1], canvas.shape[0]))
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), M, (canvas.shape[1], canvas.shape[0])) > 0
    canvas[mask] = warped[mask]
    return M


@dataclass
class BenchScene:
    seed: int = 7
    size: tuple[int, int] = SIZE
    _cache: dict = field(default_factory=dict)
    _frame_n: int = 0

    def __post_init__(self) -> None:
        rng = np.random.default_rng(self.seed)
        self.supply_style = random_style(rng, "led", "upright")
        self.meter_style = random_style(rng, "lcd", "italic")
        W, H = self.size
        bench = np.full((H, W, 3), (70, 88, 104), np.float32)
        bench += rng.normal(0, 6, (H, W, 1)).astype(np.float32)
        for _ in range(60):  # tools, screws, solder spools: texture for the camera-moved check
            x, y = int(rng.integers(0, W)), int(rng.integers(0, H))
            c = tuple(float(v) for v in rng.uniform(30, 200, 3))
            if rng.random() < 0.5:
                cv2.rectangle(bench, (x, y), (x + int(rng.integers(8, 60)), y + int(rng.integers(8, 60))), c, -1)
            else:
                cv2.circle(bench, (x, y), int(rng.integers(4, 25)), c, -1)
        self.background = np.clip(bench, 0, 255).astype(np.uint8)
        self.supply_quad = np.array([(90, 120), (560, 135), (550, 520), (80, 500)], np.float32)
        self.meter_quad = np.array([(700, 200), (1180, 190), (1190, 470), (705, 480)], np.float32)

    # --- rendering ----------------------------------------------------------------------------

    def _displays(self, s: InstrumentState):
        rng = np.random.default_rng(self.seed + 1)
        specs = {k: FieldSpec(layout(getattr(s, k), 4, self.supply_style.leading_zero)) for k in ("set_voltage", "live_current", "current_limit")}
        sup = render_supply(rng, self.supply_style, specs, cc=s.cc)
        rng = np.random.default_rng(self.seed + 2)
        ol = s.meter_text == "OL"
        cells = layout("OL " if ol else s.meter_text, 4) if s.meter_on else layout("", 4)
        met = render_meter(rng, self.meter_style, MeterReading(s.knob or s.meter_mode, s.meter_text, s.meter_unit, ol=ol), FieldSpec(cells))
        return sup, met

    def _render_clean(self, s: InstrumentState) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        k = s.key()
        if k in self._cache:
            return self._cache[k]
        sup, met = self._displays(s)
        canvas = self.background.copy()
        quads: dict[str, np.ndarray] = {}
        for name, r, dst in (("supply", sup, self.supply_quad), ("meter", met, self.meter_quad)):
            M = _place(canvas, r.image, dst)
            tf = lambda q: cv2.perspectiveTransform(np.asarray(q, np.float32).reshape(-1, 1, 2), M).reshape(-1, 2)  # noqa: E731
            quads[name] = tf(r.glass)
            for f, q in r.fields.items():
                quads[f"{name}.{f}"] = tf(q)
        if s.glare:
            canvas = distort.glare(canvas, quads["meter.main"], np.random.default_rng(self.seed + 3), 0.9)
        if s.hand:
            canvas, _ = distort.hand(canvas, quads["meter.main"], np.random.default_rng(self.seed + 4), 0.8)
        if s.offset != (0, 0):
            M = np.float32([[1, 0, s.offset[0]], [0, 1, s.offset[1]]])
            canvas = cv2.warpAffine(canvas, M, (canvas.shape[1], canvas.shape[0]), borderMode=cv2.BORDER_REPLICATE)
            quads = {k: q + np.array(s.offset, np.float32) for k, q in quads.items()}
        if len(self._cache) > 64:
            self._cache.clear()
        self._cache[k] = (canvas, quads)
        return canvas, quads

    def render(self, s: InstrumentState) -> np.ndarray:
        """One camera frame: the clean scene plus this frame's sensor noise."""
        canvas, _ = self._render_clean(s)
        self._frame_n += 1
        noise = np.random.default_rng([self.seed, self._frame_n]).normal(0, 2.0, canvas.shape).astype(np.float32)
        return np.clip(canvas.astype(np.float32) + noise, 0, 255).astype(np.uint8)

    def true_quads(self, s: InstrumentState | None = None) -> dict[str, np.ndarray]:
        return self._render_clean(s or InstrumentState())[1]

    def calibration(self, camera: str = "overhead", jitter: float = 0.0, seed: int = 0) -> Calibration:
        """What a careful four-corner calibration on the screen would give (with optional error)."""
        s = InstrumentState()
        quads = self.true_quads(s)
        rng = np.random.default_rng(seed)
        rois = {}
        for name, q in quads.items():
            qq = q + (rng.uniform(-jitter, jitter, q.shape).astype(np.float32) if jitter else 0)
            rois[name] = Roi(name, qq, 4, "display" if "." not in name else "field")
        cal = Calibration(camera, rois)
        cal.set_reference(self.render(s))
        return cal
