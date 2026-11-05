"""Repair records (local only, like the rest of the engine).

GET /records?pack=<pack id>&symptom=<symptom>      recall: past records of this model and symptom
GET /records/{id}                                  the record as stored at close-out
GET /records/{id}/report.html | report.pdf         repair report
GET /records/{id}/assessment.html | assessment.pdf trainee assessment sheet
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from engine.records import ExportUnavailable, assessment_html, load_record, recall, report_html, to_pdf

router = APIRouter(prefix="/records")
RECORD_RE = re.compile(r"^rr_[0-9a-f]{6,40}$")
DOCS = {"report": report_html, "assessment": assessment_html}


async def _view(request: Request, record_id: str):
    if not RECORD_RE.match(record_id):
        raise HTTPException(400, "bad record id")
    telemetry = request.app.state.telemetry
    await telemetry.flush()
    v = await asyncio.to_thread(load_record, telemetry.engine, record_id)
    if v is None:
        raise HTTPException(404, f"no record {record_id}")
    return v


@router.get("")
async def list_records(request: Request, pack: str, symptom: str | None = None) -> dict[str, Any]:
    telemetry = request.app.state.telemetry
    await telemetry.flush()
    return await asyncio.to_thread(recall, telemetry.engine, pack, symptom)


@router.get("/{record_id}")
async def get_record(record_id: str, request: Request) -> dict[str, Any]:
    v = await _view(request, record_id)
    return v.record


@router.get("/{record_id}/{doc}.{fmt}")
async def document(record_id: str, doc: str, fmt: str, request: Request) -> Response:
    if doc not in DOCS or fmt not in ("html", "pdf"):
        raise HTTPException(404, "report or assessment, as html or pdf")
    v = await _view(request, record_id)
    page = DOCS[doc](v)
    if fmt == "html":
        return Response(page, media_type="text/html; charset=utf-8")
    try:
        pdf = await asyncio.to_thread(to_pdf, page)
    except ExportUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="benchmate-{doc}-{record_id}.pdf"'})
