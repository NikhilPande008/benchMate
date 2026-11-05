"""Reader B: the local seven-segment reader (pure Python on OpenCV), with a confidence for every
segment (CLAUDE.md rule 3: the second, independent reader).

Input: a flat crop of one digit row (from calibration) and its number of digit positions.

1. **Ink.** Segments become bright: inverted luminance for LCDs (dark on light), the brightest
   channel for LEDs. Saturated pixels (glare) are marked unseen.
2. **Background.** A grey opening wider than any stroke estimates the background; subtracting it
   removes gradients and LED halos. Where the background itself departs from a fitted plane (a
   hand, a glare spot) segments are marked partly unseen.
3. **Deskew.** The italic slant is the shear that makes vertical strokes sharpest.
4. **Grid.** Digit height from the row profile, stroke width from run lengths; cell width, pitch
   and position are searched so every cell looks most like a valid glyph and little ink falls
   outside the cells.
5. **Segments.** Each segment's mean ink is scored against the field's own lit and unlit levels
   (two clusters), giving P(lit). Unseen segments are pulled towards 0.5.
6. **Glyphs.** Each cell's posterior over every known glyph (digits in both styles, minus, OL,
   status letters) and "not a glyph" (holes lit: a hand or a blob). Decimal points the same way;
   at most one.
7. **Reading.** Text, value, decimals, OL; the confidence is the product of the cell and decimal
   posteriors. Anything that is not a clean number is illegible, with the reason.

Parameters in `Params` are the only things tuned, and only on the synthetic "tune" split
(eval/display_set/README.md).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from .glyphs import PATTERNS, SEGMENTS

H = 96  # working height


@dataclass(frozen=True)
class Params:
    sat_level: int = 245  # min channel at or above this: saturated (glare)
    open_k: float = 0.30  # background opening kernel, as a fraction of the crop height
    min_contrast: float = 22.0  # lit-minus-background ink below this: nothing to read
    anomaly: float = 0.18  # background departing from its plane by this fraction of the contrast: unseen
    chroma_dist: float = 12.0  # LCD: colour this far from the glass (Lab a/b) is something in front of it
    max_unseen: float = 0.06  # more of the digit row unseen than this (a hand, glare): occluded, not read
    row_frac: float = 0.30  # rows with this share of the peak row ink belong to the digit band
    beta: float = 6.0  # grid search: weight of ink outside the cells
    temper: float = 0.5  # flattens the segment log-odds (calibration)
    sig_min: float = 0.06
    sig_max: float = 0.25
    min_vis: float = 0.4  # visibility below this: the segment is unknown (P = 0.5)
    invalid_prior: float = 0.02  # prior of "not a glyph" per cell
    min_conf: float = 0.5
    dp_mid: float = 0.17  # decimal point: baseline-dot ink, as a share of the lit level, at P = 0.5
    dp_width: float = 0.05
    gap_weight: float = 4.0  # grid search: weight of ink between cells
    trail_penalty: float = 4.0  # a blank last position is rare (meters' "OL ")
    misplaced_shrink: float = 0.55  # segment confidence when the row isn't a number
    no_dp_prior: float = 0.05  # meters and supplies almost always show a decimal point (6000-count
    # meters: every range but 1000 V; supplies: always), so a missing one is unlikely, and a dot
    # too faint to see gives a low-confidence reading instead of a confident integer


DEFAULT = Params()

# glyph table as matrices: K patterns × 7 segments
_CHARS = [ch for ch in PATTERNS.values()]
_PAT = np.array([[1.0 if s in pat else 0.0 for s in SEGMENTS] for pat in PATTERNS], np.float64)
DIGITS = set("0123456789")


@dataclass
class CellRead:
    ch: str  # best character; "?" = not a glyph
    p: float  # its posterior
    seg_p: dict[str, float]  # P(lit) per segment
    seg_vis: dict[str, float]  # visibility per segment (1 = fully seen)
    dp_p: float  # P(decimal point after this cell)
    alts: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class ReadB:
    text: str | None  # "0.352", "04.00", "OL", "-1.25"; None when illegible
    value: float | None
    decimals: int
    ol: bool
    legible: bool
    confidence: float
    reason: str | None
    cells: list[CellRead]
    ms: float
    debug: dict = field(default_factory=dict)

    @property
    def key(self) -> str | None:
        """What the agreement rule compares: digits and decimal point, as a normalised number
        ("04.00" and "4.00" agree; "0.35" and "3.5" don't), or "OL"."""
        if not self.legible:
            return None
        if self.ol:
            return "OL"
        return f"{self.value:.{self.decimals}f}" if self.value is not None else None


def _illegible(reason: str, t0: float, cells: list[CellRead] | None = None, **debug) -> ReadB:
    return ReadB(None, None, 0, False, False, 0.0, reason, cells or [], (time.perf_counter() - t0) * 1000, debug)


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -40, 40)))


