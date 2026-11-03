from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from engine.config import Settings
from engine.main import create_app

FAKE_KEY = "test-key-not-a-real-openai-key"


class FakeOpenAI:
    """httpx transport standing in for api.openai.com; records every request."""

    def __init__(self, status: int = 200):
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "nope"}})
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"value": "ek_fake_client_secret", "expires_at": 1_900_000_000, "session": body["session"]},
        )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        openai_api_key=FAKE_KEY,
        db_path=tmp_path / "test.sqlite",
        web_dist=tmp_path / "no-dist",
        dummy_delay_s=0.0,
    )


@pytest.fixture
def fake_openai() -> FakeOpenAI:
    return FakeOpenAI()


@pytest.fixture
def client(settings: Settings, fake_openai: FakeOpenAI):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_openai.handler))
    with TestClient(create_app(settings, http_client=http, prewarm=False)) as c:
        yield c
