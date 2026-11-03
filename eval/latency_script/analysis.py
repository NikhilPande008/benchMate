"""Per-line metrics for turn-driven harness runs (run_experiments.py).

The harness records `line_start` with the exact wall-clock start and end of speech in each
scripted line, so everything here is measured from the *real* end of speech:

- end_to_server:   real end -> the server's end-of-turn signal reaches the browser
                   (`speech_stopped` for VAD modes; `input_audio_buffer.committed` for push-to-talk)
- end_to_audible:  real end -> first audible model audio (plain lines: the reply)
- server_to_audio: end-of-turn signal -> `output_audio_buffer.started` (the model's own time)
- end_to_preamble: real end -> first audible preamble (model's, or the local clip)
- end_to_value:    real end -> audible start of the reply after the tool result
- turn_to_call:    end-of-turn signal -> the model's tool call (a spoken model preamble delays it)
- false cut-off:   an end-of-turn signal before the real end, followed within 1.5 s by another
                   speech start in the same line
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine

from engine.telemetry import Span, Transcript, lang_tag, load_sessions, load_spans, load_transcripts, percentile

ONSET_WINDOW_MS = 1000
LONG_REPLY_MS = 6000
CUTOFF_WINDOW_MS = 1500
CONNECT_STAGES = (
    "early_start",
    "voice_start",
    "secret_fetched",
    "mic_granted",
    "engine_ready",
    "pc_offer",
    "sdp_exchange",
    "pc_connected",
    "session_created",
    "ready",
)


def _attrs(s: Span) -> dict[str, Any]:
    return json.loads(s.attrs) if s.attrs else {}


def _first(spans: list[Span], name: str, after: float, before: float = float("inf")) -> Span | None:
    return next((s for s in spans if s.name == name and after <= s.t_ms < before), None)


@dataclass
class LineRow:
    id: str
    session_id: str
    set: str
    expected: str
    lang: str
    pause_ms: int
    observed: str = "none"
    false_cutoff: bool = False
    end_to_server: float | None = None
    server_to_audio: float | None = None
    end_to_audible: float | None = None
    end_to_preamble: float | None = None
    end_to_value: float | None = None
    tool_ms: float | None = None
    turn_to_call: float | None = None  # end-of-turn signal -> the model's tool call reaches the browser
    result_to_created: float | None = None
    model_spoke_before_result: bool | None = None
    local_clip_lang: str | None = None
    reply_ms: list[float] = field(default_factory=list)
    reply_audio_tokens: list[int] = field(default_factory=list)
    truncated: int = 0
    user_text: str = ""
    user_lang_api: str | None = None
    reply_text: str = ""
    reply_lang: str | None = None
    timed_out: bool = False
    problem: str | None = None


def analyse_session(spans: list[Span], transcripts: list[Transcript]) -> list[LineRow]:
    spans = sorted(spans, key=lambda s: s.t_ms)
    starts = [s for s in spans if s.name == "line_start"]
    rows = []
    for i, ls in enumerate(starts):
        a = _attrs(ls)
        lo, hi = ls.t_ms, starts[i + 1].t_ms if i + 1 < len(starts) else float("inf")
        win = [s for s in spans if lo <= s.t_ms < hi]
        true_end = a["true_end"]
        row = LineRow(
            id=a["id"], session_id=ls.session_id, set=a["set"], expected=a["kind"], lang=a["lang"], pause_ms=a.get("pause_ms", 0)
        )
        done = _first(win, "line_done", lo)
        row.timed_out = bool(done and _attrs(done).get("timed_out"))

        ends = [s for s in win if s.name == "speech_end"]
        speech_starts = [s for s in win if s.name == "speech_started"]
        row.false_cutoff = any(
            e.t_ms <= true_end + 100
            and any(e.t_ms < st.t_ms <= e.t_ms + CUTOFF_WINDOW_MS and st.t_ms <= true_end + 600 for st in speech_starts)
            for e in ends
        )
        if not ends:
            row.problem = "no end-of-turn signal"
            rows.append(row)
            continue
        final = ends[-1]
        ptt = _attrs(final).get("mode") == "ptt"
        committed = _first(win, "committed", final.t_ms) if ptt else None
        server_end = committed.t_ms if committed else final.t_ms
        row.end_to_server = server_end - true_end

        fa = _first(win, "first_audio", final.t_ms)
        onset = _first(win, "audio_onset", fa.t_ms, fa.t_ms + ONSET_WINDOW_MS) if fa else None
        if fa:
            row.server_to_audio = fa.t_ms - server_end
        if onset:
            row.end_to_audible = onset.t_ms - true_end

        calls = [s for s in win if s.name == "tool_call_emitted"]
        row.observed = "tool" if calls else "plain"
        if calls:
            row.turn_to_call = calls[-1].t_ms - server_end
            result = _first(win, "tool_result_received", calls[-1].t_ms)
            started = _first(win, "tool_started", calls[-1].t_ms - 50)
            finished = _first(win, "tool_finished", calls[-1].t_ms)
            if started and finished:
                row.tool_ms = finished.t_ms - started.t_ms
            local = _first(win, "local_preamble_onset", final.t_ms)
            clip = _first(win, "local_preamble_start", final.t_ms)
            if clip:
                row.local_clip_lang = _attrs(clip).get("lang")
            if result:
                spoke = fa is not None and fa.t_ms < result.t_ms
                row.model_spoke_before_result = spoke
                heard = [t for t in (local.t_ms if local else None, onset.t_ms if (onset and spoke) else None) if t]
                if heard:
                    row.end_to_preamble = min(heard) - true_end
                held = _first(win, "tool_output_held", result.t_ms)
                sent_at = held.t_ms if held else result.t_ms
                created = _first(win, "response_created", sent_at)
                if created:
                    row.result_to_created = created.t_ms - sent_at
                va = _first(win, "first_audio", result.t_ms)
                v_onset = _first(win, "audio_onset", va.t_ms, va.t_ms + ONSET_WINDOW_MS) if va else None
                if v_onset:
                    row.end_to_value = v_onset.t_ms - true_end
            else:
                row.problem = "tool call without result"
        if row.observed != row.expected and not row.problem:
            row.problem = f"expected {row.expected}, model did {row.observed}"

        # Replies: playback length of every non-preamble response in this line.
        done_by_id = {_attrs(s).get("response_id"): _attrs(s) for s in win if s.name == "response_done"}
        stops = {_attrs(s).get("response_id"): s.t_ms for s in win if s.name in ("audio_stopped", "audio_cleared")}
        reply_ids = []
        for s in win:
            if s.name != "first_audio":
                continue
            rid = _attrs(s).get("response_id")
            info = done_by_id.get(rid, {})
            if "function_call" in (info.get("outputs") or ""):
                continue  # the preamble response
            reply_ids.append(rid)
            if rid in stops:
                row.reply_ms.append(stops[rid] - s.t_ms)
                if info.get("out_audio_tokens") is not None:
                    row.reply_audio_tokens.append(info["out_audio_tokens"])
        row.truncated = sum(1 for info in done_by_id.values() if info.get("reason") == "max_output_tokens")

        item_ids = {_attrs(e).get("item_id") for e in ends} | ({_attrs(committed).get("item_id")} if committed else set())
        users = [t for t in transcripts if t.role == "user" and t.item_id in item_ids]
        if users:
            row.user_text = " ".join(t.text for t in users)
            row.user_lang_api = users[-1].lang_api
        replies = [t for t in transcripts if t.role == "assistant" and t.response_id in reply_ids and t.phase != "commentary"]
        if replies:
            row.reply_text = replies[-1].text
            row.reply_lang = lang_tag(row.reply_text)
        rows.append(row)
    return rows


def connect_stages(spans: list[Span]) -> dict[str, float]:
    page = next((s.t_ms for s in spans if s.name == "page_load"), None)
    if page is None:
        return {}
    out = {}
    for name in CONNECT_STAGES:
        s = next((x for x in spans if x.name == name), None)
        if s:
            out[name] = s.t_ms - page
    rtt = next((x for x in spans if x.name == "rtt"), None)
    if rtt:
        out["rtt"] = _attrs(rtt).get("ms")
    sdp = next((x for x in spans if x.name == "sdp_exchange"), None)
    if sdp:
        out["sdp_ms"] = _attrs(sdp).get("ms")
    secret = next((x for x in spans if x.name == "secret_fetched"), None)
    if secret:
        out["secret_source"] = _attrs(secret).get("source")
    return out


def stats(values: list[float]) -> dict[str, float | int | None]:
    vals = [v for v in values if v is not None]
    return {"n": len(vals), "p50": percentile(vals, 50), "p95": percentile(vals, 95)}


def summarize(rows: list[LineRow], connects: list[dict]) -> dict[str, Any]:
    plain = [r for r in rows if r.expected == "plain" and r.observed == "plain"]
    tool = [r for r in rows if r.expected == "tool" and r.observed == "tool"]
    voiced = [r for r in rows if r.problem != "no end-of-turn signal"]
    paused = [r for r in rows if r.pause_ms >= 300]
    unpaused = [r for r in rows if r.pause_ms < 300]
    replies = [ms for r in rows for ms in r.reply_ms]
    tokens = [(t, ms) for r in rows for t, ms in zip(r.reply_audio_tokens, r.reply_ms)]

    def lang_count(lang: str, ok: set[str]) -> dict[str, int]:
        lines = [r for r in rows if r.lang == lang and r.reply_lang]
        return {"lines": len(lines), "matching": sum(1 for r in lines if r.reply_lang in ok)}

    out: dict[str, Any] = {
        "lines": len(rows),
        "expected": {"plain": sum(r.expected == "plain" for r in rows), "tool": sum(r.expected == "tool" for r in rows)},
        "tool_choice_correct": sum(r.observed == r.expected for r in rows),
        "no_turn_signal": sum(r.problem == "no end-of-turn signal" for r in rows),
        "timed_out": sum(r.timed_out for r in rows),
        "plain": {
            "end_to_server": stats([r.end_to_server for r in plain]),
            "server_to_audio": stats([r.server_to_audio for r in plain]),
            "end_to_audible": stats([r.end_to_audible for r in plain]),
        },
        "tool": {
            "end_to_server": stats([r.end_to_server for r in tool]),
            "end_to_preamble": stats([r.end_to_preamble for r in tool]),
            "end_to_value": stats([r.end_to_value for r in tool]),
            "tool_ms": stats([r.tool_ms for r in tool]),
            "result_to_created": stats([r.result_to_created for r in tool]),
            "model_spoke_before_result": sum(1 for r in tool if r.model_spoke_before_result),
            "local_clip_lang_match": sum(
                1 for r in tool if r.local_clip_lang and r.local_clip_lang == ("hi" if r.lang in ("hi", "hinglish") else "en")
            ),
            "local_clips": sum(1 for r in tool if r.local_clip_lang),
        },
        "false_cutoffs": {
            "all": [sum(r.false_cutoff for r in voiced), len(voiced)],
            "paused_lines": [sum(r.false_cutoff for r in paused), len(paused)],
            "other_lines": [sum(r.false_cutoff for r in unpaused), len(unpaused)],
        },
        "replies": {
            "n": len(replies),
            "over_6s": sum(1 for ms in replies if ms > LONG_REPLY_MS),
            "p50_ms": percentile(replies, 50),
            "p95_ms": percentile(replies, 95),
            "truncated_by_cap": sum(r.truncated for r in rows),
            "audio_tokens_per_s": (sum(t for t, _ in tokens) / (sum(ms for _, ms in tokens) / 1000)) if tokens else None,
        },
        "language": {
            "hi_lines_hindi_reply": lang_count("hi", {"hi", "hinglish"}),
            "hinglish_lines_hindi_reply": lang_count("hinglish", {"hi", "hinglish"}),
            "en_lines_english_reply": lang_count("en", {"en"}),
        },
        "connect": {
            k: stats([c.get(k) for c in connects if isinstance(c.get(k), (int, float))])
            for k in (*CONNECT_STAGES, "sdp_ms", "rtt")
        },
        "secret_sources": [c.get("secret_source") for c in connects],
    }
    return out


def analyse_run(engine: Engine, run_id: str) -> dict[str, Any]:
    spans = load_spans(engine, run_id)
    transcripts = load_transcripts(engine, run_id)
    sessions = load_sessions(engine, run_id)
    by_session: dict[str, list[Span]] = defaultdict(list)
    for s in spans:
        by_session[s.session_id].append(s)
    rows, connects = [], []
    for sid, ss in by_session.items():
        rows += analyse_session(ss, [t for t in transcripts if t.session_id == sid])
        connects.append(connect_stages(ss))
    meta = sessions[0] if sessions else None
    return {
        "run_id": run_id,
        "model": meta.model if meta else None,
        "turn_mode": meta.turn_mode if meta else None,
        "prompt": meta.prompt if meta else None,
        "max_output_tokens": meta.max_output_tokens if meta else None,
        "sessions": len(by_session),
        "summary": summarize(rows, connects),
        "rows": [r.__dict__ for r in rows],
        "connects": connects,
    }


def fmt(s: dict[str, Any]) -> str:
    if not s or not s.get("n"):
        return "n=0"
    return f"{s['p50']:.0f} / {s['p95']:.0f} (n={s['n']})"
