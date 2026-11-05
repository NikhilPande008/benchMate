"""API cost from recorded usage: realtime `response.done` token usage and transcription minutes.

The browser stores each response's usage as a `response_done` span; the harness stores each
scripted line as a `line_start` span with its clip length. Prices are in engine/config.py.

Input token details count cached tokens inside `audio_tokens` and `text_tokens`, so the uncached
part is the total minus `cached_tokens_details`. Spans recorded before 4 Nov evening have no
audio/text split for input: their uncached input is priced as audio (an upper bound).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session, select

from engine.config import REALTIME_PRICES, TRANSCRIPTION_PRICES_PER_MIN

from .store import RealtimeSessionRow, Span


def _n(attrs: dict[str, Any], key: str) -> int:
    v = attrs.get(key)
    return int(v) if isinstance(v, (int, float)) else 0


def response_cost(model: str, usage: dict[str, Any]) -> float:
    """USD for one response, from the attrs of its `response_done` span."""
    p = REALTIME_PRICES.get(model) or REALTIME_PRICES["gpt-realtime-2.1"]  # unknown model: price as flagship
    out_audio, out_text = _n(usage, "out_audio_tokens"), _n(usage, "out_text_tokens")
    if not out_audio and not out_text:
        out_audio = _n(usage, "out_tokens")
    if "in_audio_tokens" in usage or "in_text_tokens" in usage:
        cached_a, cached_t = _n(usage, "in_cached_audio_tokens"), _n(usage, "in_cached_text_tokens")
        audio = max(0, _n(usage, "in_audio_tokens") - cached_a)
        text = max(0, _n(usage, "in_text_tokens") - cached_t)
    else:  # older spans: no split; cached priced as text (cheap), the rest as audio (dear)
        cached_a, cached_t = 0, _n(usage, "in_cached_tokens")
        audio, text = max(0, _n(usage, "in_tokens") - cached_t), 0
    per_m = (
        audio * p["audio_in"] + cached_a * p["audio_cached"] + text * p["text_in"] + cached_t * p["text_cached"]
        + out_audio * p["audio_out"] + out_text * p["text_out"]
    )
    return per_m / 1_000_000


def transcription_cost(model: str, seconds: float) -> float:
    return TRANSCRIPTION_PRICES_PER_MIN.get(model, 0.006) * seconds / 60.0


@dataclass
class RunCost:
    run: str
    usd: float = 0.0
    realtime_usd: float = 0.0
    transcription_usd: float = 0.0
    responses: int = 0
    lines: int = 0
    per_line: dict[str, float] = field(default_factory=dict)  # line id -> USD (responses after it started)

    @property
    def usd_per_line(self) -> float | None:
        return self.usd / self.lines if self.lines else None

    def line(self) -> dict[str, Any]:
        return asdict(self) | {"usd": round(self.usd, 4), "usd_per_line": round(self.usd_per_line, 4) if self.lines else None}


def run_cost(db: Engine, run: str, transcription_model: str = "gpt-transcribe", model: str | None = None,
             prompt: str | None = None) -> RunCost:
    """Cost of every session whose run id starts with `run` (a run id or a batch prefix); with
    `model` / `prompt`, only sessions with that model / prompt."""
    with Session(db) as s:
        # autoescape: run ids contain "_", a LIKE wildcard
        rows = list(s.exec(select(RealtimeSessionRow).where(RealtimeSessionRow.run_id.startswith(run, autoescape=True))))  # type: ignore[union-attr]
        sessions = {r.id: r.model for r in rows if prompt is None or r.prompt == prompt}
        spans = list(s.exec(
            select(Span).where(Span.run_id.startswith(run, autoescape=True), Span.name.in_(["response_done", "line_start"]))  # type: ignore[union-attr]
            .order_by(Span.t_ms)
        ))
    out = RunCost(run)
    current: dict[str, str] = {}  # session -> current line id
    for span in spans:
        if span.session_id not in sessions or (model is not None and sessions[span.session_id] != model):
            continue
        attrs = json.loads(span.attrs) if span.attrs else {}
        if span.name == "line_start":
            out.lines += 1
            line_id = str(attrs.get("id", f"line{out.lines}"))
            current[span.session_id] = line_id
            usd = transcription_cost(transcription_model, (attrs.get("clip_ms") or 0) / 1000)
            out.transcription_usd += usd
        else:
            out.responses += 1
            line_id = current.get(span.session_id, "connect")
            usd = response_cost(sessions.get(span.session_id, "gpt-realtime-2.1"), attrs)
            out.realtime_usd += usd
        out.per_line[line_id] = out.per_line.get(line_id, 0.0) + usd
    out.usd = out.realtime_usd + out.transcription_usd
    return out


DEFAULT_USD_PER_LINE = {"gpt-realtime-2.1-mini": 0.02, "gpt-realtime-2.1": 0.06}  # used when there is no history


def usd_per_line_from_history(db: Engine, model: str, run_prefixes: list[str], prompt: str | None = None) -> tuple[float, int]:
    """(mean USD per scripted line, lines seen) over earlier runs with these prefixes that recorded
    usage. With no such runs: DEFAULT_USD_PER_LINE for the model, and 0 lines seen."""
    total, lines = 0.0, 0
    for prefix in run_prefixes:
        c = run_cost(db, prefix, model=model, prompt=prompt)
        if c.responses and c.lines:
            total += c.usd
            lines += c.lines
    if lines:
        return total / lines, lines
    return DEFAULT_USD_PER_LINE.get(model, 0.06), 0


def over_budget(spent_usd: float, budget_usd: float | None) -> bool:
    return budget_usd is not None and spent_usd >= budget_usd
