"""Print p50/p95 latency from the telemetry database.

    uv run python -m engine.telemetry                # every session, grouped by model
    uv run python -m engine.telemetry --run RUN_ID   # one eval run
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from engine.config import Settings

from . import format_summary, load_sessions, load_spans, make_engine, summarize, turn_metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", help="only this run_id")
    parser.add_argument("--db", help="SQLite path (default: BENCHMATE_DB)")
    args = parser.parse_args()

    settings = Settings.from_env()
    engine = make_engine(Path(args.db) if args.db else settings.db_path)
    sessions = load_sessions(engine, args.run)
    by_model: dict[str, list[str]] = defaultdict(list)
    for s in sessions:
        by_model[s.model].append(s.id)
    if not by_model:
        print("no sessions recorded" + (f" for run {args.run}" if args.run else ""))
        return
    for model, ids in by_model.items():
        turns = turn_metrics(load_spans(engine, args.run, ids))
        print(format_summary(f"{model}  ({len(ids)} session(s), {len(turns)} turn(s))", summarize(turns)))
        print()


if __name__ == "__main__":
    main()
