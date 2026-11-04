from __future__ import annotations

import json
import time

import httpx
from fastapi.testclient import TestClient

from engine.main import create_app
from engine.tools import ALLOWED_TOOL_NAMES
from engine.telemetry import load_sessions, make_engine

from .conftest import FAKE_KEY, FakeOpenAI, flush_telemetry


def test_mints_secret_with_decided_session_settings(client, fake_openai, settings):
    resp = client.post("/realtime/client-secret", json={"model": "mini", "run_id": "r1"})
    assert resp.status_code == 200
    data = resp.json()

    assert data["client_secret"] == "ek_fake_client_secret"
    assert data["session_id"].startswith("se_")
    assert data["config"]["model"] == settings.realtime_model_mini

    req = fake_openai.requests[0]
    assert req.url.path == "/v1/realtime/client_secrets"
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = json.loads(req.content)
    assert body["expires_after"] == {"anchor": "created_at", "seconds": settings.pool_secret_ttl_s}  # via the pool
    session = body["session"]
    assert session["type"] == "realtime"
    assert session["model"] == settings.realtime_model_mini
    assert session["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "medium",
        "create_response": True,
        "interrupt_response": True,
    }
    assert session["reasoning"] == {"effort": "low"}
    assert session["truncation"] == {
        "type": "retention_ratio",
        "retention_ratio": 0.8,
        "token_limits": {"post_instructions": 8000},
    }
    assert [t["name"] for t in session["tools"]] == list(ALLOWED_TOOL_NAMES)


def test_api_key_never_returned_to_browser(client):
    resp = client.post("/realtime/client-secret", json={})
    assert FAKE_KEY not in resp.text


def test_default_model_is_flagship(client, settings):
    assert client.post("/realtime/client-secret", json={}).json()["config"]["model"] == settings.realtime_model


def test_rejects_models_not_on_the_allow_list(client, fake_openai):
    resp = client.post("/realtime/client-secret", json={"model": "gpt-4o-realtime-preview"})
    assert resp.status_code == 400
    assert fake_openai.requests == []


def test_503_without_api_key(settings):
    settings.openai_api_key = None
    with TestClient(create_app(settings)) as c:
        resp = c.post("/realtime/client-secret", json={})
    assert resp.status_code == 503


def test_openai_error_becomes_502_without_leaking_key(settings):
    fake = FakeOpenAI(status=401)
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    with TestClient(create_app(settings, http_client=http, prewarm=False)) as c:
        resp = c.post("/realtime/client-secret", json={})
    assert resp.status_code == 502
    assert FAKE_KEY not in resp.text


def test_session_is_recorded_for_telemetry(client, settings):
    sid = client.post("/realtime/client-secret", json={"model": "flagship", "run_id": "run-x"}).json()["session_id"]
    flush_telemetry(client)
    rows = load_sessions(make_engine(settings.db_path), "run-x")
    assert [(r.id, r.model) for r in rows] == [(sid, settings.realtime_model)]


def _session(fake_openai, i=0):
    return json.loads(fake_openai.requests[i].content)["session"]


def test_server_vad_modes(client, fake_openai):
    cfg = client.post("/realtime/client-secret", json={"turn_mode": "server_300", "use_pool": False}).json()["config"]
    td = _session(fake_openai)["audio"]["input"]["turn_detection"]
    assert td == {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 300,
        "create_response": True,
        "interrupt_response": True,
    }
    assert cfg["turn_mode"] == "server_300"
    assert set(cfg["turn_modes"]) >= {"semantic_medium", "semantic_high", "server_400", "server_500", "ptt"}


def test_push_to_talk_disables_turn_detection(client, fake_openai):
    client.post("/realtime/client-secret", json={"turn_mode": "ptt", "use_pool": False})
    assert _session(fake_openai)["audio"]["input"]["turn_detection"] is None


