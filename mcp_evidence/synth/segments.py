"""Seven-segment glyphs: geometry and the layout of a reading into digit cells.

Segments are named a–g (a top, b top-right, c bottom-right, d bottom, e bottom-left, f top-left,
g middle). A cell is one character plus an optional decimal point after it.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from mcp_evidence.vision.glyphs import GLYPHS, PATTERNS, SEGMENTS  # noqa: F401  (re-exported)


@dataclass(frozen=True)
class Cell:
    ch: str  # one of GLYPHS
    dp: bool = False  # decimal point after this character


def layout(text: str, n: int, leading_zero: bool = True) -> list[Cell]:
    """Right-align a displayed string ("0.352", "4.00", "OL", "-1.25") into n digit cells.

    Raises ValueError if it doesn't fit (more digits than cells)."""
    cells: list[Cell] = []
    for ch in text:
        if ch == ".":
            if not cells:
                cells.append(Cell("0" if leading_zero else " ", True))
            else:
                last = cells[-1]
                cells[-1] = Cell(last.ch, True)
        else:
            if ch not in GLYPHS:
                raise ValueError(f"no glyph for {ch!r}")
            cells.append(Cell(ch))
    if len(cells) > n:
        raise ValueError(f"{text!r} needs {len(cells)} cells, display has {n}")
    return [Cell(" ")] * (n - len(cells)) + cells


def cells_text(cells: list[Cell]) -> str:
    """The string a set of cells shows, as the readers report it ("0.352", "OL", " 4.00" → "4.00")."""
    out = ""
    for c in cells:
        out += c.ch + ("." if c.dp else "")
    return out.strip()


def format_value(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}"


def segment_polygons(x0: float, y0: float, w: float, h: float, t: float, slant: float, gap: float) -> dict[str, np.ndarray]:
    """Hexagonal segment outlines for a digit cell whose box is (x0, y0, w, h). `t` is the stroke
    thickness, `slant` the italic shear (x shift per unit of height, top to the right), `gap` the
    space between neighbouring segments."""

    def hseg(yc: float, xl: float, xr: float) -> list[tuple[float, float]]:
        xl, xr = xl + gap, xr - gap
        return [(xl, yc), (xl + t / 2, yc - t / 2), (xr - t / 2, yc - t / 2), (xr, yc), (xr - t / 2, yc + t / 2), (xl + t / 2, yc + t / 2)]

    def vseg(xc: float, yt: float, yb: float) -> list[tuple[float, float]]:
        yt, yb = yt + gap, yb - gap
        return [(xc, yt), (xc + t / 2, yt + t / 2), (xc + t / 2, yb - t / 2), (xc, yb), (xc - t / 2, yb - t / 2), (xc - t / 2, yt + t / 2)]

    xl, xr = x0 + t / 2, x0 + w - t / 2
    yt, ym, yb = y0 + t / 2, y0 + h / 2, y0 + h - t / 2
    raw = {
        "a": hseg(yt, xl, xr),
        "b": vseg(xr, yt, ym),
        "c": vseg(xr, ym, yb),
        "d": hseg(yb, xl, xr),
        "e": vseg(xl, ym, yb),
        "f": vseg(xl, yt, ym),
        "g": hseg(ym, xl, xr),
    }
    base = y0 + h
    return {k: np.array([(x + (base - y) * slant, y) for x, y in pts], dtype=np.float32) for k, pts in raw.items()}


def lit_segments(ch: str, style: int = 0) -> str:
    styles = GLYPHS[ch]
    return styles[min(style, len(styles) - 1)]
