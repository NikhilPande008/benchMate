"""Flat renderings of a multimeter LCD and a bench-supply LED panel (synthetic test data).

A rendering is drawn from a `Frame` spec: for each field, which segments of each cell are lit and
how strongly. That is what lets the distortions make flicker (segments missing in one frame) and
mid-update digits (old and new segments at once) without a second renderer.

These are stand-ins: they prove the pipeline, not its accuracy on real displays (BUILD_GUIDE
"Reading instrument displays with vision", point 7).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .segments import Cell, lit_segments, segment_polygons

Color = tuple[float, float, float]  # BGR, 0–255


@dataclass(frozen=True)
class Style:
    """How one display model looks. Randomised per sample so no reader can learn one font."""

    kind: str  # "lcd" (dark segments on grey) | "led" (bright segments on black)
    digit_h: float
    aspect: float  # cell width / height
    thickness: float  # stroke / height
    slant: float
    gap: float  # between segments, / height
    spacing: float  # between cells, / cell width
    bg: Color
    fg: Color
    ghost: float  # 0..1: how visible unlit segments are
    glyph_style: int  # which 6/7/9 variant
    leading_zero: bool
    glow: float  # LED halo strength
    family: str = "upright"


@dataclass
class FieldSpec:
    cells: list[Cell]
    # per cell: segment → intensity (0..1); None means "as the cell's character, fully lit"
    lit: list[dict[str, float]] | None = None
    dp: list[float] | None = None  # per cell decimal point intensity
    style: Style | None = None  # defaults to the display's
    height_scale: float = 1.0

    def resolved(self, glyph_style: int) -> tuple[list[dict[str, float]], list[float]]:
        lit = self.lit or [{s: 1.0 for s in lit_segments(c.ch, glyph_style)} for c in self.cells]
        dp = self.dp or [1.0 if c.dp else 0.0 for c in self.cells]
        return lit, dp


@dataclass
class Rendered:
    image: np.ndarray  # BGR uint8
    glass: np.ndarray  # 4×2 corners of the display window (tl, tr, br, bl)
    fields: dict[str, np.ndarray]  # field name → 4×2 corners of its digit area
    cells: dict[str, list[np.ndarray]] = field(default_factory=dict)  # field → 4×2 corners per digit cell


# Digit-style families. Two are held out from all Reader B tuning (eval/display_set/README.md).
FAMILIES: dict[str, dict[str, tuple[float, float]]] = {
    "upright": {"slant": (0.0, 0.03), "thickness": (0.11, 0.15), "aspect": (0.50, 0.60)},
    "italic": {"slant": (0.08, 0.15), "thickness": (0.11, 0.15), "aspect": (0.50, 0.60)},
    "thin": {"slant": (0.0, 0.10), "thickness": (0.07, 0.10), "aspect": (0.50, 0.60)},
    "bold": {"slant": (0.0, 0.10), "thickness": (0.16, 0.20), "aspect": (0.52, 0.62)},
    "narrow": {"slant": (0.0, 0.10), "thickness": (0.11, 0.15), "aspect": (0.40, 0.47)},
    "wide": {"slant": (0.0, 0.10), "thickness": (0.11, 0.15), "aspect": (0.62, 0.72)},
}


def random_style(rng: np.random.Generator, kind: str, family: str | None = None) -> Style:
    family = family or str(rng.choice(sorted(FAMILIES)))
    fam = FAMILIES[family]
    if kind == "lcd":
        g = rng.uniform(150, 215)
        bg = (g * rng.uniform(0.9, 1.0), g * rng.uniform(0.98, 1.05), g * rng.uniform(0.92, 1.02))
        fg = tuple(rng.uniform(15, 55) for _ in range(3))
        ghost = rng.uniform(0.0, 0.12)
        glow = 0.0
    else:
        bg = tuple(rng.uniform(5, 30) for _ in range(3))
        fg = (rng.uniform(40, 90), rng.uniform(40, 90), 255.0)  # red; field colours override
        ghost = rng.uniform(0.02, 0.12)
        glow = rng.uniform(0.2, 0.7)
    return Style(
        kind=kind,
        digit_h=rng.uniform(52, 78),
        aspect=rng.uniform(*fam["aspect"]),
        thickness=rng.uniform(*fam["thickness"]),
        slant=rng.uniform(*fam["slant"]),
        gap=rng.uniform(0.008, 0.03),
        spacing=rng.uniform(0.25, 0.5),
        bg=bg,  # type: ignore[arg-type]
        fg=fg,  # type: ignore[arg-type]
        ghost=ghost,
        glyph_style=int(rng.integers(0, 2)),
        leading_zero=bool(rng.random() < 0.7),
        glow=glow,
        family=family,
    )


LED_COLORS: dict[str, Color] = {"red": (60, 50, 255), "green": (80, 255, 90), "amber": (40, 190, 255), "blue": (255, 190, 60)}


def _cell_boxes(x: float, y: float, n: int, h: float, st: Style) -> list[tuple[float, float, float, float]]:
    w = h * st.aspect
    step = w * (1 + st.spacing)
    return [(x + i * step, y, w, h) for i in range(n)]


def field_width(n: int, h: float, st: Style) -> float:
    w = h * st.aspect
    return n * w + (n - 1) * w * st.spacing + h * st.slant + 0.25 * w


class Canvas:
    """Collects segments, dots, text and strokes by (colour, intensity), then draws each group into
    one supersampled ink mask and composites them, so lit segments, ghosts and LED glow mix
    correctly and a frame costs a handful of full-size masks, not one per segment."""

    def __init__(self, h: int, w: int, ss: int = 2):
        self.ss = ss
        self.shape = (h * ss, w * ss)
        self.ops: dict[tuple[Color, float], list] = {}

    def _add(self, color: Color, intensity: float, op) -> None:
        if intensity > 0:
            self.ops.setdefault((tuple(float(c) for c in color), round(float(intensity), 3)), []).append(op)  # type: ignore[arg-type]

    def _pts(self, pts: np.ndarray) -> np.ndarray:
        return np.round(pts * self.ss).astype(np.int32)

    def poly(self, pts: np.ndarray, color: Color, intensity: float) -> None:
        q = self._pts(pts)
        self._add(color, intensity, lambda m: cv2.fillPoly(m, [q], 255, cv2.LINE_AA))

    def circle(self, c: tuple[float, float], r: float, color: Color, intensity: float) -> None:
        cc, rr = (int(c[0] * self.ss), int(c[1] * self.ss)), max(1, int(r * self.ss))
        self._add(color, intensity, lambda m: cv2.circle(m, cc, rr, 255, -1, cv2.LINE_AA))

    def text(self, s: str, org: tuple[float, float], scale: float, color: Color, intensity: float, thick: float = 1.6) -> None:
        o, sc, th = (int(org[0] * self.ss), int(org[1] * self.ss)), scale * self.ss, max(1, int(thick * self.ss))
        self._add(color, intensity, lambda m: cv2.putText(m, s, o, cv2.FONT_HERSHEY_SIMPLEX, sc, 255, th, cv2.LINE_AA))

    def strokes(self, lines: list[np.ndarray], color: Color, intensity: float, thick: float) -> None:
        q, th = [self._pts(p) for p in lines], max(1, int(thick * self.ss))
        self._add(color, intensity, lambda m: cv2.polylines(m, q, False, 255, th, cv2.LINE_AA))

    def composite(self, base: np.ndarray, kind: str, glow: float) -> np.ndarray:
        layers: dict[Color, np.ndarray] = {}
        for (color, intensity), ops in sorted(self.ops.items(), key=lambda kv: kv[0][1]):
            mask = np.zeros(self.shape, np.uint8)
            for op in ops:
                op(mask)
            layer = layers.setdefault(color, np.zeros(self.shape, np.float32))
            np.maximum(layer, mask.astype(np.float32) * (intensity / 255.0), out=layer)
        img = cv2.resize(base, (self.shape[1], self.shape[0]), interpolation=cv2.INTER_LINEAR).astype(np.float32)
        for color, ink in layers.items():
            c = np.array(color, np.float32)
            if kind == "led":
                if glow > 0:
                    img += (cv2.GaussianBlur(ink, (0, 0), 4 * self.ss) * glow * 0.6)[..., None] * c
                img += ink[..., None] * c
            else:
                img = img * (1 - ink[..., None]) + c * ink[..., None]
        img = np.clip(img, 0, 255)
        return cv2.resize(img, (base.shape[1], base.shape[0]), interpolation=cv2.INTER_AREA)


def draw_field(cv: Canvas, x: float, y: float, spec: FieldSpec, st: Style, color: Color) -> list[np.ndarray]:
    """Draw one digit field; returns the corners of each cell (for labels and occlusion checks)."""
    h = st.digit_h * spec.height_scale
    lit, dps = spec.resolved(st.glyph_style)
    corners = []
    ghost_color = color
    for (cx, cy, cw, ch), segs, dp in zip(_cell_boxes(x, y, len(spec.cells), h, st), lit, dps):
        polys = segment_polygons(cx, cy, cw, ch, st.thickness * h, st.slant, st.gap * h)
        for s, pts in polys.items():
            if st.ghost > 0:
                cv.poly(pts, ghost_color, st.ghost)
            cv.poly(pts, color, segs.get(s, 0.0))
        r = st.thickness * h * 0.55
        dp_c = (cx + cw + cw * st.spacing * 0.5, cy + ch - r)
        if st.ghost > 0:
            cv.circle(dp_c, r, ghost_color, st.ghost)
        cv.circle(dp_c, r, color, dp)
        sl = ch * st.slant
        corners.append(np.array([(cx + sl, cy), (cx + cw + sl, cy), (cx + cw, cy + ch), (cx, cy + ch)], np.float32))
    return corners


def _quad(x: float, y: float, w: float, h: float) -> np.ndarray:
    return np.array([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], np.float32)


def _casing(rng: np.random.Generator, gw: float, gh: float) -> tuple[np.ndarray, float, float]:
    """A bench background with the instrument's casing; returns the image and the glass origin."""
    m = rng.uniform(24, 60)
    bezel = rng.uniform(10, 26)
    W, H = int(gw + 2 * (m + bezel)), int(gh + 2 * (m + bezel))
    bench = np.array([rng.uniform(60, 140), rng.uniform(70, 150), rng.uniform(80, 160)], np.float32)
    img = np.ones((H, W, 3), np.float32) * bench
    img += rng.normal(0, 6, (H, W, 1)).astype(np.float32)
    case = np.array([rng.uniform(25, 90)] * 3, np.float32) + rng.uniform(-8, 8, 3).astype(np.float32)
    cv2.rectangle(img, (int(m), int(m)), (int(W - m), int(H - m)), case.tolist(), -1)
    return img, m + bezel, m + bezel


