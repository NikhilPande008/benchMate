"""The interface between the engine and whatever produces evidence.

Phase 2 uses the in-process FakeBench. Phase 3's MCP evidence server (cameras, Reader A/B) sits
behind the same interface through an MCP client adapter, so the engine does not change.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Protocol

from .schema import BootSample, EvidenceError, EvidenceRecord

OnSample = Callable[[BootSample], Awaitable[bool]]  # return True to stop the watch early


class EvidenceSource(Protocol):
    async def read_supply(self, session_id: str) -> EvidenceRecord | EvidenceError: ...

    async def read_meter(
        self, session_id: str, point_id: str | None, expected_mode: str | None
    ) -> EvidenceRecord | EvidenceError: ...

    async def read_port_current(self, session_id: str) -> EvidenceRecord | EvidenceError: ...

    async def watch_boot_current(
        self, session_id: str, trigger: str, limit_a: float, on_sample: OnSample
    ) -> EvidenceRecord | EvidenceError: ...
