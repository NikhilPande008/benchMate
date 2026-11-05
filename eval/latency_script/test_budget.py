"""Harness cost control: line subsets, the per-line budget stop, the lane cap."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from budget import MAX_LANES, BudgetExceeded, BudgetGuard, select_lines  # noqa: E402

LINES = [{"id": i} for i in ["p01", "p02", "t01", "t02", "p03-pause", "t03"]]


@pytest.mark.parametrize(
    "patterns,ids",
    [
        (None, ["p01", "p02", "t01", "t02", "p03-pause", "t03"]),
        (["t01"], ["t01"]),
        (["t0*"], ["t01", "t02", "t03"]),
        (["p01", "*-pause"], ["p01", "p03-pause"]),
        (["zz"], []),
    ],
)
def test_select_lines(patterns, ids):
    assert [l["id"] for l in select_lines(LINES, patterns)] == ids


class FakeGuard(BudgetGuard):
    """Batch totals come from a list instead of the engine."""

    def __init__(self, totals, budget):
        self.logs: list[str] = []
        super().__init__("http://x", "x-1-", budget, log=self.logs.append)
        self.totals = iter(totals)

    def total(self) -> float:
        return next(self.totals)


def test_guard_logs_each_line_and_stops_at_the_budget():
    g = FakeGuard([0.40, 1.10, 1.95, 2.05], budget=2.0)
    assert g.check("a") == pytest.approx(0.40)
    assert g.check("b") == pytest.approx(0.70)
    assert g.check("c") == pytest.approx(0.85)
    with pytest.raises(BudgetExceeded, match=r"\$2\.050"):
        g.check("d")
    assert g.logs[1] == "    cost b: $0.7000 (batch $1.100 / budget $2.00)"


def test_guard_without_a_budget_never_stops():
    g = FakeGuard([10.0, 20.0], budget=None)
    g.check("a")
    g.check("b")
    assert g.spent_usd == 20.0


def test_at_most_two_lanes():
    assert MAX_LANES == 2
    import run_experiments

    with pytest.raises(SystemExit):
        sys.argv = ["run_experiments.py", "--lanes", "3", "--estimate"]
        run_experiments.main()


def test_estimate_prices_a_new_prompt_from_its_predecessor(tmp_path):
    import json as _json

    from sqlmodel import Session

    from budget import estimate
    from engine.telemetry import RealtimeSessionRow, Span, make_engine
    from engine.telemetry.cost import response_cost

    db = make_engine(tmp_path / "e.sqlite")
    usage = {"in_text_tokens": 2000, "out_audio_tokens": 300}
    with Session(db) as s:
        for sid, prompt in (("se_3", "v3_local"), ("se_2", "v2_local")):
            s.add(RealtimeSessionRow(id=sid, run_id=f"x-1-{prompt}", model="gpt-realtime-2.1-mini", prompt_version="x",
                                     created_ms=0, prompt=prompt))
            s.add(Span(run_id=f"x-1-{prompt}", session_id=sid, name="line_start", t_ms=1, source="browser", attrs='{"id": "a"}'))
            u = usage if prompt == "v3_local" else {"in_text_tokens": 10}
            s.add(Span(run_id=f"x-1-{prompt}", session_id=sid, name="response_done", t_ms=2, source="browser", attrs=_json.dumps(u)))
        s.commit()
    usd, per_line, seen = estimate(db, "gpt-realtime-2.1-mini", 10, ["x-"], "v4_local")
    assert seen == 1 and per_line == pytest.approx(response_cost("gpt-realtime-2.1-mini", usage))
    assert usd == pytest.approx(10 * per_line)
