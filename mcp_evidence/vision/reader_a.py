"""Reader A: the vision-model reader (BUILD_GUIDE "Reading instrument displays with vision").

One call per display crop (the meter, or the whole supply with its three fields), with a strict
JSON schema. Wrappers, in the order they apply:

- `CachedReaderA`: an identical crop returns the cached read at once (exact hash of the crop
  quantised to 32 levels: sensor noise can miss the cache, a changed digit can never hit it);
- `HedgedReaderA`: no answer by 2.5 s → a second request; whichever answers first wins and the
  other is cancelled; nothing by 4 s → timeout;
- the reader itself: `FixtureReaderA` (recorded or synthetic answers, used in this phase: no API
  calls) or `OpenAIReaderA` (the real call, written but disabled by config: BENCHMATE_READER_A).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Protocol

import cv2
import numpy as np

UNITS = ["V", "mV", "A", "mA", "ohm", "kohm", "Mohm", "diode_V", "OL", "unknown"]
SUPPLY_FIELDS = ("set_voltage", "live_current", "current_limit")

FIELD_SCHEMA = {
    "type": "object",
    "properties": {
        "legible": {"type": "boolean", "description": "false if any digit or the decimal point can't be read with certainty"},
        "text": {"type": ["string", "null"], "description": "digits, decimal point and minus exactly as shown, e.g. \"0.352\", \"04.00\", \"OL\""},
        "value": {"type": ["number", "null"], "description": "the number as shown, in the displayed unit; null for OL"},
        "unit": {"type": "string", "enum": UNITS},
        "mode_indicator": {"type": ["string", "null"], "description": "meter mode icon shown: diode, dcv, ohm, current; null if none"},
        "decimals_shown": {"type": "integer", "description": "digits after the decimal point"},
        "confidence": {"type": "number", "description": "0 to 1"},
    },
    "required": ["legible", "text", "value", "unit", "mode_indicator", "decimals_shown", "confidence"],
    "additionalProperties": False,
}
SCHEMAS = {
    "meter": FIELD_SCHEMA,
    "supply": {
        "type": "object",
        "properties": {f: FIELD_SCHEMA for f in SUPPLY_FIELDS},
        "required": list(SUPPLY_FIELDS),
        "additionalProperties": False,
    },
}
PROMPTS = {
    "meter": "This is a tight crop of a digital multimeter display. Read it exactly as shown. The decimal point matters most: "
             "if you are not certain where it is, set legible to false.",
    "supply": "This is a tight crop of a bench power supply display: the top row is the voltage, the middle row the output "
              "current, the small bottom row the current limit. Read each exactly as shown. The decimal points matter most: "
              "if you are not certain of one, set that field's legible to false.",
}


@dataclass(frozen=True)
class ReadA:
    legible: bool
    text: str | None
    value: float | None
    unit: str
    mode: str | None
    decimals: int
    confidence: float
    ms: float = 0.0
    hedged: bool = False
    cached: bool = False
    error: str | None = None  # "timeout" | "error" | "refusal"

    @property
    def ol(self) -> bool:
        return self.unit == "OL" or (self.text or "").upper() == "OL"

    @property
    def key(self) -> str | None:
        """Comparable with Reader B's key: the number with its decimals, or "OL"."""
        if not self.legible or self.error:
            return None
        if self.ol:
            return "OL"
        if self.value is None:
            return None
        return f"{self.value:.{self.decimals}f}"

    @classmethod
    def failed(cls, error: str, ms: float = 0.0) -> "ReadA":
        return cls(False, None, None, "unknown", None, 0, 0.0, ms, error=error)

    @classmethod
    def from_json(cls, d: dict, ms: float = 0.0) -> "ReadA":
        unit = d.get("unit") if d.get("unit") in UNITS else "unknown"
        value = d.get("value")
        return cls(bool(d.get("legible")), d.get("text"), float(value) if isinstance(value, (int, float)) else None, unit,
                   d.get("mode_indicator"), int(d.get("decimals_shown") or 0), float(d.get("confidence") or 0.0), ms)


Reading = dict[str, ReadA]  # field → read ("main" for the meter)


