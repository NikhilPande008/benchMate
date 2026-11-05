"""Benchmate engine: FastAPI app (HTTP + WebSocket). Binds to 127.0.0.1 by default."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from engine.api import dev, realtime, screen, ws
from engine.config import Settings
from engine.telemetry import TelemetryStore


def create_app(
    settings: Settings | None = None, http_client: httpx.AsyncClient | None = None, prewarm: bool = True
) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.telemetry = TelemetryStore(settings.db_path)
        app.state.connections = ws.Connections()
        app.state.http = http_client or httpx.AsyncClient()
        app.state.mint = realtime.make_minter(settings, app.state.http)
        app.state.secret_pool = realtime.SecretPool(realtime.make_minter(settings, app.state.http, settings.pool_secret_ttl_s))
        await app.state.telemetry.start()
        if settings.openai_api_key and settings.secret_pool and prewarm:
            app.state.secret_pool.pin(realtime.default_payload(settings))
            app.state.secret_pool.start()
        try:
            yield
        finally:
            await app.state.secret_pool.close()
            await app.state.telemetry.stop()
            if http_client is None:
                await app.state.http.aclose()

    app = FastAPI(title="Benchmate engine", lifespan=lifespan)
    app.include_router(realtime.router)
    app.include_router(ws.router)
    app.include_router(dev.router)
    app.include_router(screen.router)

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"ok": True, "openai_key_configured": bool(settings.openai_api_key)}

    # Serve the built web app (apps/web/dist) when present; in development Vite proxies to us.
    if settings.web_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dist, html=True), name="web")
    return app