def test_prompt_variants_and_preamble_mode(client):
    local = client.post("/realtime/client-secret", json={"prompt": "v1_local", "use_pool": False}).json()["config"]
    short = client.post("/realtime/client-secret", json={"prompt": "v1", "use_pool": False}).json()["config"]
    assert (local["preamble"], short["preamble"]) == ("local", "model")
    assert local["instructions"] != short["instructions"]


def test_output_cap_on_by_default_and_can_be_turned_off(client, fake_openai, settings):
    client.post("/realtime/client-secret", json={"use_pool": False})
    client.post("/realtime/client-secret", json={"output_cap": False, "use_pool": False})
    assert _session(fake_openai, 0)["max_output_tokens"] == settings.max_output_tokens
    assert "max_output_tokens" not in _session(fake_openai, 1)


def test_rejects_unknown_turn_mode_and_prompt(client, fake_openai):
    assert client.post("/realtime/client-secret", json={"turn_mode": "server_50"}).status_code == 400
    assert client.post("/realtime/client-secret", json={"prompt": "v9"}).status_code == 400
    assert fake_openai.requests == []


def test_second_request_is_served_from_the_pool(client, fake_openai):
    first = client.post("/realtime/client-secret", json={"model": "mini"}).json()
    assert first["secret_source"] == "fresh"
    for _ in range(100):  # the background refill runs on the app's loop
        if client.app.state.secret_pool.size():
            break
        time.sleep(0.01)
    second = client.post("/realtime/client-secret", json={"model": "mini"}).json()
    assert second["secret_source"] == "pool"
    assert second["session_id"] != first["session_id"]


def test_pool_never_hands_out_a_nearly_expired_secret():
    import asyncio

    from engine.api.realtime import SecretPool

    async def mint(payload):
        return {"value": "ek_new", "expires_at": time.time() + 120}

    async def scenario():
        pool = SecretPool(mint, min_remaining_s=30)
        payload = {"x": 1}
        pool._items[pool.key(payload)] = {"value": "ek_old", "expires_at": time.time() + 5}
        item, source = await pool.take(payload)
        await pool.close()
        return item["value"], source

    assert asyncio.run(scenario()) == ("ek_new", "fresh")


def test_pool_refreshes_wanted_secrets_before_they_expire_and_forgets_unused_ones():
    import asyncio

    from engine.api.realtime import SecretPool

    minted = []

    async def mint(payload):
        minted.append(payload["x"])
        return {"value": f"ek_{len(minted)}", "expires_at": time.time() + 600}

    async def scenario():
        pool = SecretPool(mint, min_remaining_s=60, keep_s=1800)
        used, stale = {"x": "used"}, {"x": "stale"}
        await pool.take(used)
        await pool.take(stale)
        await asyncio.sleep(0)  # refills run
        pool._items[pool.key(used)]["expires_at"] = time.time() + 70  # about to age out
        pool._wanted[pool.key(stale)] = (stale, time.time() - 3600)  # not used for an hour
        pool.maintain_once()
        await asyncio.sleep(0)
        await pool.close()
        return minted, pool.key(stale) in pool._items

    minted_payloads, stale_kept = asyncio.run(scenario())
    assert minted_payloads.count("used") == 3  # take (fresh), refill, refresh before expiry
    assert not stale_kept


def test_pooled_secrets_get_the_longer_pool_lifetime(settings, fake_openai):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_openai.handler))
    with TestClient(create_app(settings, http_client=http, prewarm=False)) as c:
        c.post("/realtime/client-secret", json={"use_pool": False})
        c.post("/realtime/client-secret", json={})
        for _ in range(100):
            if len(fake_openai.requests) >= 3:
                break
            time.sleep(0.01)
    ttls = [json.loads(r.content)["expires_after"]["seconds"] for r in fake_openai.requests]
    assert ttls[0] == settings.client_secret_ttl_s
    assert ttls[1:] == [settings.pool_secret_ttl_s] * 2