class ReaderA(Protocol):
    async def read(self, display: str, crop: np.ndarray, attempt: int = 0) -> Reading: ...


def crop_hash(display: str, crop: np.ndarray) -> str:
    q = (crop // 8).astype(np.uint8)
    return hashlib.sha1(display.encode() + str(q.shape).encode() + q.tobytes()).hexdigest()


def fields_of(display: str) -> tuple[str, ...]:
    return SUPPLY_FIELDS if display == "supply" else ("main",)


def _failed_all(display: str, error: str, ms: float) -> Reading:
    return {f: ReadA.failed(error, ms) for f in fields_of(display)}


# --- fixture reader (this phase) ---------------------------------------------------------------


@dataclass
class FixtureAnswer:
    reading: Reading
    latency_s: tuple[float, ...] = (1.2,)  # per attempt; > timeout means it never answers in time
    raises: bool = False


class FixtureReaderA:
    """Recorded or synthetic answers keyed by crop hash; unknown crops get `default`. Latencies
    are slept for real, scaled by `time_scale` (tests and the eval run it small)."""

    def __init__(self, default: Callable[[str, np.ndarray], FixtureAnswer] | None = None, time_scale: float = 1.0):
        self.answers: dict[str, FixtureAnswer] = {}
        self.default = default
        self.time_scale = time_scale
        self.calls = 0

    def register(self, display: str, crop: np.ndarray, answer: FixtureAnswer) -> str:
        h = crop_hash(display, crop)
        self.answers[h] = answer
        return h

    async def read(self, display: str, crop: np.ndarray, attempt: int = 0) -> Reading:
        self.calls += 1
        ans = self.answers.get(crop_hash(display, crop))
        if ans is None:
            ans = self.default(display, crop) if self.default else FixtureAnswer(_failed_all(display, "illegible", 0.0))
        lat = ans.latency_s[min(attempt, len(ans.latency_s) - 1)]
        if lat > 0 and self.time_scale > 0:
            await asyncio.sleep(lat * self.time_scale)
        if ans.raises:
            raise RuntimeError("fixture: vision API error")
        return {f: replace(r, ms=lat * 1000) for f, r in ans.reading.items()}


# --- hedging and cache -------------------------------------------------------------------------


class HedgedReaderA:
    """Second request after `hedge_s`; first answer wins; `timeout_s` hard limit (decided settings:
    4 s, hedge at 2.5 s). Times are scaled by `time_scale` for tests."""

    def __init__(self, inner: ReaderA, hedge_s: float = 2.5, timeout_s: float = 4.0, time_scale: float = 1.0):
        self.inner, self.hedge_s, self.timeout_s, self.time_scale = inner, hedge_s, timeout_s, time_scale
        self.hedges = 0
        self.timeouts = 0

    async def read(self, display: str, crop: np.ndarray, attempt: int = 0) -> Reading:
        loop = asyncio.get_running_loop()
        ts = self.time_scale
        t0 = loop.time()
        deadline = t0 + self.timeout_s * ts

        def done_ms() -> float:
            return (loop.time() - t0) * 1000 / (ts or 1)

        first = asyncio.ensure_future(self.inner.read(display, crop, 0))
        try:
            return await asyncio.wait_for(asyncio.shield(first), self.hedge_s * ts)
        except asyncio.TimeoutError:
            pass  # slow: hedge
        except Exception:
            pass  # failed early: the second request is the one retry
        self.hedges += 1
        second = asyncio.ensure_future(self.inner.read(display, crop, 1))
        pending = {first, second}
        errored = 0
        while pending:
            done, pending = await asyncio.wait(pending, timeout=max(0.0, deadline - loop.time()), return_when=asyncio.FIRST_COMPLETED)
            if not done:
                break
            for d in done:
                if d.exception() is None:
                    for t in pending:
                        t.cancel()
                    return {f: replace(r, hedged=True, ms=done_ms()) for f, r in d.result().items()}
                errored += 1
        for t in pending:
            t.cancel()
        if errored == 2:
            return _failed_all(display, "error", done_ms())
        self.timeouts += 1
        return _failed_all(display, "timeout", done_ms())


class CachedReaderA:
    def __init__(self, inner: ReaderA, size: int = 256):
        self.inner = inner
        self.size = size
        self._cache: OrderedDict[str, Reading] = OrderedDict()
        self.hits = 0

    async def read(self, display: str, crop: np.ndarray, attempt: int = 0) -> Reading:
        h = crop_hash(display, crop)
        if h in self._cache:
            self.hits += 1
            self._cache.move_to_end(h)
            return {f: replace(r, cached=True, ms=0.0) for f, r in self._cache[h].items()}
        out = await self.inner.read(display, crop, attempt)
        if all(r.error is None for r in out.values()):
            self._cache[h] = out
            while len(self._cache) > self.size:
                self._cache.popitem(last=False)
        return out


# --- the real call (disabled in this phase) ----------------------------------------------------


def resize_for_model(crop: np.ndarray, width: int = 512) -> np.ndarray:
    """The smallest size that keeps digits sharp: start at 512 px wide (BUILD_GUIDE)."""
    if crop.shape[1] <= width:
        return crop
    h = int(round(crop.shape[0] * width / crop.shape[1]))
    return cv2.resize(crop, (width, h), interpolation=cv2.INTER_AREA)


def request_body(display: str, crop: np.ndarray, model: str, detail: str = "high", reasoning: str | None = "none") -> dict:
    """POST /v1/responses: one image as a base64 data URL, a strict JSON schema output
    (developers.openai.com/api/docs/guides/images-vision and /structured-outputs, read 5 Nov 2026)."""
    ok, buf = cv2.imencode(".jpg", resize_for_model(crop), [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok
    url = "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()
    body: dict = {
        "model": model,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": PROMPTS[display]},
                                               {"type": "input_image", "image_url": url, "detail": detail}]}],
        "text": {"format": {"type": "json_schema", "name": f"{display}_reading", "schema": SCHEMAS[display], "strict": True}},
        "max_output_tokens": 400,
        "store": False,
    }
    if reasoning:
        body["reasoning"] = {"effort": reasoning}
    return body


