"""uv run python -m mcp_evidence.synth --out eval/display_set/synthetic --samples 700 --seed 7"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from .dataset import write_dataset


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("eval/display_set/synthetic"))
    ap.add_argument("--samples", type=int, default=700, help="3 frames each")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    t0 = time.time()
    meta = write_dataset(a.out, a.samples, a.seed)
    print(f"{meta['images']} images ({meta['samples']} samples) in {a.out} in {time.time() - t0:.1f} s")
    for k, v in meta["counts"].items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
