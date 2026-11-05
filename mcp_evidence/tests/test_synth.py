"""Synthetic display generator (test data only): layouts, reproducibility, labels, distortions."""
from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from mcp_evidence.synth.dataset import DISTORTIONS, load_labels, make_sample, write_dataset
from mcp_evidence.synth.displays import FieldSpec, MeterReading, random_style, render_meter
from mcp_evidence.synth.segments import GLYPHS, PATTERNS, Cell, cells_text, layout, lit_segments, segment_polygons


@pytest.mark.parametrize(
    "text,n,cells",
    [
        ("0.352", 4, [("0", True), ("3", False), ("5", False), ("2", False)]),
        ("4.00", 4, [(" ", False), ("4", True), ("0", False), ("0", False)]),
        ("04.00", 4, [("0", False), ("4", True), ("0", False), ("0", False)]),
        ("12.34", 4, [("1", False), ("2", True), ("3", False), ("4", False)]),
        ("OL ", 4, [(" ", False), ("O", False), ("L", False), (" ", False)]),
        ("-1.25", 4, [("-", False), ("1", True), ("2", False), ("5", False)]),
        (".5", 3, [(" ", False), ("0", True), ("5", False)]),
    ],
)
def test_layout(text, n, cells):
    got = layout(text, n)
    assert [(c.ch, c.dp) for c in got] == cells
    assert cells_text(got) == text.strip() if not text.startswith(".") else "0.5"


def test_layout_rejects_what_does_not_fit():
    with pytest.raises(ValueError):
        layout("12.345", 4)
    with pytest.raises(ValueError):
        layout("x", 4)


def test_every_glyph_style_reads_back_as_its_character():
    for ch, styles in GLYPHS.items():
        for s in styles:
            assert PATTERNS[frozenset(s)] == ("0" if ch == "O" else ch)
    digits = {PATTERNS[frozenset(s)] for ch in "0123456789" for s in GLYPHS[ch]}
    assert digits == set("0123456789")


def test_segment_polygons_have_seven_hexagons_inside_the_cell():
    polys = segment_polygons(10, 20, 30, 60, 6, 0.0, 1)
    assert set(polys) == set("abcdefg") and all(p.shape == (6, 2) for p in polys.values())
    allp = np.concatenate(list(polys.values()))
    assert allp[:, 0].min() >= 10 and allp[:, 0].max() <= 40 and allp[:, 1].min() >= 20 and allp[:, 1].max() <= 80


def test_the_renderer_draws_what_the_label_says():
    """Clean, flat, no camera effects: every segment of the truth is dark, every other one light,
    sampled at the segment centres."""
    rng = np.random.default_rng(1)
    st = random_style(rng, "lcd")
    for text in ["0.352", "8.888", "1.074", "5.169"]:
        cells = layout(text, 4)
        r = render_meter(np.random.default_rng(2), st, MeterReading("diode", text, "diode_V"), FieldSpec(cells))
        gray = cv2.cvtColor(r.image, cv2.COLOR_BGR2GRAY).astype(float)
        bg = np.median(gray[int(r.glass[0][1]) + 2 : int(r.glass[2][1]) - 2, int(r.glass[0][0]) + 2 : int(r.glass[1][0]) - 2])
        for cell, quad in zip(cells, r.cells["main"]):
            x0 = quad[3][0]
            y0 = quad[0][1]
            w = quad[2][0] - quad[3][0]
            h = quad[3][1] - quad[0][1]
            polys = segment_polygons(x0, y0, w, h, st.thickness * h, st.slant, st.gap * h)
            on = set(lit_segments(cell.ch, st.glyph_style))
            for s, pts in polys.items():
                cx, cy = pts.mean(axis=0)
                v = gray[int(round(cy)), int(round(cx))]
                if s in on:
                    assert v < bg - 40, (text, cell.ch, s, v, bg)
                else:
                    assert v > bg - 25, (text, cell.ch, s, v, bg)