# --- multimeter --------------------------------------------------------------------------------

METER_UNITS = {"V": "V", "mV": "mV", "A": "A", "mA": "mA", "ohm": "", "kohm": "k", "Mohm": "M", "diode_V": "V"}


def _omega(cv: Canvas, x: float, y: float, s: float, color: Color, intensity: float) -> None:
    """Ω: an open ring with two feet."""
    t = np.linspace(np.pi * 0.72, np.pi * 2.28, 24)
    ring = np.stack([x + s * 0.5 + np.cos(t) * s * 0.42, y - s * 0.55 - np.sin(t) * s * 0.42], 1).astype(np.float32)
    feet = [np.array([(x + s * 0.05, y), (x + s * 0.3, y)], np.float32), np.array([(x + s * 0.7, y), (x + s * 0.95, y)], np.float32)]
    cv.strokes([ring, *feet], color, intensity, max(1.2, s * 0.09))


def _diode(cv: Canvas, x: float, y: float, s: float, color: Color, intensity: float) -> None:
    tri = np.array([(x + s * 0.2, y - s * 0.5), (x + s * 0.2, y + s * 0.0), (x + s * 0.62, y - s * 0.25)], np.float32)
    cv.poly(tri, color, intensity)
    cv.strokes([np.array([(x + s * 0.64, y - s * 0.52), (x + s * 0.64, y + s * 0.02)], np.float32),
                np.array([(x, y - s * 0.25), (x + s * 0.9, y - s * 0.25)], np.float32)], color, intensity, max(1.2, s * 0.08))


