"""Reader B accuracy, confidence calibration and agreement-rule outcomes on the display sets.

No API calls: Reader A is the fixture reader (mcp_evidence/vision/reader_a.py).

Split discipline (eval/display_set/README.md): Reader B's parameters are tuned on the synthetic
"tune" split only. The held-out splits (distortions glare, low_contrast, noise, mid_update; digit
families bold, narrow) and the real photos in real_misc/ run on the frozen reader and are
reported apart. Every result file records the SHA-256 of reader_b.py it ran with.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from mcp_evidence.synth.dataset import load_labels
from mcp_evidence.vision import reader_b
from mcp_evidence.vision.calibration import Roi
from mcp_evidence.vision.glyphs import SEGMENTS
from mcp_evidence.vision.reader_b import Params, ReadB, read_field

ROOT = Path(__file__).resolve().parent
SYNTH = ROOT / "synthetic"
REAL_MISC = ROOT / "real_misc"
BINS = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0001]


def reader_sha() -> str:
    return hashlib.sha256(Path(reader_b.__file__).read_bytes()).hexdigest()[:16]


def text_key(text: str) -> str | None:
    """'04.00' → '4.00', 'OL' → 'OL'; None for 'mixed' / 'flicker' frames."""
    if text in ("mixed", "flicker"):
        return None
    if text == "OL":
        return "OL"
    dec = len(text.split(".")[1]) if "." in text else 0
    return f"{float(text):.{dec}f}"


def truth_key(t: dict) -> str:
    return "OL" if t["ol"] else f"{t['value']:.{t['decimals']}f}"


@dataclass
class FieldRead:
    sample: str
    field: str
    frame: int
    distortion: str
    split: str
    readable: bool
    truth: str | None  # key of what this frame shows (None: mid-change)
    read: ReadB


def crop_field(img: np.ndarray, corners, jitter: float = 0.0, rng: np.random.Generator | None = None) -> np.ndarray:
    c = np.array(corners, np.float32)
    if jitter and rng is not None:
        c = c + rng.uniform(-jitter, jitter, c.shape).astype(np.float32)
    return Roi("f", c).warp(img)


def synthetic_reads(splits: set[str], frames=(0, 1, 2), jitter: float = 0.0, params: Params = reader_b.DEFAULT,
                    root: Path = SYNTH, limit: int | None = None) -> list[FieldRead]:
    labels = [lab for lab in load_labels(root) if lab["split"] in splits][:limit]
    out: list[FieldRead] = []
    rng = np.random.default_rng(1)
    for lab in labels:
        imgs = {i: cv2.imread(str(root / lab["frames"][i])) for i in frames}
        for f, corners in lab["fields"].items():
            for i in frames:
                r = read_field(crop_field(imgs[i], corners, jitter, rng), 4, params=params)
                out.append(FieldRead(lab["id"], f, i, lab["distortion"], lab["split"], lab["readable"][f],
                                     text_key(lab["frame_truth"][f][i]), r))
    return out


def accuracy(reads: list[FieldRead], by: str = "distortion") -> dict[str, dict]:
    """Per group: on readable fields, correct / wrong / illegible; on unreadable ones, how many
    were (rightly) illegible and how many were read as a number anyway."""
    g: dict[str, dict] = defaultdict(lambda: {"n": 0, "correct": 0, "wrong": 0, "illegible": 0, "unreadable_n": 0,
                                               "unreadable_illegible": 0, "unreadable_read": 0, "ms": []})
    for r in reads:
        if r.truth is None:
            continue  # mid-change frames: only the agreement rule is judged on them
        d = g[getattr(r, by)]
        d["ms"].append(r.read.ms)
        if not r.readable:
            d["unreadable_n"] += 1
            d["unreadable_illegible" if not r.read.legible else "unreadable_read"] += 1
            continue
        d["n"] += 1
        if not r.read.legible:
            d["illegible"] += 1
        elif r.read.key == r.truth:
            d["correct"] += 1
        else:
            d["wrong"] += 1
    out = {}
    for k, d in sorted(g.items()):
        ms = d.pop("ms")
        n = max(d["n"], 1)
        out[k] = d | {"acc": round(d["correct"] / n, 3), "wrong_rate": round(d["wrong"] / n, 3),
                      "ms_p50": round(float(np.median(ms)), 1) if ms else None, "ms_p95": round(float(np.percentile(ms, 95)), 1) if ms else None}
    return out


def _bins(pairs: list[tuple[float, bool]]) -> dict:
    rows, ece, n_all = [], 0.0, len(pairs)
    for lo, hi in zip(BINS, BINS[1:]):
        sel = [ok for c, ok in pairs if lo <= c < hi]
        conf = [c for c, _ in pairs if lo <= c < hi]
        if sel:
            acc, mc = float(np.mean(sel)), float(np.mean(conf))
            ece += len(sel) / n_all * abs(acc - mc)
            rows.append({"bin": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": len(sel), "mean_conf": round(mc, 3), "accuracy": round(acc, 3)})
    return {"n": n_all, "ece": round(ece, 4), "bins": rows}


def segment_calibration(reads: list[FieldRead], labels: dict[str, dict], visible_only: bool = True) -> dict:
    """Each segment's decision confidence max(P, 1−P) against its true state, on frames whose
    segments are known (all frames: the labels give the lit segments per frame). Only reads
    whose grid matched the label's cell count are used; hidden segments optional."""
    pairs: list[tuple[float, bool]] = []
    for r in reads:
        if not r.read.cells:
            continue
        lab = labels[r.sample]
        truth_cells = lab["segments"][r.field][r.frame]
        hidden = lab.get("hidden", {}).get(r.field, [""] * len(truth_cells))
        for cell, tcell, hid in zip(r.read.cells, truth_cells, hidden):
            for s in SEGMENTS:
                if visible_only and s in hid:
                    continue
                p = cell.seg_p[s]
                pairs.append((max(p, 1 - p), (p >= 0.5) == (s in tcell)))
    return _bins(pairs)


