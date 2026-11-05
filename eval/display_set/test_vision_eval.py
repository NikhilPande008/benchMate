"""The display-set evaluation code: real_misc labels (CSV and JSONL), the read path on a
folder of photos, the fixture Reader A's 'what is visible' model, and the outcome counting."""
from __future__ import annotations

import json

import cv2
import numpy as np

from eval.display_set import vision_eval as ve
from mcp_evidence.synth.displays import FieldSpec, MeterReading, random_style, render_meter
from mcp_evidence.synth.segments import layout


def _photo(text: str, seed: int = 3):
    st = random_style(np.random.default_rng(seed), "lcd", "upright")
    r = render_meter(np.random.default_rng(seed + 1), st, MeterReading("diode", text, "diode_V"), FieldSpec(layout(text, 4)))
    return r.image, r.fields["main"]


def test_real_misc_labels_csv_and_jsonl(tmp_path):
    (tmp_path / "labels.csv").write_text("file,text,corners,digits,polarity,readable,notes\n"
                                         "a.jpg,12.34,,4,dark,yes,shade\n"
                                         "b.jpg,0.352,1 2 3 4 5 6 7 8,,light,no,\n")
    labs = ve.real_misc_labels(tmp_path)
    assert [l["key"] for l in labs] == ["12.34", "0.352"] and labs[0]["corners"] is None and labs[1]["corners"][3] == [7.0, 8.0]
    assert labs[1]["digits"] == 4 and labs[1]["readable"] is False and labs[0]["polarity"] == "dark"
    (tmp_path / "labels.csv").unlink()
    (tmp_path / "labels.jsonl").write_text(json.dumps({"file": "c.jpg", "text": "OL"}) + "\n")
    assert ve.real_misc_labels(tmp_path)[0]["key"] == "OL"
    assert ve.real_misc_labels(tmp_path / "missing") == []


def test_real_misc_reads_cropped_and_cornered_photos(tmp_path):
    img, quad = _photo("1.074")
    cv2.imwrite(str(tmp_path / "full.png"), img)
    x0, y0 = quad.min(axis=0).astype(int)
    x1, y1 = quad.max(axis=0).astype(int)
    cv2.imwrite(str(tmp_path / "crop.png"), img[y0:y1, x0:x1])
    corners = " ".join(f"{v:.1f}" for v in np.asarray(quad).ravel())
    (tmp_path / "labels.csv").write_text(f"file,text,corners,digits\nfull.png,1.074,{corners},4\ncrop.png,1.074,,4\nmissing.png,1.0,,\n")
    res = ve.real_misc_reads(tmp_path)
    assert len(res) == 2 and all(r.key == "1.074" for _, r in res)
    s = ve.real_misc_summary(res)
    assert s["correct"] == 2 and s["acc"] == 1.0 and s["items"][0]["read"] == "1.074"


def test_visible_text_reads_what_a_hand_leaves():
    cells = ["abcdef.", "bc", "abdeg", "abcdg"]  # 0.123
    assert ve.visible_text(cells, ["", "", "", ""]) == "0.123"
    # a hidden last digit looks blank: the pessimistic fixture reads "0.12", as a careless reader would
    assert ve.visible_text(cells, ["", "", "", "abcdg"]) == "0.12"
    assert ve.visible_text(cells, ["", "", "", "f"]) == "0.123"  # f is unlit in a 3 anyway
    assert ve.visible_text(["", "abcdef", "def", ""], ["", "", "", ""]) == "OL"
    assert ve.visible_text(cells, ["", "", "", "a"]) is None  # a 3 without a: no glyph


def test_shift_dp_moves_the_point_by_one():
    rng = np.random.default_rng(0)
    for _ in range(20):
        t = ve._shift_dp("0.352", rng)
        assert t in ("03.52",) or t is None or t.replace(".", "") == "0352"


def test_outcome_counting():
    o = ve.Outcome("s", "main", "clean", "tune", True, "0.352", "agreed", "0.352", "ok", 1, False)
    w = ve.Outcome("s", "main", "clean", "tune", True, "0.352", "agreed", "3.52", "ok", 1, False)
    u = ve.Outcome("s", "main", "clean", "tune", True, "0.352", "unclear", None, "unclear", 2, True)
    m = ve.Outcome("s", "main", "clean", "tune", True, "0.352", "agreed", "0.352", "wrong_mode", 1, False)
    assert [x.result for x in (o, w, u, m)] == ["accepted_correct", "accepted_wrong", "to_spoken", "to_spoken"]
    t = ve.agreement_table([o, w, u])["clean"]
    assert (t["accepted_correct"], t["accepted_wrong"], t["to_spoken"], t["fields"]) == (1, 1, 1, 3)