def detect_polarity(gray: np.ndarray) -> str:
    """'dark' (dark digits on a light background) or 'light' (lit digits on dark): the background
    is the majority, so the median sits near it."""
    p10, p50, p90 = np.percentile(gray, [10, 50, 90])
    return "dark" if (p90 - p50) < (p50 - p10) else "light"


def _integral(a: np.ndarray) -> np.ndarray:
    return cv2.integral(a.astype(np.float64))


def _rect_mean(ii: np.ndarray, x0, x1, y0, y1):
    """Mean of the image over rectangles (arrays broadcast); clipped to the image."""
    Hh, Ww = ii.shape[0] - 1, ii.shape[1] - 1
    x0 = np.clip(np.round(x0), 0, Ww).astype(np.int64)
    x1 = np.clip(np.round(x1), 0, Ww).astype(np.int64)
    y0 = np.clip(np.round(y0), 0, Hh).astype(np.int64)
    y1 = np.clip(np.round(y1), 0, Hh).astype(np.int64)
    x1 = np.maximum(x1, x0)
    y1 = np.maximum(y1, y0)
    s = ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0]
    area = (x1 - x0) * (y1 - y0)
    return np.where(area > 0, s / np.maximum(area, 1), 0.0), area


def _segment_rects(X0, X1, Y0, Y1, t):
    """Core rectangles of segments a–g and the two holes for cell boxes X0..X1 (arrays) and the
    digit band Y0..Y1, stroke width t. Returns x0, x1, y0, y1 with a last axis of 9."""
    Ym = (Y0 + Y1) / 2
    L = X1 - X0
    al_h = 0.22 * np.maximum(L - 2 * t, 1)  # along-length shrink of horizontal segments
    vh = (Y1 - Y0) / 2 - 1.5 * t
    al_v = 0.22 * np.maximum(vh, 1)
    ac = 0.25 * t  # across shrink
    hx0, hx1 = X0 + t + al_h, X1 - t - al_h
    lx0, lx1 = X0 + ac, X0 + t - ac
    rx0, rx1 = X1 - t + ac, X1 - ac
    uy0, uy1 = Y0 + t + al_v, Ym - t / 2 - al_v
    dy0, dy1 = Ym + t / 2 + al_v, Y1 - t - al_v
    def b(*arrays):  # everything to the cells' shape
        return np.broadcast_arrays(*arrays, X0 + X1)[:-1]

    x0 = np.stack(b(hx0, rx0, rx0, hx0, lx0, lx0, hx0, X0 + 1.4 * t, X0 + 1.4 * t), -1)
    x1 = np.stack(b(hx1, rx1, rx1, hx1, lx1, lx1, hx1, X1 - 1.4 * t, X1 - 1.4 * t), -1)
    y0 = np.stack(b(Y0 + ac, uy0, dy0, Y1 - t + ac, dy0, uy0, Ym - t / 2 + ac, Y0 + 1.4 * t, Ym + 0.9 * t), -1)
    y1 = np.stack(b(Y0 + t - ac, uy1, dy1, Y1 - ac, dy1, uy1, Ym + t / 2 - ac, Ym - 0.9 * t, Y1 - 1.4 * t), -1)
    return x0, x1, y0, y1


def _shear(a: np.ndarray, s: float, interp=cv2.INTER_LINEAR, border=0.0) -> np.ndarray:
    h, w = a.shape[:2]
    M = np.array([[1.0, s, 0.0], [0.0, 1.0, 0.0]], np.float32)
    return cv2.warpAffine(a, M, (w + int(np.ceil(abs(s) * h)) + 1, h), flags=interp, borderMode=cv2.BORDER_CONSTANT, borderValue=border)