@pytest.mark.parametrize("distortion", DISTORTIONS)
@pytest.mark.parametrize("display", ["meter", "supply"])
def test_every_distortion_makes_three_labelled_frames(display, distortion):
    if distortion == "dim_lcd" and display == "supply":
        pytest.skip("dim LCD is the meter's low battery")
    s = make_sample(7, 3, display, distortion)
    assert len(s.frames) == 3 and all(f.dtype == np.uint8 and f.shape == s.frames[0].shape for f in s.frames)
    fields = {"main"} if display == "meter" else {"set_voltage", "live_current", "current_limit"}
    assert set(s.truth) == fields == set(s.fields) == set(s.cells) == set(s.readable)
    h, w = s.frames[0].shape[:2]
    for f in fields:
        assert len(s.cells[f]) == 4
        q = s.fields[f]
        assert q[:, 0].min() >= -1 and q[:, 0].max() <= w + 1 and q[:, 1].min() >= -1 and q[:, 1].max() <= h + 1
    lab = s.label(["a", "b", "c"])
    assert lab["synthetic"] is True and json.loads(json.dumps(lab)) == lab


def test_samples_are_reproducible_from_the_seed():
    a, b = make_sample(7, 42), make_sample(7, 42)
    assert all(np.array_equal(x, y) for x, y in zip(a.frames, b.frames))
    assert a.label([]) == b.label([])
    assert not np.array_equal(make_sample(8, 42).frames[0], a.frames[0])


def test_flicker_changes_exactly_one_frame():
    for i in range(5):
        s = make_sample(7, i, "supply", "flicker")
        marks = [ft.count("flicker") for ft in s.frame_truth.values()]
        assert set(marks) == {1}
        k = s.frame_truth["set_voltage"].index("flicker")
        others = [j for j in range(3) if j != k]
        d_same = np.abs(s.frames[others[0]].astype(int) - s.frames[others[1]].astype(int)).mean()
        d_flick = np.abs(s.frames[k].astype(int) - s.frames[others[0]].astype(int)).mean()
        assert d_flick > d_same


def test_mid_update_settles_on_the_new_value():
    for i in range(6):
        s = make_sample(7, i, "meter", "mid_update")
        seq = s.frame_truth["main"]
        assert seq[-1] == s.truth["main"].text and seq[-2] in (s.truth["main"].text, "mixed")
        assert s.notes and "→" in s.notes[0]


def test_a_hand_over_the_digits_makes_the_sample_unreadable():
    got = [make_sample(7, i, "meter", "hand") for i in range(12)]
    assert any(not s.readable["main"] for s in got) and any(s.readable["main"] for s in got)


def test_perspective_moves_the_corners():
    s = make_sample(7, 5, "meter", "perspective")
    q = s.fields["main"]
    assert abs(q[0][1] - q[1][1]) > 0.5 or abs(q[0][0] - q[3][0]) > 0.5


def test_write_dataset(tmp_path):
    meta = write_dataset(tmp_path, 12, 7)
    labels = load_labels(tmp_path)
    assert meta["synthetic"] and meta["images"] == 36 and len(labels) == 12
    assert {lab["distortion"] for lab in labels} == set(DISTORTIONS)
    for lab in labels:
        for f in lab["frames"]:
            img = cv2.imread(str(tmp_path / f))
            assert img is not None and img.shape[1] == lab["size"][0]


def test_a_very_dim_lcd_is_unreadable_when_lit_and_unlit_segments_look_alike():
    from mcp_evidence.synth.dataset import DIM_READABLE

    got = [make_sample(7, i, "meter", "dim_lcd") for i in range(30)]
    for s in got:
        ink = 1.0 - 0.7 * s.severity
        assert s.readable["main"] is (ink - s.style.ghost >= DIM_READABLE)
        assert s.extra["battery_low"] is True
    assert any(not s.readable["main"] for s in got) and any(s.readable["main"] for s in got)