def _battery(cv: Canvas, x: float, y: float, s: float, color: Color, intensity: float) -> None:
    body = np.array([(x, y - s * 0.4), (x + s * 0.8, y - s * 0.4), (x + s * 0.8, y), (x, y), (x, y - s * 0.4)], np.float32)
    nub = np.array([(x + s * 0.8, y - s * 0.28), (x + s * 0.9, y - s * 0.28), (x + s * 0.9, y - s * 0.12), (x + s * 0.8, y - s * 0.12)], np.float32)
    cv.strokes([body], color, intensity, max(1.2, s * 0.08))
    cv.poly(nub, color, intensity)


@dataclass
class MeterReading:
    mode: str  # diode | dcv | ohm | dca
    text: str  # as displayed: "0.352", "OL", "12.34"
    unit: str  # V | mV | A | mA | ohm | kohm | Mohm | diode_V
    ol: bool = False
    battery_low: bool = False


def render_meter(rng: np.random.Generator, st: Style, reading: MeterReading, spec: FieldSpec, ink: float = 1.0) -> Rendered:
    """A 4-digit LCD with mode icons (AUTO, DC, diode, Ω), the unit, and a low-battery icon."""
    n = len(spec.cells)
    gw = field_width(n, st.digit_h, st) + st.digit_h * 1.6
    gh = st.digit_h * 2.0
    base, gx, gy = _casing(rng, gw, gh)
    cv = Canvas(base.shape[0], base.shape[1])
    fg = st.fg
    ghost = st.ghost
    dx, dy = gx + st.digit_h * 0.25, gy + st.digit_h * 0.62
    cells = draw_field(cv, dx, dy, spec, st, fg)
    s = st.digit_h * 0.3
    # icons along the top: lit for the current mode, ghosted otherwise
    icons = [("AUTO", reading.mode in ("dcv", "ohm", "dca")), ("DC", reading.mode in ("dcv", "dca"))]
    ix = gx + st.digit_h * 0.25
    for label, on in icons:
        cv.text(label, (ix, gy + s * 1.2), s / 30 * 1.0, fg, ink if on else ghost)
        ix += s * (len(label) * 0.75 + 0.8)
    _diode(cv, ix + s * 0.3, gy + s * 1.15, s * 1.1, fg, ink if reading.mode == "diode" else ghost)
    _battery(cv, gx + gw - s * 1.6, gy + s * 1.2, s * 1.1, fg, ink if reading.battery_low else ghost)
    # unit, right of the digits
    ux, uy = dx + field_width(n, st.digit_h, st) + st.digit_h * 0.12, dy + st.digit_h * 0.98
    prefix = METER_UNITS.get(reading.unit, "")
    if reading.unit in ("ohm", "kohm", "Mohm") or (reading.ol and reading.mode == "ohm"):
        if prefix:
            cv.text(prefix, (ux, uy), s / 30 * 1.3, fg, ink)
        _omega(cv, ux + (s * 0.9 if prefix else 0), uy, s * 1.3, fg, ink)
    else:
        cv.text(prefix or "V", (ux, uy), s / 30 * 1.3, fg, ink)
    base_glass = base.copy()
    cv2.rectangle(base_glass, (int(gx), int(gy)), (int(gx + gw), int(gy + gh)), st.bg, -1)
    img = cv.composite(base_glass, "lcd", 0.0)
    h = st.digit_h
    fx0 = dx - h * 0.08
    return Rendered(img.astype(np.uint8), _quad(gx, gy, gw, gh),
                    {"main": _quad(fx0, dy - h * 0.08, field_width(n, h, st) + h * 0.12, h * 1.16)}, {"main": cells})


