"""POST /realtime/client-secret: hand the browser a short-lived Realtime client secret.

The OpenAI API key stays in the engine (CLAUDE.md rule 5). The session is fully configured
here, and the same config is returned so the browser's Agents SDK session.update matches it
exactly; instructions and tools never change mid-session (rule 7). Only the turn detection
mode may be switched at runtime (a dev control), and the browser gets the allowed modes here.

Minting a secret is a round trip to OpenAI, so the engine keeps one pre-minted secret per
session config (SecretPool) and refills it in the background after each hand-out.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from typing import Any, Awaitable, Callable

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from engine.config import Settings
from engine.session.prompt import LOCAL_PREAMBLE_PROMPTS, PROMPTS, TRANSCRIPTION_KEYWORDS, load_prompt
from engine.telemetry import RealtimeSessionRow, TelemetryStore, now_ms
from engine.tools import tool_specs

log = logging.getLogger("benchmate.realtime")
router = APIRouter()

_VAD_COMMON = {"create_response": True, "interrupt_response": True}
# Allowed turn detection modes (Realtime API `audio.input.turn_detection`). None = push-to-talk:
# the browser clears the input buffer on key down and commits + requests a response on key up.
TURN_MODES: dict[str, dict[str, Any] | None] = {
    "semantic_medium": {"type": "semantic_vad", "eagerness": "medium", **_VAD_COMMON},
    "semantic_high": {"type": "semantic_vad", "eagerness": "high", **_VAD_COMMON},
    **{
        f"server_{ms}": {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": ms,
            **_VAD_COMMON,
        }
        for ms in (300, 400, 500)
    },
    "ptt": None,
}


class ClientSecretRequest(BaseModel):
    model: str | None = Field(default=None, description='"flagship", "mini", or an allowed model id')
    run_id: str | None = Field(default=None, max_length=64)
    turn_mode: str | None = Field(default=None, description="key of TURN_MODES; default from config")
    prompt: str | None = Field(default=None, description="key of PROMPTS; default from config")
    output_cap: bool | None = Field(default=None, description="apply max_output_tokens (default: on)")
    use_pool: bool = True


def new_session_id() -> str:
    return f"se_{int(time.time() * 1000):x}{secrets.token_hex(4)}"


def voice_config(
    settings: Settings, model: str, turn_mode: str, prompt_name: str, cap: int | None
) -> dict[str, Any]:
    """The session settings, in Realtime API (snake_case) shape, plus what the browser needs."""
    prompt = load_prompt(PROMPTS[prompt_name])
    return {
        "model": model,
        "instructions": prompt.text,
        "prompt": prompt_name,
        "prompt_version": prompt.version,
        "preamble": "local" if prompt_name in LOCAL_PREAMBLE_PROMPTS else "model",
        "voice": settings.voice,
        "turn_mode": turn_mode,
        "turn_detection": TURN_MODES[turn_mode],
        "turn_modes": TURN_MODES,
        "reasoning": {"effort": settings.reasoning_effort},
        "truncation": {
            "type": "retention_ratio",
            "retention_ratio": 0.8,
            "token_limits": {"post_instructions": 8000},
        },
        "max_output_tokens": cap,
        "transcription": {
            "model": settings.transcription_model,
            "languages": ["hi", "en"],
            "keywords": TRANSCRIPTION_KEYWORDS,
        },
        "tools": tool_specs(),
    }


def session_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    """Body of `session` for POST /v1/realtime/client_secrets."""
    payload = {
        "type": "realtime",
        "model": cfg["model"],
        "instructions": cfg["instructions"],
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "turn_detection": cfg["turn_detection"],
                "transcription": cfg["transcription"],
                "noise_reduction": None,
            },
            "output": {"voice": cfg["voice"]},
        },
        "reasoning": cfg["reasoning"],
        "truncation": cfg["truncation"],
        "tools": cfg["tools"],
        "tool_choice": "auto",
    }
    if cfg["max_output_tokens"] is not None:
        payload["max_output_tokens"] = cfg["max_output_tokens"]
    return payload


Mint = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class SecretPool:
    """At most one pre-minted secret per session payload, so a page load does not wait for a
    round trip to OpenAI. A secret is handed out only while it has at least `min_remaining_s`
    left. `maintain()` re-mints before expiry for payloads used in the last `keep_s` seconds
    (and for pinned ones, such as the page's default config)."""

    def __init__(self, mint: Mint, min_remaining_s: float = 60.0, keep_s: float = 1800.0):
        self._mint = mint
        self._min_remaining_s = min_remaining_s
        self._keep_s = keep_s
        self._items: dict[str, dict[str, Any]] = {}
        self._wanted: dict[str, tuple[dict[str, Any], float]] = {}  # key -> (payload, last used)
        self._pinned: set[str] = set()
        self._refills: dict[str, asyncio.Task[None]] = {}
        self._maintainer: asyncio.Task[None] | None = None

    @staticmethod
    def key(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def size(self) -> int:
        return len(self._items)

    def _fresh(self, item: dict[str, Any] | None, margin: float = 0.0) -> bool:
        return bool(item) and item["expires_at"] - time.time() >= self._min_remaining_s + margin

    async def take(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        key = self.key(payload)
        self._wanted[key] = (payload, time.time())
        item = self._items.pop(key, None)
        if item and self._fresh(item):
            source = "pool"
        else:
            item, source = await self._mint(payload), "fresh"
        self.refill(payload)
        return item, source

    def pin(self, payload: dict[str, Any]) -> None:
        key = self.key(payload)
        self._pinned.add(key)
        self._wanted[key] = (payload, time.time())
        self.refill(payload)

    def refill(self, payload: dict[str, Any]) -> None:
        key = self.key(payload)
        running = self._refills.get(key)
        if running and not running.done():
            return
        self._refills[key] = asyncio.create_task(self._fill(key, payload))

    async def _fill(self, key: str, payload: dict[str, Any]) -> None:
        try:
            self._items[key] = await self._mint(payload)
        except Exception as exc:  # a failed refill just means the next request mints fresh
            log.warning("secret refill failed: %s", exc)

    def maintain_once(self) -> None:
        now = time.time()
        for key, (payload, used) in list(self._wanted.items()):
            if key not in self._pinned and now - used > self._keep_s:
                self._wanted.pop(key)
                self._items.pop(key, None)
                continue
            if not self._fresh(self._items.get(key), margin=30):
                self.refill(payload)

    def start(self, every_s: float = 20.0) -> None:
        async def loop() -> None:
            while True:
                await asyncio.sleep(every_s)
                self.maintain_once()

        self._maintainer = asyncio.create_task(loop())

    async def close(self) -> None:
        if self._maintainer:
            self._maintainer.cancel()
        for task in self._refills.values():
            task.cancel()


def make_minter(settings: Settings, http: httpx.AsyncClient, ttl_s: int | None = None) -> Mint:
    ttl = ttl_s or settings.client_secret_ttl_s

    async def mint(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await http.post(
                f"{settings.openai_base_url}/realtime/client_secrets",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "expires_after": {"anchor": "created_at", "seconds": ttl},
                    "session": payload,
                },
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"OpenAI request failed: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            try:
                message = resp.json().get("error", {}).get("message", "")
            except ValueError:
                message = ""
            raise HTTPException(502, f"OpenAI returned {resp.status_code}: {message}"[:300])
        data = resp.json()
        return {"value": data["value"], "expires_at": data["expires_at"]}

    return mint


def default_payload(settings: Settings) -> dict[str, Any]:
    """The config the web page asks for when nothing is overridden (used to pre-warm the pool)."""
    cfg = voice_config(
        settings, settings.realtime_model_mini, settings.turn_mode, settings.prompt, settings.max_output_tokens
    )
    return session_payload(cfg)


@router.post("/realtime/client-secret")
async def create_client_secret(body: ClientSecretRequest, request: Request) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    telemetry: TelemetryStore = request.app.state.telemetry
    pool: SecretPool = request.app.state.secret_pool

    if not settings.openai_api_key:
        raise HTTPException(503, "OPENAI_API_KEY is not set in the engine environment")
    model = settings.resolve_model(body.model)
    if model is None:
        raise HTTPException(400, "model must be 'flagship', 'mini' or an allowed model id")
    turn_mode = body.turn_mode or settings.turn_mode
    if turn_mode not in TURN_MODES:
        raise HTTPException(400, f"turn_mode must be one of {sorted(TURN_MODES)}")
    prompt_name = body.prompt or settings.prompt
    if prompt_name not in PROMPTS:
        raise HTTPException(400, f"prompt must be one of {sorted(PROMPTS)}")
    cap = settings.max_output_tokens if body.output_cap is not False else None

    cfg = voice_config(settings, model, turn_mode, prompt_name, cap)
    payload = session_payload(cfg)
    t0 = time.perf_counter()
    if body.use_pool and settings.secret_pool:
        minted, source = await pool.take(payload)
    else:
        minted, source = await request.app.state.mint(payload), "fresh"
    mint_ms = (time.perf_counter() - t0) * 1000

    session_id = new_session_id()
    telemetry.record_session(
        RealtimeSessionRow(
            id=session_id,
            run_id=body.run_id,
            model=model,
            prompt_version=cfg["prompt_version"],
            created_ms=now_ms(),
            turn_mode=turn_mode,
            prompt=prompt_name,
            max_output_tokens=cap,
            secret_source=source,
        )
    )
    return {
        "session_id": session_id,
        "client_secret": minted["value"],
        "expires_at": minted["expires_at"],
        "secret_source": source,
        "mint_ms": round(mint_ms, 1),
        "config": cfg,
    }
