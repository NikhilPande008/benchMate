"""Cost control for eval runs (CLAUDE.md "Cost control").

Before a run: `estimate()` prices the planned lines from earlier runs' recorded usage, with no API
calls. During a run: `BudgetGuard.check()` asks the engine (GET /dev/cost) what the batch has cost
so far, logs the line's cost, and raises `BudgetExceeded` once the batch passes its budget.
"""
from __future__ import annotations

import fnmatch
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from engine.telemetry.cost import over_budget, usd_per_line_from_history

DEFAULT_BUDGET_USD = 2.0
MAX_LANES = 2


class BudgetExceeded(Exception):
    pass


@dataclass
class BudgetGuard:
    base: str
    run_prefix: str  # the whole batch: every lane's runs start with it
    budget_usd: float | None = DEFAULT_BUDGET_USD
    spent_usd: float = 0.0
    log: Callable[[str], None] = print

    def total(self) -> float:
        url = f"{self.base}/dev/cost?run={urllib.parse.quote(self.run_prefix)}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return float(json.load(r)["usd"])

    def check(self, label: str) -> float:
        """Cost since the last check (this line); raises once the batch is over budget."""
        total = self.total()
        line, self.spent_usd = total - self.spent_usd, total
        cap = f" / budget ${self.budget_usd:.2f}" if self.budget_usd is not None else ""
        self.log(f"    cost {label}: ${line:.4f} (batch ${total:.3f}{cap})")
        if over_budget(total, self.budget_usd):
            raise BudgetExceeded(f"batch {self.run_prefix} has cost ${total:.3f}, budget ${self.budget_usd:.2f}")
        return line


def select_lines(lines: list[dict], patterns: list[str] | None) -> list[dict]:
    """Lines whose id matches any of the patterns (shell-style: t01, p0*, *-pause); all if none."""
    if not patterns:
        return lines
    return [l for l in lines if any(fnmatch.fnmatch(l["id"], p) for p in patterns)]


# a new prompt is priced from its predecessor until it has history of its own (same length, same tools)
SIMILAR_PROMPT = {"v4_local": "v3_local", "v3": "v3_local"}


def estimate(db, model_id: str, n_lines: int, history_prefixes: list[str], prompt: str | None = None) -> tuple[float, float, int]:
    """(USD for n_lines, USD per line, lines of history it is based on). History from earlier runs
    with the same model and prompt, else its predecessor's, else any prompt's."""
    for p in dict.fromkeys([prompt, SIMILAR_PROMPT.get(prompt or ""), None]):  # in order, no repeats
        per_line, seen = usd_per_line_from_history(db, model_id, history_prefixes, p)
        if seen:
            break
    return per_line * n_lines, per_line, seen