def estimate_slant(norm: np.ndarray) -> float:
    """Italic slant (x shift per unit of height, top to the right) from stroke edges: where the
    ink gradient is mostly horizontal (the sides of vertical strokes), gy / gx equals the slant.
    Weighted median, so horizontal segments and noise don't move it."""
    b = cv2.GaussianBlur(norm, (0, 0), 1.2)
    gx = cv2.Sobel(b, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(b, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.abs(gx)
    sel = (mag > 0.25 * (mag.max() + 1e-6)) & (np.abs(gy) < 0.6 * mag)
    if sel.sum() < 20:
        return 0.0
    r, w = (gy[sel] / gx[sel]), mag[sel]
    order = np.argsort(r)
    cw = np.cumsum(w[order])
    med = float(r[order][np.searchsorted(cw, cw[-1] / 2)])
    return float(np.clip(round(med, 3), -0.1, 0.3))


def read_field(crop: np.ndarray, digits: int = 4, polarity: str | None = None, params: Params = DEFAULT) -> ReadB:
    t0 = time.perf_counter()
    P = params
    if crop is None or crop.size == 0:
        return _illegible("no_image", t0)
    scale = H / crop.shape[0]
    img = cv2.resize(crop, (max(8, int(round(crop.shape[1] * scale))), H), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    if img.ndim == 2:
        gray = maxc = minc = img
    else:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        maxc, minc = img.max(axis=2), img.min(axis=2)
    pol = polarity or detect_polarity(gray)
    ink = (255.0 - gray.astype(np.float32)) if pol == "dark" else maxc.astype(np.float32)
    sat = cv2.dilate((minc >= P.sat_level).astype(np.uint8), np.ones((3, 3), np.uint8)) > 0

    # 2. background
    k = max(5, int(P.open_k * H) | 1)
    bg = cv2.blur(cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((k, k), np.uint8)), (k, k))
    top = ink - bg
    valid = ~sat
    if valid.sum() < 0.2 * valid.size:
        return _illegible("saturated", t0, polarity=pol)
    lo, hi = np.percentile(top[valid], [50, 99.3])
    contrast = float(hi - lo)
    if contrast < P.min_contrast:
        return _illegible("low_contrast", t0, polarity=pol, contrast=contrast)
    norm = np.clip((top - lo) / (hi - lo), 0.0, 1.2).astype(np.float32)
    norm[sat] = 0.0
    # background anomalies: departure from a plane fitted to the background
    hh, ww = bg.shape
    ys, xs = np.mgrid[0:hh:4, 0:ww:4]
    A = np.stack([np.ones(xs.size), xs.ravel(), ys.ravel()], 1)
    coef, *_ = np.linalg.lstsq(A, bg[::4, ::4].ravel(), rcond=None)
    yy, xx = np.mgrid[0:hh, 0:ww]
    resid = np.abs(bg - (coef[0] + coef[1] * xx + coef[2] * yy))
    unseen = sat | (resid > P.anomaly * contrast)
    if pol == "dark" and img.ndim == 3:
        # On an LCD a hand or finger can be as bright as the glass; its colour gives it away.
        lab = cv2.blur(cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32), (5, 5))
        bgpx = norm < 0.25
        if bgpx.sum() > 50:
            a0, b0 = np.median(lab[..., 1][bgpx]), np.median(lab[..., 2][bgpx])
            chroma = np.hypot(lab[..., 1] - a0, lab[..., 2] - b0)
            near_ink = cv2.dilate((norm > 0.2).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0  # segments have their own tint
            foreign = ((chroma > P.chroma_dist) & ~near_ink).astype(np.uint8)
            foreign = cv2.morphologyEx(foreign, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8)) > 0
            unseen |= foreign
    unseen = unseen.astype(np.float32)

    # 3. deskew: the edges of near-vertical strokes slope by the slant (gy/gx = slant)
    slant = estimate_slant(norm)
    norm = _shear(norm, slant)
    unseen = _shear(unseen, slant, border=0.0)
    W = norm.shape[1]
    occluded = float(unseen[:, : max(1, W - int(np.ceil(abs(slant) * H)))].mean())

    # 4. digit band, stroke width, grid
    on = norm > 0.5
    rows = on.mean(axis=1)
    if rows.max() <= 0:
        return _illegible("blank", t0, polarity=pol)
    band = np.where(rows >= P.row_frac * rows.max())[0]
    Y0, Y1 = float(band.min()), float(band.max() + 1)
    h = Y1 - Y0
    if h < 0.3 * H:
        return _illegible("no_digits", t0, polarity=pol, band=[Y0, Y1])
    band_unseen = float(unseen[int(Y0) : int(Y1)].mean())
    if max(band_unseen, occluded) > P.max_unseen:
        return _illegible("occluded", t0, polarity=pol, unseen=round(max(band_unseen, occluded), 3))
    runs: list[int] = []
    for r in on[int(Y0) : int(Y1)]:
        d = np.diff(np.concatenate([[0], r.astype(np.int8), [0]]))
        starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
        runs += [e - s for s, e in zip(starts, ends) if 2 <= e - s <= h / 2]
    t = float(np.clip(np.percentile(runs, 40) if runs else 0.12 * h, 0.05 * h, 0.25 * h))

    colsum = norm[int(Y0) : int(Y1)].clip(0, 1).sum(axis=0)
    total = float(colsum.sum())
    colcount = on[int(Y0) : int(Y1)].sum(axis=0)
    lit_cols = np.where(colcount >= max(2.0, 0.4 * t))[0]  # a lone horizontal segment counts
    if total <= 0 or lit_cols.size == 0:
        return _illegible("blank", t0, polarity=pol)
    cum = np.concatenate([[0.0], np.cumsum(colsum)])
    xr0 = float(lit_cols.max() + 1)
    n = digits
    ii = _integral(norm.clip(0, 1))
    # The row is anchored at both ends: the first cell starts near the ROI's left edge (the
    # calibration drags it to the digit row; leading blank positions included) and the last lit
    # cell ends at the rightmost ink (or one cell earlier: "OL " leaves the last position blank).
    aspects = np.arange(0.34, 0.81, 0.02)
    lefts = slant * Y1 + np.arange(-0.06, 0.37, 0.03) * h  # deskewing moved the bottom-left corners right
    offs = np.array([-0.2, 0.0, 0.2, 0.4, 0.6]) * t  # the last cell's edge: at the ink (a "1") or past a segment tip
    trails = np.array([0, 1]) if n > 2 else np.array([0])
    A_, L_, O_, T_ = [g.ravel() for g in np.meshgrid(aspects, lefts, offs, trails, indexing="ij")]
    cw = A_ * h
    xr = xr0 + O_
    pitch = (xr - L_ - cw) / np.maximum(n - 1 - T_, 1) if n > 1 else cw * 1.3
    ok = (pitch >= 1.05 * cw) & (pitch <= 1.9 * cw) if n > 1 else np.ones_like(cw, bool)
    idx = np.arange(n)
    R = L_[:, None] + cw[:, None] + idx[None, :] * pitch[:, None]  # (C, n)
    X0 = R - cw[:, None]
    S_ = pitch / cw - 1
    x0, x1, y0, y1 = _segment_rects(X0, R, Y0, Y1, t)
    m, _ = _rect_mean(ii, x0, x1, y0, y1)  # (C, n, 9)
    p = _sigmoid((m - 0.45) / 0.08).clip(1e-4, 1 - 1e-4)
    seg, holes = p[..., :7], p[..., 7:]
    ll = np.log(seg) @ _PAT.T + np.log(1 - seg) @ (1 - _PAT).T  # (C, n, K)
    best = ll.max(axis=-1) + np.log(1 - holes).sum(axis=-1)
    # the gaps between cells are dark above the decimal-point zone: a grid shifted by a stroke
    # puts the strokes there (an 8 would read as a 3, a leading 1 would vanish)
    if n > 1:
        gx0 = R[:, :-1] + 0.15 * (X0[:, 1:] - R[:, :-1])
        gx1 = X0[:, 1:] - 0.15 * (X0[:, 1:] - R[:, :-1])
        gm, _ = _rect_mean(ii, gx0, np.maximum(gx1, gx0 + 1), Y0 + 0.5 * t, Y1 - 1.8 * t)
        best[:, :-1] += P.gap_weight * np.log(1 - _sigmoid((gm - 0.45) / 0.08).clip(1e-4, 1 - 1e-4))
    span0 = np.clip(np.round(X0[:, 0]), 0, W).astype(int)
    span1 = np.clip(np.round(R[:, -1] + 0.3 * cw), 0, W).astype(int)
    outside = (cum[span0] + (cum[-1] - cum[span1])) / total
    off_left = np.clip((-X0[:, 0] - 0.25 * cw) / np.maximum(cw, 1), 0, None)  # cells cut off at the left
    # a blank last position is the meter's "OL " layout only: no penalty when the cell before it is an L
    is_l = np.zeros(len(T_), bool)
    if n > 1:
        is_l = np.array([_CHARS[k] == "L" for k in ll[:, n - 2, :].argmax(axis=-1)])
    score = np.where(ok, best.sum(axis=1) - P.beta * n * outside - 50.0 * off_left - P.trail_penalty * T_ * ~is_l, -np.inf)
    if not np.isfinite(score).any():
        return _illegible("no_grid", t0, polarity=pol)
    c = int(np.argmax(score))
    grid = {"aspect": round(float(A_[c]), 3), "spacing": round(float(S_[c]), 3), "trail": int(T_[c]), "left": round(float(L_[c]), 1),
            "t": round(t, 2), "band": [Y0, Y1], "slant": round(slant, 3), "polarity": pol, "contrast": round(contrast, 1)}

    # 5. segment probabilities against the field's own lit/unlit levels
    mc = m[c]  # (n, 9)
    segm = mc[:, :7]
    vis_m, _ = _rect_mean(_integral(unseen), x0[c], x1[c], y0[c], y1[c])
    vis = 1.0 - vis_m  # (n, 9)
    lit = segm > 0.5
    if not lit.any():
        return _illegible("blank", t0, **grid)
    mu_on = float(segm[lit].mean())
    mu_off = float(segm[~lit].mean()) if (~lit).any() else 0.05
    dev = np.concatenate([segm[lit] - mu_on, segm[~lit] - mu_off])
    sig = float(np.clip(dev.std() if dev.size > 2 else 0.1, P.sig_min, P.sig_max))

    def p_lit(v, visib):
        llr = ((v - mu_off) ** 2 - (v - mu_on) ** 2) / (2 * sig * sig) * P.temper
        pr = _sigmoid(llr)
        w = np.clip((visib - P.min_vis) / (1 - P.min_vis), 0, 1)
        return (0.5 + (pr - 0.5) * w).clip(1e-6, 1 - 1e-6)

    ps = p_lit(segm, vis[:, :7])  # (n, 7)
    ph = p_lit(mc[:, 7:], vis[:, 7:])  # holes
    # decimal points: a dot at the baseline in the middle of the gap after a cell, with nothing
    # above it (a neighbouring e or c segment bleeding into the gap is lit above the baseline too)
    gap = float(pitch[c] - cw[c]) if n > 1 else 0.0
    gw = max(2.0, min(0.8 * gap, 1.3 * t))
    Ym = (Y0 + Y1) / 2
    uii = _integral(unseen)
    dps, dpv = [], []
    for i in range(n - 1):
        gc = (R[c, i] + X0[c, i + 1]) / 2
        low, _ = _rect_mean(ii, gc - gw / 2, gc + gw / 2, Y1 - 1.3 * t, Y1 + 0.2 * t)
        up, _ = _rect_mean(ii, gc - gw / 2, gc + gw / 2, Ym + 0.5 * t, Y1 - 1.8 * t)
        uv, _ = _rect_mean(uii, gc - gw / 2, gc + gw / 2, Y1 - 1.3 * t, Y1 + 0.2 * t)
        dps.append(float(low) - max(0.0, float(up)))
        dpv.append(1.0 - float(uv))
    raw = np.array(dps + [0.0]) / max(mu_on, 1e-3)
    pr = _sigmoid((raw - P.dp_mid) / P.dp_width)
    w = np.clip((np.array(dpv + [1.0]) - P.min_vis) / (1 - P.min_vis), 0, 1)
    p_dp = (0.5 + (pr - 0.5) * w).clip(1e-6, 1 - 1e-6)
    p_dp[-1] = 1e-6  # no decimal point after the last digit

    # 6. glyph posteriors per cell, with "not a glyph"
    ll = np.log(ps) @ _PAT.T + np.log(1 - ps) @ (1 - _PAT).T + np.log(1 - ph).sum(axis=1, keepdims=True)  # (n, K)
    ll_inv = 9 * np.log(0.5) + np.log(P.invalid_prior)
    full = np.concatenate([ll, np.full((n, 1), ll_inv)], axis=1)
    post = np.exp(full - full.max(axis=1, keepdims=True))
    post /= post.sum(axis=1, keepdims=True)
    chars = _CHARS + ["?"]
    cells: list[CellRead] = []
    for i in range(n):
        order = np.argsort(-post[i])
        cells.append(CellRead(chars[order[0]], float(post[i, order[0]]), {s: float(ps[i, j]) for j, s in enumerate(SEGMENTS)},
                              {s: float(vis[i, j]) for j, s in enumerate(SEGMENTS)}, float(p_dp[i]),
                              [(chars[o], round(float(post[i, o]), 4)) for o in order[1:3]]))
    # decimal point configuration: none, or exactly one
    lnone = float(np.log(1 - p_dp).sum())
    lone = [lnone - np.log(1 - p_dp[i]) + np.log(p_dp[i]) + np.log((1 - P.no_dp_prior) / max(n - 1, 1)) for i in range(n - 1)]
    configs = np.array([lnone + np.log(P.no_dp_prior)] + lone + [-np.inf])
    cp = np.exp(configs - configs.max())
    cp /= cp.sum()
    dp_at = int(np.argmax(cp)) - 1  # -1: none
    conf = float(np.prod([cr.p for cr in cells]) * cp.max())
    # OL has no decimal point by definition: its confidence uses the dot evidence without the prior
    flat = np.array([lnone] + [lnone - np.log(1 - p_dp[i]) + np.log(p_dp[i]) for i in range(n - 1)])
    conf_ol = float(np.prod([cr.p for cr in cells]) * np.exp(flat[0] - flat.max()) / np.exp(flat - flat.max()).sum())

    # 7. the reading
    ch = [cr.ch for cr in cells]
    dbg = grid | {"chars": "".join(ch), "dp_at": dp_at}
    reason, text, ol = _structure(ch, dp_at)
    # A segment's confidence includes the doubt about where its cell is: shrunk by the cell's
    # glyph posterior, and more when the row doesn't decode to a number (the grid is likely off).
    shrink = 1.0 if reason is None else P.misplaced_shrink
    for cr in cells:
        f = shrink * (cr.p if cr.ch != "?" else 0.3)
        cr.seg_p = {k: 0.5 + (v - 0.5) * f for k, v in cr.seg_p.items()}
    if reason is not None:
        return _illegible(reason, t0, cells, **dbg)
    assert text is not None
    legible = conf >= P.min_conf
    ms = (time.perf_counter() - t0) * 1000
    if ol:
        legible = conf_ol >= P.min_conf
        return ReadB("OL", None, 0, True, legible, conf_ol, None if legible else "low_confidence", cells, ms, dbg)
    decimals = (n - 1 - dp_at) if dp_at >= 0 else 0
    return ReadB(text, float(text), decimals, False, legible, conf, None if legible else "low_confidence", cells, ms, dbg)


def _structure(ch: list[str], dp_at: int) -> tuple[str | None, str | None, bool]:
    """(reason it isn't a number, text, is OL). Leading blanks only; a minus only first; digits
    otherwise; "0L" (with blanks around) is OL; the decimal point on a digit."""
    n = len(ch)
    if "?" in ch:
        return "invalid_glyph", None, False
    first = next((i for i, x in enumerate(ch) if x != " "), None)
    if first is None:
        return "blank", None, False
    body = ch[first:]
    while body and body[-1] == " ":
        body = body[:-1]
    if body == ["0", "L"]:
        return None, "OL", True
    if len(body) != n - first:
        return "trailing_blank", None, False
    if " " in body:
        return "gap", None, False
    if any(x not in DIGITS and not (x == "-" and k == 0) for k, x in enumerate(body)):
        return "non_numeric", None, False
    if 0 <= dp_at < first:
        return "dp_on_blank", None, False
    text = "".join(ch[i] + ("." if i == dp_at else "") for i in range(first, n))
    if text in ("-", "") or text.endswith(".") or (text.startswith("-") and len(text) < 2):
        return "non_numeric", None, False
    return None, text, False
