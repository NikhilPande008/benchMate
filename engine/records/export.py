"""Repair report and trainee assessment sheet from a closed repair session, and recall.

Both documents are built from what the engine logged (the record written at close-out, the
evidence rows, the pack's labels), never from the voice model's words. HTML first; PDF through
headless Chromium (Playwright's, already on the dev machine). Without it, the HTML still works.
No personal data: there is no customer field; the trainee's name is a blank line on the sheet.
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session, select

from engine.telemetry.store import EvidenceRow, RepairRecordRow
from mcp_evidence.packs import load_pack
from mcp_evidence.packs.schema import Pack

OUTCOME_WORDS = {"fixed": "Fixed", "unresolved": "Unresolved", "referred": "Referred", "abandoned": "Stopped"}
CLASS_WORDS = {"normal": "NORMAL", "short": "SHORT", "low": "LOW", "high": "HIGH", "open": "OPEN", "ok": "OK", "faulty": "FAULTY",
               "none": "NOTHING HOT", "cap_hot": "CAPACITOR HOT", "ic_hot": "IC HOT", "NORMAL_BOOT": "NORMAL BOOT",
               "SHORT_IMMEDIATE": "SHORT AT CONNECT", "NO_CURRENT": "NO CURRENT", "RISE_THEN_DROP": "RISES THEN DROPS", "AT_LIMIT": "AT LIMIT"}
BAD = {"short", "open", "faulty", "cap_hot", "ic_hot", "SHORT_IMMEDIATE", "NO_CURRENT", "AT_LIMIT"}
DONE, NOT_DONE = '<span class="word ok">DONE</span>', '<span class="word warn">NOT DONE</span>'
YES, NO = '<span class="word ok">YES</span>', '<span class="word bad">NO</span>'
STEP_WORDS = {"connect_boot_cable": "connect the boot cable", "press_power_key": "press the power key", "connect_port_supply": "connect the supply to the port"}


class ExportUnavailable(RuntimeError):
    """No headless Chromium for the PDF (the HTML is still available)."""


@dataclass
class RecordView:
    record: dict[str, Any]
    evidence: list[dict[str, Any]]
    pack: Pack
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def simulated(self) -> bool:
        return any(e.get("simulated") for e in self.evidence)

    def fault_name(self, fid: str | None) -> str:
        if not fid or fid == "other":
            return "Unknown fault (none of the pack's faults)"
        f = next((f for f in self.pack.faults if f.id == fid), None)
        return f.name.en if f else fid

    def fault(self, fid: str | None):
        return next((f for f in self.pack.faults if f.id == fid), None)

    def label(self, mid: str | None) -> str:
        if not mid:
            return "—"
        try:
            m = self.pack.measurement(mid)
        except (KeyError, StopIteration):
            return mid
        if m.point_id:
            p = self.pack.point(m.point_id)
            return f"{p.label.en} ({p.mode})"
        return m.label.en


def load_record(db: Engine, record_id: str) -> RecordView | None:
    with Session(db) as s:
        row = s.get(RepairRecordRow, record_id)
        if row is None:
            return None
        rec = json.loads(row.data)
        ev = [json.loads(r.data) | {"ledger_use": r.ledger_use} for r in
              s.exec(select(EvidenceRow).where(EvidenceRow.session_id == row.session_id).order_by(EvidenceRow.seq))]  # type: ignore[arg-type]
    return RecordView(rec, ev, load_pack(rec["pack_id"]))


def recall(db: Engine, pack_id: str, symptom: str | None, limit: int = 50) -> dict[str, Any]:
    """Past records for this phone model and symptom (session_recall, ARCHITECTURE.md §5.1): how
    many, how many were fixed, the most common confirmed fault, the latest few."""
    with Session(db) as s:
        rows = list(s.exec(select(RepairRecordRow).order_by(RepairRecordRow.t_ms.desc())))  # type: ignore[attr-defined]
    items = []
    for r in rows:
        d = json.loads(r.data)
        if d.get("pack_id") != pack_id or (symptom and d.get("symptom") != symptom):
            continue
        items.append({"record": r.id, "outcome": r.outcome, "fault": r.fault_id, "t_ms": r.t_ms, "fix_check_passed": d.get("fix_check_passed")})
        if len(items) >= limit:
            break
    fixed = [i for i in items if i["outcome"] == "fixed"]
    counts: dict[str, int] = {}
    for i in fixed:
        if i["fault"]:
            counts[i["fault"]] = counts.get(i["fault"], 0) + 1
    top = max(counts, key=lambda k: counts[k]) if counts else None
    return {"n": len(items), "fixed": len(fixed), "top_fixed_fault": top, "latest": items[:5]}


# --- rendering ------------------------------------------------------------------------------------

CSS = """
@page { size: A4; margin: 16mm 14mm; }
body { font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif; color: #141413; font-size: 10.5pt; line-height: 1.4; }
h1 { font-size: 18pt; margin: 0 0 2mm; } h2 { font-size: 12pt; margin: 6mm 0 2mm; border-bottom: 1px solid #ccc; padding-bottom: 1mm; }
.meta { color: #555; } .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 2mm 8mm; }
table { border-collapse: collapse; width: 100%; } th, td { text-align: left; padding: 1.4mm 2mm; border-bottom: 1px solid #e3e3df; vertical-align: top; }
th { font-size: 9pt; color: #555; text-transform: uppercase; letter-spacing: .04em; }
.word { font-weight: 800; padding: 0 1.5mm; border-radius: 1mm; } .ok { color: #17702f; background: #dff3e3; } .bad { color: #b3261e; background: #fde3e0; }
.warn { color: #8a5300; background: #fff0d1; } .info { color: #1557c0; background: #e3ecfb; }
.bar { height: 3mm; background: #e3e3df; border-radius: 1.5mm; } .bar div { height: 100%; background: #1557c0; border-radius: 1.5mm; }
.box { border: 1px solid #ccc; border-radius: 2mm; padding: 3mm; } .note { color: #8a5300; } .sig { margin-top: 10mm; display: grid; grid-template-columns: 1fr 1fr; gap: 12mm; }
.sig div { border-top: 1px solid #141413; padding-top: 1mm; color: #555; } .score { font-size: 16pt; font-weight: 800; }
"""


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _when(ms: float | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def _value(e: dict[str, Any]) -> str:
    if e.get("kind") == "supply":
        f = e.get("fields") or {}
        v, lim, live = (f.get(k, {}).get("value") for k in ("set_voltage", "current_limit", "live_current"))
        return f"{v if v is not None else '—'} V, limit {lim if lim is not None else '—'} A, live {live if live is not None else '—'} A"
    if e.get("kind") == "boot_trace":
        t = e.get("trace") or {}
        feats = t.get("features") or {}
        return f"peak {feats.get('i_peak', '—')} A over {len(t.get('samples', []))} samples"
    if e.get("open"):
        return "OL (open)"
    if e.get("value") is None:
        return "—"
    unit = {"diode_V": "V", "ohm": "Ω"}.get(e.get("unit") or "", e.get("unit") or "")
    return f"{e['value']} {unit}".strip()


def _word(cls: str | None) -> str:
    if not cls:
        return ""
    tone = "bad" if cls in BAD else ("warn" if cls in ("low", "high", "RISE_THEN_DROP") else "ok")
    return f'<span class="word {tone}">{_e(CLASS_WORDS.get(cls, cls.upper()))}</span>'


def _head(v: RecordView, title: str) -> str:
    r = v.record
    sim = '<p class="note"><b>Simulated readings:</b> this session ran on the fake bench (no real instrument).</p>' if v.simulated else ""
    placeholder = '<p class="note">The model pack still holds placeholder values (reference board not yet measured).</p>' if v.pack.placeholder else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{_e(title)} {_e(r['id'])}</title><style>{CSS}</style></head><body>
<h1>{_e(title)}</h1>
<p class="meta">Record {_e(r['id'])} · session {_e(r['session_id'])} · generated {_e(v.generated_at)} by Benchmate</p>
{sim}{placeholder}
<div class="grid box">
<div><b>Phone</b><br>{_e(v.pack.name.en)}{'' if r.get('model_data', True) else ' (general dead-phone flow: no model data)'}</div>
<div><b>Symptom</b><br>{_e(r.get('symptom') or '—')}</div>
<div><b>Started</b><br>{_when(r.get('started_ms'))}</div>
<div><b>Closed</b><br>{_when(r.get('closed_ms'))} ({_duration(r)})</div>
<div><b>Outcome</b><br><span class="word {'ok' if r['outcome'] == 'fixed' else 'warn'}">{_e(OUTCOME_WORDS.get(r['outcome'], r['outcome']).upper())}</span></div>
<div><b>Diagnosed fault</b><br>{_e(v.fault_name(r.get('fault_id')))}</div>
</div>"""


def _duration(r: dict[str, Any]) -> str:
    a, b = r.get("started_ms"), r.get("closed_ms")
    if not a or not b:
        return "—"
    m = round((b - a) / 60000)
    return f"{m} min" if m >= 1 else "under a minute"


def _evidence_table(v: RecordView) -> str:
    rows = []
    for e in v.evidence:
        label = "Supply display" if e.get("kind") == "supply" else ("Boot current" if e.get("kind") == "boot_trace" else v.label(e.get("measurement_id")))
        cls = e.get("outcome") or ((e.get("trace") or {}).get("pattern") if e.get("kind") == "boot_trace" else None)
        src = "Camera" if e.get("source") == "camera" else "Spoken"
        conf = f"{round(float(e.get('confidence') or 0) * 100)}%"
        flags = ", ".join(x for x in ("confirmed" if e.get("confirmed") else "", "not used" if e.get("ledger_use") is None and e.get("kind") not in ("supply",) else "",
                                      "after the fix" if e.get("ledger_use") in ("fix_check", "after_fix") else "") if x)
        rows.append(f"<tr><td>{_when(_ts(e))[11:]}</td><td>{_e(label)}</td><td>{_e(_value(e))}</td><td>{_word(cls)}</td><td>{src}</td><td>{conf}</td><td>{_e(flags)}</td></tr>")
    return ("<table><tr><th>Time</th><th>Reading</th><th>Value</th><th>Class</th><th>Source</th><th>Conf.</th><th></th></tr>"
            + "".join(rows) + "</table>") if rows else "<p>No readings.</p>"


def _ts(e: dict[str, Any]) -> float | None:
    c = e.get("created_at")
    if not c:
        return None
    try:
        return datetime.fromisoformat(c).timestamp() * 1000
    except ValueError:
        return None


def report_html(v: RecordView) -> str:
    r = v.record
    f = v.fault(r.get("fault_id"))
    fix = f.fix.en if f is not None and getattr(f, "fix", None) else "—"
    check = r.get("fix_check")
    passed = r.get("fix_check_passed")
    check_line = ("—" if not check else f"{_e(v.label(check))}: " + ('<span class="word ok">PASSED</span>' if passed else
                  '<span class="word bad">NOT PASSED</span>' if passed is False else '<span class="word warn">NOT MEASURED</span>'))
    ranking = "".join(
        f"<tr><td>{_e(v.fault_name(h))}</td><td style='width:45%'><div class='bar'><div style='width:{max(1, p * 100):.0f}%'></div></div></td><td>{round(p * 100)}%</td></tr>"
        for i, (h, p) in enumerate((r.get("ranking") or [])[:6]) if i < 3 or p >= 0.01)
    s = r.get("safety") or {}
    safety = (f"<p>Power steps approved by the safety gate: <b>{s.get('approved', 0)}</b>; blocked: <b>{len(s.get('blocked', []))}</b>"
              + (" (" + ", ".join(_e(b['reason']) for b in s.get("blocked", [])) + ")" if s.get("blocked") else "")
              + f". Power instructions stopped by the guardrail: <b>{len(s.get('violations', []))}</b>."
              + (f" Disconnect-now alerts: <b>{s['disconnect_now']}</b>." if s.get("disconnect_now") else "")
              + (" Safety stop: " + _e("; ".join(s["stops"])) + "." if s.get("stops") else "") + "</p>")
    note = f"<h2>Note</h2><p>{_e(r.get('note'))}</p>" if r.get("note") else ""
    return _head(v, "Repair report") + f"""
<h2>Result</h2>
<div class="box"><p><b>Fault:</b> {_e(v.fault_name(r.get('fault_id')))}</p><p><b>Fix:</b> {_e(fix)}</p><p><b>Confirming reading:</b> {check_line}</p></div>
<h2>Readings</h2>{_evidence_table(v)}
<h2>Final fault ranking (the engine's ledger)</h2><table>{ranking}</table>
<h2>Safety</h2>{safety}{note}
<p class="meta">The fault ranking comes from the engine's ledger (Bayes update over the pack's faults); the voice assistant never ranks faults.</p>
</body></html>"""


def assessment_html(v: RecordView) -> str:
    """For a mentor reviewing a trainee: did they follow the suggested steps, read values back,
    keep to the safety gate, and confirm the fix."""
    r = v.record
    steps = r.get("steps") or []
    measured = [e.get("measurement_id") for e in v.evidence if e.get("measurement_id")]
    rows, followed, asked = [], 0, 0
    for i, st in enumerate(steps):
        mid = st.get("measurement")
        if not mid:
            continue
        asked += 1
        done = mid in measured
        followed += done
        power = f" (needs power: {STEP_WORDS.get(st.get('power_step'), st.get('power_step'))})" if st.get("power_step") else ""
        mark = DONE if done else NOT_DONE
        rows.append(f"<tr><td>{i + 1}</td><td>{_e(v.label(mid))}{_e(power)}</td><td>{mark}</td></tr>")
    spoken = [e for e in v.evidence if e.get("source") == "spoken" and e.get("kind") != "observation"]
    confirmed = sum(1 for e in spoken if e.get("confirmed"))
    s = r.get("safety") or {}
    viol = len(s.get("violations", []))
    checks = [
        ("Battery removed before any power step", r.get("battery") == "removed"),
        ("Every power step went through the safety gate (no guardrail stops)", viol == 0),
        ("Spoken readings confirmed by read-back", not spoken or confirmed == len(spoken)),
        ("Fix confirmed by a healthy reading", bool(r.get("fix_check_passed"))),
    ]
    score = followed + sum(ok for _, ok in checks)
    out_of = asked + len(checks)
    check_rows = "".join(f"<tr><td>{_e(t)}</td><td>{YES if ok else NO}</td></tr>" for t, ok in checks)
    return _head(v, "Trainee assessment") + f"""
<p><b>Trainee:</b> ______________________ &nbsp; <b>Mentor:</b> ______________________ &nbsp; <b>Level:</b> {_e(r.get('level') or 'trainee')}</p>
<h2>Procedure: the steps Benchmate suggested</h2>
<table><tr><th>#</th><th>Suggested measurement</th><th>Done</th></tr>{''.join(rows) or '<tr><td colspan=3>No steps were proposed.</td></tr>'}</table>
<p>Followed {followed} of {asked} suggested steps.</p>
<h2>Good practice</h2><table>{check_rows}</table>
<h2>Readings taken</h2>{_evidence_table(v)}
<h2>Score</h2><p class="score">{score} / {out_of}</p>
<p class="meta">A guide for the mentor, computed from the session log: steps followed, safety and confirmation practice. It does not judge the trainee's hand skills.</p>
<h2>Mentor's comments</h2><div class="box" style="height:30mm"></div>
<div class="sig"><div>Trainee signature</div><div>Mentor signature</div></div>
</body></html>"""


def to_pdf(page_html: str) -> bytes:
    """HTML → PDF in headless Chromium (call from a worker thread)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - playwright is a dev dependency
        raise ExportUnavailable("Playwright is not installed: the HTML version is available") from exc
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content(page_html, wait_until="load")
                return page.pdf(format="A4", print_background=True, margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
            finally:
                browser.close()
    except Exception as exc:
        raise ExportUnavailable(f"headless Chromium failed: {exc}") from exc
