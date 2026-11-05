"""Companion screen: images and calibration (ARCHITECTURE.md §8). Local only (the engine binds to
127.0.0.1); the API key is never involved.

GET  /screen/frame/{camera}.jpg?session=      latest frame: "overhead" (vision mode) or "microscope"
GET  /screen/board/{session}/{side}.jpg      the reference board for the session's pack (synthetic stand-in)
GET  /screen/board-point?session=            the next point on the reference and on the live microscope frame
GET  /screen/evidence/{session}/{id}.jpg     the frame a reading came from
GET  /screen/calibration?session=            the overhead camera's calibration (corners per display and row)
POST /screen/calibration?session=            {"rois":[{"name","corners"}]}: save it (and the reference frame)
POST /screen/preview?session=                {"name","corners","digits"}: the warped crop and Reader B's reading
"""
from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from mcp_evidence.vision.bench import VisionBench
from mcp_evidence.vision.board import BOARD_SIZE, Microscope, align, map_point, reference_board
from mcp_evidence.vision.calibration import CalibrationStore, Roi
from mcp_evidence.vision.reader_b import read_field

router = APIRouter(prefix="/screen")
SESSION_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_microscopes: dict[str, Microscope] = {}


def _jpeg(img: np.ndarray, quality: int = 82) -> Response:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise HTTPException(500, "encode failed")
    return Response(buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


def _conn(request: Request, session: str) -> Any:
    if not SESSION_RE.match(session):
        raise HTTPException(400, "bad session id")
    conn = request.app.state.connections.get(session)
    if conn is None:
        raise HTTPException(404, f"no session {session!r} on this engine")
    return conn


def _vision(conn: Any) -> VisionBench:
    if not isinstance(conn.bench, VisionBench):
        raise HTTPException(409, "no camera in this session (BENCHMATE_EVIDENCE=fake)")
    return conn.bench


def _overhead(vb: VisionBench) -> np.ndarray:
    assert vb.camera is not None
    f = vb.camera.ring.latest() if vb.camera._task and not vb.camera._task.done() else vb.camera.grab_once()
    if f is None:
        raise HTTPException(503, "overhead camera not responding")
    return f.image


def _step_point(conn: Any):
    r = conn.repair
    st = r.pending_step
    if not (r.pack and st):
        return None
    m = r.pack.measurement(st.measurement_id)
    if not m.point_id:
        return None
    p = r.pack.point(m.point_id)
    return p if p.xy else None


def _microscope_frame(conn: Any) -> tuple[np.ndarray, np.ndarray, Any]:
    r = conn.repair
    if r.pack is None:
        raise HTTPException(409, "no repair started")
    p = _step_point(conn)
    side = p.side if p else "top"
    ref = reference_board(r.pack, side)
    m = _microscopes.setdefault(conn.hello.session_id, Microscope())
    img = m.frame(ref, p.xy if p else None, p.xy if p else None)
    return img, ref, p


@router.get("/frame/{camera}.jpg")
async def frame(camera: str, session: str, request: Request) -> Response:
    conn = _conn(request, session)
    if camera == "overhead":
        return _jpeg(await asyncio.to_thread(_overhead, _vision(conn)))
    if camera == "microscope":
        img, _, _ = await asyncio.to_thread(_microscope_frame, conn)
        return _jpeg(img)
    raise HTTPException(404, "camera must be overhead or microscope")


@router.get("/board/{session}/{side}.jpg")
async def board(session: str, side: str, request: Request) -> Response:
    conn = _conn(request, session)
    if side not in ("top", "bottom") or conn.repair.pack is None:
        raise HTTPException(404, "no board")
    return _jpeg(await asyncio.to_thread(reference_board, conn.repair.pack, side), 88)


@router.get("/board-point")
async def board_point(session: str, request: Request) -> dict[str, Any]:
    conn = _conn(request, session)
    img, ref, p = await asyncio.to_thread(_microscope_frame, conn)
    if p is None:
        return {"point": None, "ref_size": list(BOARD_SIZE)}
    H = await asyncio.to_thread(align, ref, img)
    live = map_point(H, p.xy) if H is not None else None
    return {"point": p.id, "side": p.side, "ref": list(p.xy), "ref_size": list(BOARD_SIZE),
            "live": [round(live[0], 1), round(live[1], 1)] if live else None, "live_size": [img.shape[1], img.shape[0]],
            "aligned": H is not None}


@router.get("/evidence/{session}/{evidence_id}.jpg")
async def evidence_frame(session: str, evidence_id: str, request: Request) -> Response:
    conn = _conn(request, session)
    rec = next((e for e in conn.repair.evidence if e.id == evidence_id), None)
    if rec is None or not rec.frame_paths:
        raise HTTPException(404, "no frame for that reading")
    path = Path(rec.frame_paths[0])  # only paths the engine itself wrote
    if not path.is_file():
        raise HTTPException(404, "frame file is gone")
    return Response(path.read_bytes(), media_type="image/jpeg")


@router.get("/calibration")
async def get_calibration(session: str, request: Request) -> dict[str, Any]:
    vb = _vision(_conn(request, session))
    img = await asyncio.to_thread(_overhead, vb)
    assert vb.calibration is not None
    return {"camera": vb.calibration.camera, "size": [img.shape[1], img.shape[0]], "created_at": vb.calibration.created_at,
            "rois": [{"name": r.name, "corners": [[round(float(x), 1), round(float(y), 1)] for x, y in r.corners], "digits": r.digits, "kind": r.kind}
                     for r in vb.calibration.rois.values()]}


class RoiIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    corners: list[list[float]] = Field(min_length=4, max_length=4)
    digits: int = Field(default=4, ge=1, le=8)


class CalibrationIn(BaseModel):
    rois: list[RoiIn] = Field(min_length=1, max_length=16)


@router.post("/calibration")
async def save_calibration(session: str, body: CalibrationIn, request: Request) -> dict[str, Any]:
    vb = _vision(_conn(request, session))
    cal = vb.calibration
    assert cal is not None
    for r in body.rois:
        if r.name not in cal.rois:
            raise HTTPException(400, f"unknown region {r.name!r}")
        old = cal.rois[r.name]
        cal.rois[r.name] = Roi(r.name, np.array(r.corners, np.float32), r.digits, old.kind)
    img = await asyncio.to_thread(_overhead, vb)
    cal.set_reference(img)  # the moved check compares against the view at calibration
    vb.health.camera(True)
    store = CalibrationStore(request.app.state.settings.db_path.parent / "calibration")
    path = await asyncio.to_thread(store.save, cal)
    return {"saved": str(path.name), "rois": len(cal.rois)}


class PreviewIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    corners: list[list[float]] = Field(min_length=4, max_length=4)
    digits: int = Field(default=4, ge=1, le=8)


@router.post("/preview")
async def preview(session: str, body: PreviewIn, request: Request) -> dict[str, Any]:
    vb = _vision(_conn(request, session))
    img = await asyncio.to_thread(_overhead, vb)
    kind = "field" if "." in body.name else "display"
    roi = Roi(body.name, np.array(body.corners, np.float32), body.digits, kind)
    crop = await asyncio.to_thread(roi.warp, img)
    out: dict[str, Any] = {"name": body.name}
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
    out["image"] = "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode() if ok else None
    if kind == "field":
        r = await asyncio.to_thread(read_field, crop, body.digits)
        out["reading"] = {"text": r.key, "legible": r.legible, "conf": round(r.confidence, 3), "reason": r.reason}
    return out
