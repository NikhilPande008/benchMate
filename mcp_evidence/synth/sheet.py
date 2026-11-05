"""Contact sheet of a generated set, for looking at it: one row per distortion, frame a of the
first few samples, with the truth under each.

    uv run python -m mcp_evidence.synth.sheet eval/display_set/synthetic --out /tmp/sheet.jpg
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from .dataset import DISTORTIONS, load_labels

TILE = (300, 190)


def _caption(lab: dict) -> str:
    t = lab["truth"]
    if lab["display"] == "meter":
        return f"{t['main']['text']} {lab['unit']}"
    return f"{t['set_voltage']['text']}V {t['live_current']['text']}A lim {t['current_limit']['text']}"


def sheet(root: Path, per_row: int = 6, frame: int = 0) -> np.ndarray:
    labels = load_labels(root)
    rows = []
    for d in DISTORTIONS:
        tiles = []
        for lab in [x for x in labels if x["distortion"] == d][:per_row]:
            img = cv2.imread(str(root / lab["frames"][frame]))
            img = cv2.resize(img, (TILE[0], TILE[1] - 30), interpolation=cv2.INTER_AREA)
            tile = np.full((TILE[1], TILE[0], 3), 255, np.uint8)
            tile[: TILE[1] - 30] = img
            ok = "" if all(lab["readable"].values()) else " (unreadable)"
            cv2.putText(tile, f"{d} {lab['severity']:.2f}{ok}", (4, TILE[1] - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 0, 0), 1, cv2.LINE_AA)
            cv2.putText(tile, _caption(lab), (4, TILE[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 0, 160), 1, cv2.LINE_AA)
            tiles.append(tile)
        while len(tiles) < per_row:
            tiles.append(np.full((TILE[1], TILE[0], 3), 255, np.uint8))
        rows.append(np.hstack(tiles))
    return np.vstack(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--per-row", type=int, default=6)
    ap.add_argument("--frame", type=int, default=0)
    a = ap.parse_args()
    cv2.imwrite(str(a.out), sheet(a.root, a.per_row, a.frame), [cv2.IMWRITE_JPEG_QUALITY, 85])
    print(a.out)


if __name__ == "__main__":
    main()
