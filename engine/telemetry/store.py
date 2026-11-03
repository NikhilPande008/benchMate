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
                s.merge(row) if isinstance(row, RealtimeSessionRow) else s.add(row)
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