def reading_calibration(reads: list[FieldRead]) -> dict:
    """The reading's confidence against whether it was right, over legible reads of readable,
    settled frames."""
    pairs = [(r.read.confidence, r.read.key == r.truth) for r in reads if r.truth is not None and r.readable and r.read.legible]
    return _bins(pairs)


# --- real_misc ---------------------------------------------------------------------------------


def real_misc_labels(root: Path = REAL_MISC) -> list[dict]:
    rows: list[dict] = []
    if (root / "labels.jsonl").exists():
        rows = [json.loads(x) for x in (root / "labels.jsonl").read_text().splitlines() if x.strip()]
    elif (root / "labels.csv").exists():
        with open(root / "labels.csv", newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        corners = r.get("corners") or ""
        if isinstance(corners, str):
            nums = [float(x) for x in corners.replace(",", " ").split()]
            corners = np.array(nums, np.float32).reshape(4, 2).tolist() if len(nums) == 8 else None
        text = str(r["text"]).strip()
        digits = r.get("digits")
        n_digits = int(digits) if str(digits or "").strip() else sum(ch.isdigit() or ch == "-" for ch in text) or 2
        out.append({"file": r["file"], "text": text, "key": text_key(text), "corners": corners, "digits": n_digits,
                    "polarity": (r.get("polarity") or "").strip() or None,
                    "readable": str(r.get("readable", "yes")).strip().lower() not in ("no", "false", "0"), "notes": r.get("notes", "")})
    return out


def real_misc_reads(root: Path = REAL_MISC, params: Params = reader_b.DEFAULT) -> list[tuple[dict, ReadB]]:
    out = []
    for lab in real_misc_labels(root):
        img = cv2.imread(str(root / lab["file"]))
        if img is None:
            continue
        crop = Roi("f", np.array(lab["corners"], np.float32)).warp(img) if lab["corners"] else img
        out.append((lab, read_field(crop, lab["digits"], lab["polarity"], params)))
    return out


def real_misc_summary(results: list[tuple[dict, ReadB]]) -> dict:
    readable = [(lab, r) for lab, r in results if lab["readable"]]
    n = len(readable)
    correct = sum(1 for lab, r in readable if r.legible and r.key == lab["key"])
    wrong = sum(1 for lab, r in readable if r.legible and r.key != lab["key"])
    pairs = [(r.confidence, r.key == lab["key"]) for lab, r in readable if r.legible]
    return {"n": len(results), "readable": n, "correct": correct, "wrong": wrong, "illegible": n - correct - wrong,
            "acc": round(correct / n, 3) if n else None, "reading_calibration": _bins(pairs),
            "items": [{"file": lab["file"], "truth": lab["key"], "read": r.key, "legible": r.legible, "conf": round(r.confidence, 3),
                       "reason": r.reason} for lab, r in results]}


# --- agreement rule with a fixture Reader A -------------------------------------------------------

from mcp_evidence.vision.agreement import DisplaySpec, read_display  # noqa: E402
from mcp_evidence.vision.glyphs import PATTERNS  # noqa: E402
from mcp_evidence.vision.reader_a import FixtureAnswer, FixtureReaderA, HedgedReaderA, ReadA, fields_of  # noqa: E402

# Assumed Reader A behaviour (no API calls in this phase). Frontier models read digital displays
# about 80% right and fail mostly on the decimal point (MeasureBench, BUILD_GUIDE); this fixture
# reads what is visible in each frame (so a hand, glare or a flickering frame fools it as it
# fools Reader B: correlated errors) and adds these independent failures.
FIXTURE_RATES = {"wrong_dp": 0.08, "wrong_digit": 0.05, "illegible": 0.05, "slow": 0.04, "timeout": 0.02, "error": 0.01}
CONFUSABLE = {"0": "8", "1": "7", "2": "7", "3": "8", "4": "9", "5": "6", "6": "5", "7": "1", "8": "0", "9": "8"}


def visible_text(cells: list[str], hidden: list[str]) -> str | None:
    """What a reader sees: each cell's lit segments minus the hidden ones, as a glyph; None if a
    cell shows no glyph."""
    out = ""
    for seg, hid in zip(cells, hidden):
        lit = frozenset(seg.replace(".", "")) - frozenset(hid)
        ch = PATTERNS.get(lit)
        if ch is None:
            return None
        out += ch + ("." if seg.endswith(".") and "." not in hid else "")
    t = out.strip()
    if t.replace(" ", "") in ("0L",):
        return "OL"
    try:
        float(t)
    except ValueError:
        return None
    return t if " " not in t else None


def _shift_dp(text: str, rng: np.random.Generator) -> str | None:
    if "." not in text:
        return None
    digits = text.replace(".", "").replace("-", "")
    neg = text.startswith("-")
    i = text.replace("-", "").index(".")
    j = i + (1 if rng.random() < 0.5 else -1)
    if j <= 0 or j >= len(digits):
        j = i - 1 if j >= len(digits) else i + 1
    if j <= 0 or j >= len(digits):
        return None
    return ("-" if neg else "") + digits[:j] + "." + digits[j:]


def fixture_answer(lab: dict, frame: int, rng: np.random.Generator, rates: dict = FIXTURE_RATES) -> FixtureAnswer:
    display = "meter" if lab["display"] == "meter" else "supply"
    reading: dict[str, ReadA] = {}
    lat = [float(rng.uniform(0.9, 1.8)), float(rng.uniform(0.9, 1.5))]
    r = rng.random()
    if r < rates["timeout"]:
        lat = [5.0, 5.0]
    elif r < rates["timeout"] + rates["slow"]:
        lat = [float(rng.uniform(2.6, 3.9)), float(rng.uniform(0.9, 1.4))]
    raises = rng.random() < rates["error"]
    for f in fields_of(display):
        segs = lab["segments"][f][frame]
        hid = lab.get("hidden", {}).get(f) or [""] * len(segs)
        text = visible_text(segs, hid)
        legible = text is not None
        conf = float(rng.uniform(0.88, 0.98))
        u = rng.random()
        if legible and u < rates["wrong_dp"]:
            text = _shift_dp(text, rng) or text  # overconfident, as models are on decimals
        elif legible and u < rates["wrong_dp"] + rates["wrong_digit"]:
            k = [i for i, c in enumerate(text) if c.isdigit()]
            if k:
                i = int(rng.choice(k))
                text = text[:i] + CONFUSABLE[text[i]] + text[i + 1 :]
        elif u < rates["wrong_dp"] + rates["wrong_digit"] + rates["illegible"]:
            legible, conf = False, float(rng.uniform(0.1, 0.4))
        if not legible:
            reading[f] = ReadA(False, None, None, "unknown", None, 0, conf)
            continue
        assert text is not None
        ol = text == "OL"
        dec = 0 if ol else (len(text.split(".")[1]) if "." in text else 0)
        unit = "OL" if ol else (lab.get("unit", "V") if display == "meter" else ("V" if f == "set_voltage" else "A"))
        mode = lab.get("mode") if display == "meter" else None
        reading[f] = ReadA(True, text, None if ol else float(text), unit, mode, dec, conf)
    return FixtureAnswer(reading, tuple(lat), raises)


@dataclass
class Outcome:
    sample: str
    field: str
    distortion: str
    split: str
    readable: bool
    truth: str
    status: str  # agreed | unclear (spoken) | ...
    key: str | None
    display_status: str
    a_calls: int
    hedged: bool

    @property
    def result(self) -> str:
        # per field: the gate checks each supply field's confidence; a wrong mode or an implausible
        # display accepts nothing
        if self.status == "agreed" and self.display_status not in ("implausible", "wrong_mode"):
            return "accepted_correct" if self.key == self.truth else "accepted_wrong"
        return "to_spoken"


async def _agreement_one(lab: dict, root: Path, rng: np.random.Generator, jitter: float, time_scale: float) -> list[Outcome]:
    display = "meter" if lab["display"] == "meter" else "supply"
    imgs = [cv2.imread(str(root / p)) for p in lab["frames"]]
    jit = {k: (np.array(v, np.float32) + (rng.uniform(-jitter, jitter, (4, 2)).astype(np.float32) if jitter else 0))
           for k, v in [("display", lab["glass"])] + list(lab["fields"].items())}
    rois = {k: Roi(k, v, kind="display" if k == "display" else "field") for k, v in jit.items()}

    def crops(img: np.ndarray) -> dict[str, np.ndarray]:
        return {k: r.warp(img) for k, r in rois.items()}

    fixture = FixtureReaderA(time_scale=time_scale)
    for i in (1, 2):
        fixture.register(display, crops(imgs[i])["display"], fixture_answer(lab, i, rng))
    reader = HedgedReaderA(fixture, time_scale=time_scale)

    async def nxt(i: int) -> np.ndarray | None:
        return imgs[i] if i < len(imgs) else None

    res = await read_display(DisplaySpec(display, crops), nxt, reader, expected_mode=lab.get("mode"))
    out = []
    for f, fr in res.fields.items():
        out.append(Outcome(lab["id"], f, lab["distortion"], lab["split"], lab["readable"][f], truth_key(lab["truth"][f]),
                           fr.status, fr.key, res.status, res.a_calls, res.hedged))
    return out


def agreement_outcomes(splits: set[str], root: Path = SYNTH, jitter: float = 0.0, time_scale: float = 0.001,
                       limit: int | None = None, seed: int = 11) -> list[Outcome]:
    labels = [lab for lab in load_labels(root) if lab["split"] in splits][:limit]
    out: list[Outcome] = []

    async def go() -> None:
        for lab in labels:
            rng = np.random.default_rng([seed, int(lab["id"][1:])])
            out.extend(await _agreement_one(lab, root, rng, jitter, time_scale))

    asyncio.run(go())
    return out


def agreement_table(outcomes: list[Outcome], by: str = "distortion") -> dict[str, dict]:
    g: dict[str, dict] = defaultdict(lambda: {"fields": 0, "accepted_correct": 0, "accepted_wrong": 0, "to_spoken": 0,
                                               "unreadable": 0, "unreadable_accepted_wrong": 0, "a_calls": 0, "hedged": 0})
    seen: set[tuple[str, str]] = set()
    for o in outcomes:
        d = g[getattr(o, by)]
        d["fields"] += 1
        d[o.result] += 1
        if not o.readable:
            d["unreadable"] += 1
            d["unreadable_accepted_wrong"] += o.result == "accepted_wrong"
        if (o.sample, o.distortion) not in seen:
            seen.add((o.sample, o.distortion))
            d["a_calls"] += o.a_calls
            d["hedged"] += o.hedged
    return dict(sorted(g.items()))


import asyncio  # noqa: E402