# --- bench supply ----------------------------------------------------------------------------

SUPPLY_FIELDS = ("set_voltage", "live_current", "current_limit")


def render_supply(rng: np.random.Generator, st: Style, specs: dict[str, FieldSpec], cc: bool = False,
                  colors: dict[str, Color] | None = None) -> Rendered:
    """Two big LED rows (V, A) and a smaller current-limit row, with CV/CC lamps."""
    if st.kind == "lcd":
        colors = {k: st.fg for k in SUPPLY_FIELDS}
    colors = colors or {"set_voltage": LED_COLORS["red"], "live_current": LED_COLORS["green"], "current_limit": LED_COLORS["amber"]}
    h = st.digit_h
    rows = [("set_voltage", 1.0, "V"), ("live_current", 1.0, "A"), ("current_limit", 0.6, "A")]
    widths = [field_width(len(specs[name].cells), h * sc, st) for name, sc, _ in rows]
    gw = max(widths) + h * 1.5
    gh = h * (1.0 + 1.0 + 0.6) + h * 1.3
    base, gx, gy = _casing(rng, gw, gh)
    cv = Canvas(base.shape[0], base.shape[1])
    y = gy + h * 0.3
    fields, cells = {}, {}
    for (name, sc, unit), w in zip(rows, widths):
        spec = specs[name]
        spec.height_scale = sc
        x = gx + h * 0.35 + (max(widths) - w)
        cells[name] = draw_field(cv, x, y, spec, st, colors[name])
        hh = h * sc
        cv.text(unit, (x + w + h * 0.12, y + hh * 0.95), hh / 100 * 1.3, colors[name], 0.9, thick=max(1.2, hh * 0.05))
        if name == "current_limit":
            cv.text("LIMIT", (gx + h * 0.1, y + hh * 0.7), hh / 100 * 0.75, colors[name], 0.8)
        fields[name] = _quad(x - hh * 0.08, y - hh * 0.08, w + hh * 0.12, hh * 1.16)
        y += hh + h * 0.3
    for i, (label, on) in enumerate([("CV", not cc), ("CC", cc)]):
        lx = gx + gw - h * 0.95
        ly = gy + h * (0.35 + 0.45 * i)
        lamp = st.fg if st.kind == "lcd" else LED_COLORS["green" if label == "CV" else "red"]
        cv.circle((lx, ly), h * 0.07, lamp, 1.0 if on else 0.08)
        cv.text(label, (lx + h * 0.12, ly + h * 0.07), h / 100 * 0.6, st.fg if st.kind == "lcd" else (160, 160, 160), 0.8)
    base_glass = base.copy()
    cv2.rectangle(base_glass, (int(gx), int(gy)), (int(gx + gw), int(gy + gh)), st.bg, -1)
    img = cv.composite(base_glass, st.kind, st.glow)
    return Rendered(img.astype(np.uint8), _quad(gx, gy, gw, gh), fields, cells)