def parse_response(display: str, resp: dict, ms: float) -> Reading:
    """output[type=message].content[0]: output_text (JSON) or a refusal."""
    if resp.get("status") == "incomplete":
        return _failed_all(display, "error", ms)
    msg = next((o for o in resp.get("output", []) if o.get("type") == "message"), None)
    part = (msg or {}).get("content", [{}])[0] if msg else {}
    if part.get("type") == "refusal":
        return _failed_all(display, "refusal", ms)
    try:
        data = json.loads(part.get("text") or resp.get("output_text") or "")
    except (TypeError, ValueError):
        return _failed_all(display, "error", ms)
    if display == "supply":
        return {f: ReadA.from_json(data.get(f) or {}, ms) for f in SUPPLY_FIELDS}
    return {"main": ReadA.from_json(data, ms)}


class OpenAIReaderA:
    """The real Reader A. Not used until BENCHMATE_READER_A=openai (docs/PENDING_RUNS.md); the
    key stays in the engine process (CLAUDE.md rule 5)."""

    URL = "https://api.openai.com/v1/responses"

    def __init__(self, api_key: str, model: str = "gpt-6-luna", detail: str = "high", reasoning: str | None = "none",
                 client: Awaitable | None = None, enabled: bool = False):
        self.api_key, self.model, self.detail, self.reasoning = api_key, model, detail, reasoning
        self.enabled = enabled
        self._client = client

    async def read(self, display: str, crop: np.ndarray, attempt: int = 0) -> Reading:
        if not self.enabled:
            raise RuntimeError("OpenAIReaderA is disabled in this phase (BENCHMATE_READER_A=openai to enable)")
        import httpx

        client = self._client or httpx.AsyncClient(timeout=10.0)
        t0 = time.perf_counter()
        try:
            r = await client.post(self.URL, json=request_body(display, crop, self.model, self.detail, self.reasoning),  # type: ignore[union-attr]
                                  headers={"Authorization": f"Bearer {self.api_key}"})
            ms = (time.perf_counter() - t0) * 1000
            if r.status_code != 200:
                return _failed_all(display, "error", ms)
            return parse_response(display, r.json(), ms)
        finally:
            if self._client is None:
                await client.aclose()  # type: ignore[union-attr]
