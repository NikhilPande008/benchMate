"""Labelled synthetic display set: meter and bench-supply frames under camera distortions.

    uv run python -m mcp_evidence.synth --out eval/display_set/synthetic --samples 700 --seed 7

Every sample is three frames, 150 ms apart (the two-frame agreement rule plus the third frame it
asks for on disagreement), with ground truth for every field. The set is reproducible from the
seed: sample i depends only on (seed, i). Results on it are **synthetic** and say nothing about
accuracy on the real supply and meter (BUILD_GUIDE, "Measure on your own equipment").
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import distort
from .displays import LED_COLORS, SUPPLY_FIELDS, FieldSpec, MeterReading, Rendered, Style, random_style, render_meter, render_supply
from .segments import Cell, layout, lit_segments

DISTORTIONS = ("clean", "perspective", "blur", "glare", "low_contrast", "flicker", "mid_update", "hand", "dim_lcd", "noise")
FRAME_MS = (0, 150, 300)
DIM_READABLE = 0.25  # lit minus ghost ink below this: a person can't tell lit from unlit segments
GENERATOR_VERSION = 1


@dataclass
class FieldTruth:
    text: str  # as displayed, without blank cells: "0.352", "04.00", "OL"
    value: float | None  # None for OL
    decimals: int
    ol: bool = False


@dataclass
class Sample:
    id: str
    display: str  # meter | supply
    distortion: str
    severity: float
    frames: list[np.ndarray]
    truth: dict[str, FieldTruth]
    extra: dict  # meter: mode, unit, battery_low; supply: cc
    glass: np.ndarray
    fields: dict[str, np.ndarray]
    cells: dict[str, list[np.ndarray]]
    readable: dict[str, bool]
    frame_truth: dict[str, list[str]]
    style: Style
    jpeg_quality: int = 90
    notes: list[str] = field(default_factory=list)

    def label(self, frame_files: list[str]) -> dict:
        r2 = lambda a: np.round(np.asarray(a, dtype=float), 2).tolist()  # noqa: E731
        return {
            "id": self.id,
            "synthetic": True,
            "display": self.display,
            "distortion": self.distortion,
            "severity": round(self.severity, 3),
            "frames": frame_files,
            "frame_ms": list(FRAME_MS),
            "size": [int(self.frames[0].shape[1]), int(self.frames[0].shape[0])],
            "glass": r2(self.glass),
            "fields": {k: r2(v) for k, v in self.fields.items()},
            "cells": {k: [r2(c) for c in v] for k, v in self.cells.items()},
            "truth": {k: asdict(v) for k, v in self.truth.items()},
            **self.extra,
            "readable": self.readable,
            "frame_truth": self.frame_truth,
            "style": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(self.style).items() if k in
                      ("kind", "digit_h", "slant", "thickness", "ghost", "glyph_style", "leading_zero")},
            "jpeg_quality": self.jpeg_quality,
            "notes": self.notes,
        }


# --- values ----------------------------------------------------------------------------------


def _pick(rng: np.random.Generator, options: list, p: list[float]):
    return options[int(rng.choice(len(options), p=p))]


def random_meter(rng: np.random.Generator) -> tuple[MeterReading, FieldTruth]:
    """A reading a 6000-count meter could show in the modes a board repair uses."""
    mode = _pick(rng, ["diode", "dcv", "ohm", "dca"], [0.45, 0.3, 0.2, 0.05])
    if mode == "diode":
        if rng.random() < 0.15:
            return MeterReading("diode", "OL", "diode_V", ol=True), FieldTruth("OL", None, 0, True)
        lo, hi = _pick(rng, [(0, 0.06), (0.06, 0.3), (0.3, 0.8), (0.8, 1.999)], [0.3, 0.25, 0.35, 0.1])
        v = float(rng.uniform(lo, hi))
        return MeterReading("diode", f"{v:.3f}", "diode_V"), FieldTruth(f"{v:.3f}", round(v, 3), 3)
    if mode == "ohm" and rng.random() < 0.2:
        return MeterReading("ohm", "OL", "Mohm", ol=True), FieldTruth("OL", None, 0, True)
    ranges = {
        "dcv": [("mV", 0, 599.9, 1), ("V", 0, 5.999, 3), ("V", 6, 59.99, 2)],
        "ohm": [("ohm", 0, 599.9, 1), ("kohm", 0, 5.999, 3), ("kohm", 6, 59.99, 2), ("Mohm", 0, 5.999, 3)],
        "dca": [("mA", 0, 59.99, 2), ("A", 0, 5.999, 3)],
    }[mode]
    unit, lo, hi, dec = ranges[int(rng.integers(len(ranges)))]
    v = float(rng.uniform(lo, hi))
    if mode == "dcv" and rng.random() < 0.08:  # probes reversed
        v = -float(rng.uniform(0.01, 0.99 if dec == 3 else 9.9))
        dec = 2 if abs(v) < 10 else 1
    text = f"{v:.{dec}f}"
    return MeterReading(mode, text, unit), FieldTruth(text, round(v, dec), dec)


def random_supply(rng: np.random.Generator, leading_zero: bool) -> tuple[dict[str, FieldTruth], bool]:
    vdec = _pick(rng, [2, 1, 3], [0.8, 0.1, 0.1])
    if rng.random() < 0.7:
        v = 4.0 if rng.random() < 0.4 else float(rng.uniform(3.8, 4.2))
    else:
        v = float(_pick(rng, [rng.uniform(0, 3.8), rng.uniform(4.2, 15.0), 5.0, 3.7, 12.0], [0.3, 0.4, 0.1, 0.1, 0.1]))
    if v >= 10:
        vdec = min(vdec, 2)  # four digits
    ldec = _pick(rng, [2, 3], [0.6, 0.4])
    lim = float(rng.uniform(0.5, 1.5) if rng.random() < 0.6 else rng.uniform(0.1, 3.0))
    lim = round(lim, ldec)
    adec = _pick(rng, [3, 2], [0.75, 0.25])
    r = rng.random()
    if r < 0.45:
        a = 0.0
    elif r < 0.75:
        a = float(rng.uniform(0.001, 0.3))
    elif r < 0.95:
        a = float(rng.uniform(0.0, lim * 0.95))
    else:
        a = lim  # at the limit: CC lamp on
    a = round(min(a, lim), adec)
    vtext = f"{v:0{vdec + 3}.{vdec}f}" if (leading_zero and v < 10 and vdec <= 2) else f"{v:.{vdec}f}"
    truth = {
        "set_voltage": FieldTruth(vtext, round(v, vdec), vdec),
        "live_current": FieldTruth(f"{a:.{adec}f}", a, adec),
        "current_limit": FieldTruth(f"{lim:.{ldec}f}", lim, ldec),
    }
    return truth, a >= lim


def _spec(text: str, n: int, leading_zero: bool) -> FieldSpec:
    return FieldSpec(layout(text, n, leading_zero))


def _meter_cells(text: str, ol: bool) -> list[Cell]:
    return layout("OL " if ol else text, 4)


def _changed_value(rng: np.random.Generator, t: FieldTruth) -> FieldTruth:
    """Another value of the same shape (the display updating to a new reading)."""
    if t.ol or t.value is None:
        v = float(rng.uniform(0.3, 0.8))
        return FieldTruth(f"{v:.3f}", round(v, 3), 3)
    step = 10 ** -t.decimals
    for _ in range(20):
        v = round(t.value + rng.choice([-1, 1]) * step * int(rng.integers(3, 400)), t.decimals) + 0.0  # no -0.0
        text = f"{v:.{t.decimals}f}"
        if v >= 0 and len(text.replace(".", "").replace("-", "")) <= 4 and text != t.text:
            return FieldTruth(text, v, t.decimals)
    return FieldTruth(t.text, t.value, t.decimals)


def _mix(old: list[Cell], new: list[Cell], rng: np.random.Generator, st: Style) -> FieldSpec:
    """Digits caught mid-update: LCDs fade (old segments half-dark under the new ones); LEDs
    switch digit by digit (some digits already new, some still old)."""
    lit, dps = [], []
    lcd = st.kind == "lcd"
    fade = float(rng.uniform(0.35, 0.7))
    for o, n in zip(old, new):
        so, sn = set(lit_segments(o.ch, st.glyph_style)), set(lit_segments(n.ch, st.glyph_style))
        if lcd:
            d = {s: 1.0 for s in sn} | {s: fade for s in so - sn}
            dp = 1.0 if n.dp else (fade if o.dp else 0.0)
        else:
            src = n if rng.random() < 0.5 else o
            d = {s: 1.0 for s in lit_segments(src.ch, st.glyph_style)}
            dp = 1.0 if src.dp else 0.0
        lit.append(d)
        dps.append(dp)
    return FieldSpec(new, lit, dps)


def _drop(spec: FieldSpec, rng: np.random.Generator, st: Style, severity: float) -> FieldSpec:
    """Flicker: segments missing in one frame. A multiplexed LED caught between scans loses whole
    digits; a refreshing LCD loses random segments."""
    lit, dps = spec.resolved(st.glyph_style)
    lit = [dict(d) for d in lit]
    dps = list(dps)
    if st.kind == "led" and rng.random() < 0.6:
        k = 1 + int(rng.random() < severity)
        for i in rng.choice(len(lit), size=min(k, len(lit)), replace=False):
            lit[int(i)], dps[int(i)] = {}, 0.0
    else:
        p = 0.15 + 0.35 * severity
        for d in lit:
            for s in list(d):
                if rng.random() < p:
                    d.pop(s)
        dps = [0.0 if rng.random() < p else x for x in dps]
    if not any(lit) or lit == spec.resolved(st.glyph_style)[0]:
        first = next((i for i, d in enumerate(lit) if d), 0)
        lit[first] = {}
    return FieldSpec(spec.cells, lit, dps)


# --- samples ---------------------------------------------------------------------------------


def _render(display: str, rng: np.random.Generator, st: Style, specs: dict[str, FieldSpec], extra: dict, ink: float) -> Rendered:
    if display == "meter":
        reading = MeterReading(extra["mode"], "", extra["unit"], ol=extra["ol"], battery_low=extra["battery_low"])
        spec = specs["main"]
        if ink < 1.0:
            lit, dps = spec.resolved(st.glyph_style)
            spec = FieldSpec(spec.cells, [{s: v * ink for s, v in d.items()} for d in lit], [x * ink for x in dps])
        return render_meter(rng, st, reading, spec, ink)
    return render_supply(rng, st, specs, cc=extra["cc"], colors=extra.get("colors"))


def _cell_fraction(mask: np.ndarray, quad: np.ndarray) -> float:
    m = np.zeros(mask.shape, np.uint8)
    cv2.fillPoly(m, [np.round(quad).astype(np.int32)], 1)
    area = m.sum()
    return float((mask & (m > 0)).sum() / area) if area else 0.0


def make_sample(seed: int, index: int, display: str | None = None, distortion: str | None = None) -> Sample:
    rng = np.random.default_rng([seed, index])
    distortion = distortion or DISTORTIONS[index % len(DISTORTIONS)]
    if display is None:
        display = "meter" if (distortion == "dim_lcd" or (index // len(DISTORTIONS)) % 2 == 0) else "supply"
    severity = float(rng.uniform(0.3, 1.0)) if distortion != "clean" else 0.0
    kind = "lcd" if display == "meter" or rng.random() < 0.2 else "led"
    st = random_style(rng, kind)
    notes: list[str] = []
    if display == "meter":
        reading, t = random_meter(rng)
        truth = {"main": t}
        extra = {"mode": reading.mode, "unit": reading.unit, "ol": reading.ol, "battery_low": distortion == "dim_lcd"}
        specs = {"main": FieldSpec(_meter_cells(t.text, t.ol))}
        out_extra = {"mode": reading.mode, "unit": reading.unit, "battery_low": extra["battery_low"]}
    else:
        truth, cc = random_supply(rng, st.leading_zero)
        specs = {k: _spec(truth[k].text, 4, st.leading_zero) for k in SUPPLY_FIELDS}
        colors = None
        if kind == "led":
            pal = [LED_COLORS[c] for c in rng.permutation(["red", "green", "amber", "blue"])]
            colors = dict(zip(SUPPLY_FIELDS, pal)) if rng.random() < 0.3 else None
        extra = {"cc": cc, "colors": colors}
        out_extra = {"cc": cc}
    frame_truth = {k: [v.text] * 3 for k, v in truth.items()}

    # per-frame specs: flicker and mid-update change what is lit
    frame_specs = [dict(specs) for _ in FRAME_MS]
    if distortion == "flicker":
        idx = int(rng.choice(3, p=[0.3, 0.5, 0.2]))  # which frame caught the display between scans
        frame_specs[idx] = {f: _drop(sp, rng, st, severity) for f, sp in specs.items()}
        for f in specs:
            frame_truth[f][idx] = "flicker"
    elif distortion == "mid_update":
        target = "main" if display == "meter" else _pick(rng, ["live_current", "set_voltage"], [0.8, 0.2])
        old = truth[target]
        new = _changed_value(rng, old)
        n = 4
        old_cells = _meter_cells(old.text, old.ol) if display == "meter" else layout(old.text, n, st.leading_zero)
        new_cells = layout(new.text, n, st.leading_zero)
        mixed = _mix(old_cells, new_cells, rng, st)
        variant = int(rng.integers(3))
        seq = [[mixed, new_cells, new_cells], [old_cells, mixed, new_cells], [old_cells, new_cells, new_cells]][variant]
        names = [["mixed", new.text, new.text], [old.text, "mixed", new.text], [old.text, new.text, new.text]][variant]
        for i, c in enumerate(seq):
            frame_specs[i] = dict(specs) | {target: c if isinstance(c, FieldSpec) else FieldSpec(c)}
        truth[target] = new  # the settled value
        frame_truth[target] = names
        notes.append(f"{target} changes {old.text} → {new.text}")
        if display == "meter" and old.ol:
            extra["ol"] = False

    ink = float(1.0 - 0.7 * severity) if distortion == "dim_lcd" else 1.0

    # one fixed scene (casing, light, hand, glare) for all three frames: same rng state per frame
    scene_seed = int(rng.integers(1 << 31))
    frames, glass, fields, cells = [], None, {}, {}
    occl = None
    M = None
    pq = int(rng.integers(35, 70)) if distortion == "noise" else int(rng.integers(80, 95))
    for i, fs in enumerate(frame_specs):
        srng = np.random.default_rng(scene_seed)
        r = _render(display, srng, st, {k: FieldSpec(v.cells, v.lit, v.dp) for k, v in fs.items()}, extra, ink)
        img = r.image
        pts = {"glass": r.glass, **{f"f:{k}": v for k, v in r.fields.items()},
               **{f"c:{k}:{j}": c for k, cs in r.cells.items() for j, c in enumerate(cs)}}
        drng = np.random.default_rng(scene_seed + 1)  # same distortion parameters in every frame
        if distortion == "perspective":
            img, pts, M = distort.perspective(img, pts, drng, severity)
        elif distortion == "blur":
            img = distort.blur(img, drng, severity)
        elif distortion == "glare":
            img = distort.glare(img, pts["glass"], drng, severity)
        elif distortion == "low_contrast":
            img = distort.low_contrast(img, drng, severity)
        elif distortion == "hand":
            img, occl = distort.hand(img, pts["glass"], drng, severity)
        elif distortion == "noise":
            img = distort.noise(img, np.random.default_rng([seed, index, i]), severity)
        img = distort.realism(img, np.random.default_rng(scene_seed + 2), np.random.default_rng([seed, index, 100 + i]))
        frames.append(img)
        if i == 0:
            glass = pts["glass"]
            fields = {k[2:]: v for k, v in pts.items() if k.startswith("f:")}
            cells = {}
            for k, v in pts.items():
                if k.startswith("c:"):
                    _, f, j = k.split(":")
                    cells.setdefault(f, []).append(v)

    readable = {k: True for k in truth}
    for f, cs in cells.items():
        lit_cells = [c for c, cell in zip(cs, specs[f].cells) if cell.ch != " "] or cs
        if distortion == "hand" and occl is not None:
            if any(_cell_fraction(occl, c) > 0.15 for c in lit_cells):
                readable[f] = False
        if distortion == "glare":
            sat = frames[0].max(axis=2) >= 250
            if any(_cell_fraction(sat, c) > 0.35 for c in lit_cells):
                readable[f] = False
        if distortion == "blur" and severity > 0.9:
            readable[f] = False
        if distortion == "dim_lcd" and ink - st.ghost < DIM_READABLE:
            readable[f] = False  # lit segments barely darker than unlit ones: an 8 and a 4 look alike

    return Sample(f"s{index:05d}", display, distortion, severity, frames, truth, out_extra, glass, fields, cells,
                  readable, frame_truth, st, pq, notes)


def write_dataset(out: Path, samples: int, seed: int) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with open(out / "labels.jsonl", "w", encoding="utf-8") as fh:
        for i in range(samples):
            s = make_sample(seed, i)
            d = out / s.distortion
            d.mkdir(exist_ok=True)
            files = []
            for tag, img in zip("abc", s.frames):
                name = f"{s.distortion}/{s.id}_{tag}.jpg"
                (out / name).write_bytes(distort.jpeg(img, s.jpeg_quality))
                files.append(name)
            fh.write(json.dumps(s.label(files), ensure_ascii=False) + "\n")
            key = f"{s.display}/{s.distortion}"
            counts[key] = counts.get(key, 0) + 1
    meta = {"synthetic": True, "generator_version": GENERATOR_VERSION, "seed": seed, "samples": samples,
            "images": samples * len(FRAME_MS), "frame_ms": list(FRAME_MS), "counts": dict(sorted(counts.items())),
            "note": "Synthetic stand-ins for the real supply and meter displays. Not a measure of real-world accuracy."}
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def load_labels(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / "labels.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
