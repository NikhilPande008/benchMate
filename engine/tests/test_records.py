"""Repair records: the record written at close-out, the repair report and the trainee assessment
(HTML, PDF through headless Chromium), the records API, and recall over past records. The
sessions are the scripted technician on the fake bench."""
from __future__ import annotations

import asyncio
import json

import pytest

from engine.records import ExportUnavailable, assessment_html, load_record, recall, report_html, to_pdf
from engine.session.repair import RepairSession
from engine.telemetry import TelemetryStore
from eval.scenarios.engine_run import Technician
from mcp_evidence.fake_bench import FakeBench


def run_closed(store: TelemetryStore, scenario: str = "short_battery_line", note: str | None = None) -> str:
    """A whole repair to close-out, its rows written to the store's database."""
    rows: list[tuple[str, dict]] = []
    tech = Technician(scenario)
    tech.repair.persist = lambda table, row: rows.append((table, row))
    res = asyncio.run(tech.run())
    if note:
        asyncio.run(tech.repair.close_repair("unresolved", note=note))
    for table, row in rows:
        store.record_repair(table, row)
    batch = []
    while not store._queue.empty():
        batch.append(store._queue.get_nowait())
    store._write(batch)
    rec = next(r for t, r in reversed(rows) if t == "repair_records")
    assert res.outcome in ("fixed", "unresolved")
    return rec["id"]


@pytest.fixture
def store(tmp_path):
    return TelemetryStore(tmp_path / "rec.sqlite")


def test_close_out_writes_what_the_documents_need(store):
    rid = run_closed(store)
    v = load_record(store.engine, rid)
    r = v.record
    assert r["outcome"] == "fixed" and r["fault_id"] == "vbat_cap_short" and r["fix_check_passed"] is True
    assert r["steps"] and all("measurement" in s for s in r["steps"])
    assert r["safety"]["approved"] >= 1 and r["safety"]["violations"] == [] and r["battery"] == "removed"
    assert r["ranking"][0][0] == "vbat_cap_short" and r["closed_ms"] >= r["started_ms"]
    assert len(v.evidence) == len(r["evidence"]) and v.simulated


def test_the_repair_report(store):
    v = load_record(store.engine, run_closed(store))
    page = report_html(v)
    assert "Repair report" in page and "Demo phone" in page and "FIXED" in page
    assert v.fault_name("vbat_cap_short") in page and v.fault("vbat_cap_short").fix.en in page and "PASSED" in page
    assert page.count("<tr><td>") >= len(v.evidence) and "Simulated readings" in page
    assert f"approved by the safety gate: <b>{v.record['safety']['approved']}</b>" in page
    assert "the voice assistant never ranks faults" in page and "<script" not in page


def test_the_trainee_assessment(store):
    v = load_record(store.engine, run_closed(store))
    page = assessment_html(v)
    assert "Trainee assessment" in page and "Trainee:" in page and "Mentor's comments" in page
    steps = [s for s in v.record["steps"] if s.get("measurement")]
    assert page.count('<span class="word ok">DONE</span>') + page.count("NOT DONE</span>") == len(steps)
    assert page.count('<span class="word ok">YES</span>') == 4  # a clean run: every good-practice check holds
    assert " / " in page


def test_user_text_is_escaped(store):
    rid = run_closed(store, note="<script>alert(1)</script> & co")
    page = report_html(load_record(store.engine, rid))
    assert "<script>" not in page and "&lt;script&gt;" in page and "&amp; co" in page


def test_pdf_through_headless_chromium(store):
    v = load_record(store.engine, run_closed(store))
    try:
        pdf = to_pdf(report_html(v))
    except ExportUnavailable as exc:
        pytest.skip(str(exc))
    assert pdf[:5] == b"%PDF-" and len(pdf) > 5000


def test_recall_by_model_and_symptom(store):
    a = run_closed(store, "short_battery_line")
    run_closed(store, "no_current")
    run_closed(store, "not_charging")
    rc = recall(store.engine, "demo_model", "dead")
    assert rc["n"] == 2 and rc["fixed"] == 2 and a in {i["record"] for i in rc["latest"]}
    assert rc["top_fixed_fault"] in ("vbat_cap_short", "open_vbat_path")
    assert recall(store.engine, "demo_model", "not_charging")["n"] == 1
    assert recall(store.engine, "general_dead_phone", "dead")["n"] == 0


def test_start_repair_mentions_past_repairs():
    async def go(rc):
        async def hook(pack_id, symptom):
            assert (pack_id, symptom) == ("demo_model", "dead")
            return rc

        r = RepairSession("se_rc", FakeBench(time_scale=0), recall=hook)
        return await r.start_repair("demo", "phone is dead", "removed")

    out = asyncio.run(go({"n": 3, "fixed": 2, "top_fixed_fault": "vbat_cap_short", "latest": []}))
    assert out["recall"] == {"n": 3, "fixed": 2, "top": "vbat_cap_short"} and len(json.dumps(out)) < 320
    assert "recall" not in asyncio.run(go({"n": 0, "fixed": 0, "top_fixed_fault": None, "latest": []}))


def test_the_records_api(client, settings):
    rid = run_closed(TelemetryStore(settings.db_path))
    assert client.get(f"/records/{rid}").json()["id"] == rid
    r = client.get(f"/records/{rid}/report.html")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "Repair report" in r.text
    assert "Trainee assessment" in client.get(f"/records/{rid}/assessment.html").text
    pdf = client.get(f"/records/{rid}/assessment.pdf")
    assert pdf.status_code in (200, 503)
    if pdf.status_code == 200:
        assert pdf.content[:5] == b"%PDF-" and pdf.headers["content-type"] == "application/pdf"
    assert client.get("/records/rr_0000000000/report.html").status_code == 404
    assert client.get("/records/../etc/report.html").status_code in (400, 404)
    assert client.get(f"/records/{rid}/other.html").status_code == 404
    assert client.get("/records?pack=demo_model&symptom=dead").json()["n"] == 1
