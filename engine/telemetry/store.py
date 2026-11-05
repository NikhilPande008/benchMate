"""Per-turn latency spans, stored in SQLite.

Writes never block the event loop: `record()` only enqueues; one background task drains the
queue and writes batches on a worker thread.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field as PField
from sqlalchemy import Engine, inspect
from sqlmodel import Field, Session, SQLModel, create_engine, select

from .lang import lang_tag


class Span(SQLModel, table=True):
    __tablename__ = "spans"

    id: int | None = Field(default=None, primary_key=True)
    run_id: str | None = Field(default=None, index=True)
    session_id: str = Field(index=True)
    turn: int = 0
    name: str = Field(index=True)
    t_ms: float  # epoch milliseconds (browser and engine share the laptop's clock)
    source: str  # "browser" | "engine"
    attrs: str | None = None  # JSON


class RealtimeSessionRow(SQLModel, table=True):
    __tablename__ = "realtime_sessions"

    id: str = Field(primary_key=True)
    run_id: str | None = Field(default=None, index=True)
    model: str
    prompt_version: str
    created_ms: float
    turn_mode: str | None = None
    prompt: str | None = None
    max_output_tokens: int | None = None
    secret_source: str | None = None  # "pool" | "fresh"


class Transcript(SQLModel, table=True):
    __tablename__ = "transcripts"

    id: int | None = Field(default=None, primary_key=True)
    run_id: str | None = Field(default=None, index=True)
    session_id: str = Field(index=True)
    turn: int = 0
    role: str  # "user" | "assistant"
    item_id: str | None = None
    response_id: str | None = None
    phase: str | None = None  # assistant only: "commentary" | "final_answer"
    text: str
    lang_api: str | None = None  # user only: codes from gpt-transcribe, comma-separated
    lang_tag: str  # engine.telemetry.lang.lang_tag(text)
    t_ms: float


class TranscriptIn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = PField(max_length=4000)
    t_ms: float
    turn: int = 0
    item_id: str | None = PField(default=None, max_length=128)
    response_id: str | None = PField(default=None, max_length=128)
    phase: str | None = PField(default=None, max_length=32)
    languages: list[str] | None = PField(default=None, max_length=8)


class EvidenceRow(SQLModel, table=True):
    __tablename__ = "evidence"

    id: str = Field(primary_key=True)
    session_id: str = Field(index=True)
    run_id: str | None = Field(default=None, index=True)
    seq: int = 0  # arrival order within the repair session
    # how the ledger used it: "ledger" | "remeasure" | "fix_check" | "after_fix" | None (log only)
    ledger_use: str | None = None
    kind: str
    measurement_id: str | None = None
    outcome: str | None = None
    confidence: float
    source: str
    simulated: bool = False
    data: str  # the full EvidenceRecord as JSON
    t_ms: float


class LedgerSnapshotRow(SQLModel, table=True):
    __tablename__ = "ledger_snapshots"

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    run_id: str | None = Field(default=None, index=True)
    index: int
    evidence_id: str
    after: str  # JSON {fault: p}
    deltas: str  # JSON {fault: change}
    stop: bool
    conflict: str | None = None
    t_ms: float


class RepairStateRow(SQLModel, table=True):
    """The repair session's state, overwritten as it changes; with the evidence it rebuilds the
    session after a restart. Power approvals and pending read-backs are never stored."""

    __tablename__ = "repair_state"

    session_id: str = Field(primary_key=True)
    run_id: str | None = Field(default=None, index=True)
    data: str  # JSON, RepairSession.state_dict()
    t_ms: float


class RepairRecordRow(SQLModel, table=True):
    __tablename__ = "repair_records"

    id: str = Field(primary_key=True)
    session_id: str = Field(index=True)
    run_id: str | None = Field(default=None, index=True)
    outcome: str
    fault_id: str | None = None
    data: str
    t_ms: float


class UiEventRow(SQLModel, table=True):
    """Every UI message the engine sent to the companion screen (events and screen.state), in
    order, so a session can be replayed through the screen (engine/replay.py)."""

    __tablename__ = "ui_events"

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    run_id: str | None = Field(default=None, index=True)
    event: str
    data: str  # JSON, the whole message
    t_ms: float


class SpanIn(BaseModel):
    """A span as sent by the browser (or created by the engine)."""

    name: str = PField(min_length=1, max_length=64)
    t_ms: float
    turn: int = 0
    attrs: dict[str, Any] | None = None


def now_ms() -> float:
    return time.time() * 1000.0


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Minimal forward migration: add new nullable columns to tables that already exist."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}"
                    )


class TelemetryStore:
    def __init__(self, db_path: Path):
        self.engine = make_engine(db_path)
        self._queue: asyncio.Queue[SQLModel] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    # --- async side (event loop) -------------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._drain(), name="telemetry-writer")

    async def stop(self) -> None:
        await self._queue.join()
        if self._task:
            self._task.cancel()

    async def flush(self) -> None:
        await self._queue.join()

    def record(
        self,
        session_id: str,
        span: SpanIn,
        source: Literal["browser", "engine"],
        run_id: str | None = None,
    ) -> None:
        self._queue.put_nowait(
            Span(
                run_id=run_id,
                session_id=session_id,
                turn=span.turn,
                name=span.name,
                t_ms=span.t_ms,
                source=source,
                attrs=json.dumps(span.attrs, separators=(",", ":")) if span.attrs else None,
            )
        )

    def record_session(self, row: RealtimeSessionRow) -> None:
        self._queue.put_nowait(row)

    def record_repair(self, table: str, row: dict[str, Any], run_id: str | None = None) -> None:
        """Evidence, ledger snapshots and repair records from a RepairSession."""
        t = now_ms()
        if table == "evidence":
            row = dict(row)
            seq, use = row.pop("seq", 0), row.pop("ledger_use", None)
            model: SQLModel = EvidenceRow(
                id=row["id"], session_id=row["session_id"], run_id=run_id, seq=seq, ledger_use=use, kind=row["kind"],
                measurement_id=row.get("measurement_id"), outcome=row.get("outcome"), confidence=row["confidence"],
                source=row["source"], simulated=row.get("simulated", False), data=json.dumps(row), t_ms=t,
            )
        elif table == "repair_state":
            model = RepairStateRow(session_id=row["session_id"], run_id=run_id, data=json.dumps(row), t_ms=t)
        elif table == "ledger_snapshots":
            model = LedgerSnapshotRow(
                session_id=row["session_id"], run_id=run_id, index=row["index"], evidence_id=row["evidence_id"],
                after=json.dumps(row["after"]), deltas=json.dumps(row["deltas"]), stop=row["stop"],
                conflict=row.get("conflict"), t_ms=t,
            )
        elif table == "repair_records":
            model = RepairRecordRow(
                id=row["id"], session_id=row["session_id"], run_id=run_id, outcome=row["outcome"],
                fault_id=row.get("fault_id"), data=json.dumps(row), t_ms=t,
            )
        else:
            raise ValueError(table)
        self._queue.put_nowait(model)

    def record_ui(self, session_id: str, msg: dict[str, Any], run_id: str | None = None) -> None:
        self._queue.put_nowait(UiEventRow(session_id=session_id, run_id=run_id, event=str(msg.get("event", ""))[:64],
                                          data=json.dumps(msg, separators=(",", ":"), default=str), t_ms=now_ms()))

    def record_transcript(self, session_id: str, msg: TranscriptIn, run_id: str | None = None) -> None:
        self._queue.put_nowait(
            Transcript(
                run_id=run_id,
                session_id=session_id,
                turn=msg.turn,
                role=msg.role,
                item_id=msg.item_id,
                response_id=msg.response_id,
                phase=msg.phase,
                text=msg.text,
                lang_api=",".join(msg.languages) if msg.languages else None,
                lang_tag=lang_tag(msg.text),
                t_ms=msg.t_ms,
            )
        )

    async def _drain(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await asyncio.to_thread(self._write, batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    # --- sync side (worker thread / CLI) -----------------------------------------------------

    def _write(self, rows: Iterable[SQLModel]) -> None:
        with Session(self.engine) as s:
            for row in rows:
                s.merge(row) if isinstance(row, (RealtimeSessionRow, EvidenceRow, RepairRecordRow, RepairStateRow)) else s.add(row)
            s.commit()


def load_spans(engine: Engine, run_id: str | None = None, session_ids: list[str] | None = None) -> list[Span]:
    with Session(engine) as s:
        q = select(Span)
        if run_id is not None:
            q = q.where(Span.run_id == run_id)
        if session_ids:
            q = q.where(Span.session_id.in_(session_ids))  # type: ignore[union-attr]
        return list(s.exec(q.order_by(Span.t_ms)))


def load_sessions(engine: Engine, run_id: str | None = None) -> list[RealtimeSessionRow]:
    with Session(engine) as s:
        q = select(RealtimeSessionRow)
        if run_id is not None:
            q = q.where(RealtimeSessionRow.run_id == run_id)
        return list(s.exec(q.order_by(RealtimeSessionRow.created_ms)))


def load_transcripts(engine: Engine, run_id: str | None = None, session_ids: list[str] | None = None) -> list[Transcript]:
    with Session(engine) as s:
        q = select(Transcript)
        if run_id is not None:
            q = q.where(Transcript.run_id == run_id)
        if session_ids:
            q = q.where(Transcript.session_id.in_(session_ids))  # type: ignore[union-attr]
        return list(s.exec(q.order_by(Transcript.t_ms)))


def load_evidence(engine: Engine, session_id: str) -> list[EvidenceRow]:
    with Session(engine) as s:
        q = select(EvidenceRow).where(EvidenceRow.session_id == session_id)
        return list(s.exec(q.order_by(EvidenceRow.seq)))  # type: ignore[arg-type]


def load_snapshots(engine: Engine, session_id: str) -> list[LedgerSnapshotRow]:
    with Session(engine) as s:
        q = select(LedgerSnapshotRow).where(LedgerSnapshotRow.session_id == session_id)
        return list(s.exec(q.order_by(LedgerSnapshotRow.index)))  # type: ignore[arg-type]


def load_repair_state(engine: Engine, session_id: str) -> dict[str, Any] | None:
    with Session(engine) as s:
        row = s.get(RepairStateRow, session_id)
        return json.loads(row.data) if row else None


def load_ui_events(engine: Engine, session_id: str) -> list[UiEventRow]:
    with Session(engine) as s:
        q = select(UiEventRow).where(UiEventRow.session_id == session_id)
        return list(s.exec(q.order_by(UiEventRow.id)))  # type: ignore[arg-type]


def list_ui_sessions(engine: Engine, limit: int = 50) -> list[dict[str, Any]]:
    """Sessions that can be replayed, newest first."""
    from sqlalchemy import func

    with Session(engine) as s:
        q = (select(UiEventRow.session_id, func.count(UiEventRow.id), func.min(UiEventRow.t_ms), func.max(UiEventRow.t_ms))
             .group_by(UiEventRow.session_id).order_by(func.max(UiEventRow.t_ms).desc()).limit(limit))
        return [{"session_id": sid, "messages": n, "started_ms": a, "ended_ms": b, "seconds": round((b - a) / 1000, 1)}
                for sid, n, a, b in s.exec(q)]  # type: ignore[call-overload]
