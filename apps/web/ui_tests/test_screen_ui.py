"""Playwright UI tests of the companion screen against the engine with fake cameras and the
fixture Reader A: no audio, no microphone, no API calls (no key, the secret pool off, and the
OpenAI base URL points at a closed local port). The page is opened screen-only
(?screen=1&session=…) and the repair is driven through /dev/sessions/{id}/drive with exactly
the messages the voice page would send.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

pw = pytest.importorskip("playwright.sync_api")

WEB = Path(__file__).resolve().parents[1]
DIST = WEB / "dist"


def _stale() -> bool:
    if not (DIST / "index.html").exists():
        return True
    built = (DIST / "index.html").stat().st_mtime
    srcs = [WEB / "index.html", *WEB.glob("src/**/*.ts"), *WEB.glob("src/**/*.tsx"), *WEB.glob("src/**/*.css")]
    return any(p.stat().st_mtime > built for p in srcs)


@pytest.fixture(scope="module")
def dist() -> Path:
    if _stale():
        if shutil.which("npm") is None:
            pytest.skip("npm not installed and the web build is missing or stale")
        subprocess.run(["npm", "run", "build"], cwd=WEB, check=True, capture_output=True, timeout=180)
    return DIST


@pytest.fixture(scope="module")
def engine(dist, tmp_path_factory):
    import uvicorn

    from engine.config import Settings
    from engine.main import create_app

    tmp = tmp_path_factory.mktemp("ui")
    settings = Settings(openai_api_key=None, openai_base_url="http://127.0.0.1:9", secret_pool=False, db_path=tmp / "ui.sqlite",
                        web_dist=dist, evidence="vision", fake_time_scale=0.02, frames_dir=tmp / "frames", dummy_delay_s=0.0)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings, prewarm=False), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield url
    server.should_exit = True
    t.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser installed
            pytest.skip(f"chromium not available: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    pg = ctx.new_page()
    yield pg
    ctx.close()


def drive(engine: str, sid: str, msg: dict) -> dict:
    r = httpx.post(f"{engine}/dev/sessions/{sid}/drive", json=msg, timeout=20)
    assert r.status_code == 200, r.text
    return r.json()


def tool(engine: str, sid: str, name: str, **args) -> dict:
    return drive(engine, sid, {"type": "tool_call", "name": name, "args": args})["output"]


def open_screen(page, engine: str, sid: str, extra: str = ""):
    page.goto(f"{engine}/?screen=1&session={sid}{extra}")
    page.get_by_test_id("screen").wait_for()
    page.wait_for_function("() => !document.body.innerText.includes('ENGINE DISCONNECTED')")
    return page


def started(engine: str, sid: str, lang: str = "en") -> dict:
    drive(engine, sid, {"type": "lang", "lang": lang})
    tool(engine, sid, "start_repair", model="demo", symptom="dead", battery="removed")
    tool(engine, sid, "get_reading", kind="supply")
    return tool(engine, sid, "next_step")


def test_the_five_zones_and_the_header(page, engine):
    open_screen(page, engine, "se_ui_zones")
    for zone in ("board", "readings", "ledger", "evidence", "conversation"):
        assert page.get_by_test_id(zone).is_visible(), zone
    assert page.get_by_test_id("session-state").inner_text() == "INTAKE"


def test_a_repair_fills_the_screen(page, engine):
    sid = "se_ui_repair"
    open_screen(page, engine, sid)
    step = started(engine, sid)
    assert step["point"] == "vbat_conn"
    page.get_by_test_id("next-step").filter(has_text="VBAT").wait_for()
    assert page.get_by_test_id("supply-volts").inner_text() == "4.00"
    assert page.get_by_test_id("supply-limit").inner_text() == "1.00"
    faults = page.locator("[data-testid^='fault-']")
    assert faults.count() >= 3 and "%" in faults.first.inner_text()
    page.get_by_test_id("ref-marker").wait_for()
    page.get_by_test_id("live-marker").wait_for(timeout=8000)  # aligned on the live microscope frame
    assert page.get_by_test_id("reference").evaluate("img => img.naturalWidth") == 640
    assert page.get_by_test_id("microscope").evaluate("img => img.naturalWidth") == 800
    out = tool(engine, sid, "get_reading", kind="meter", point_id="vbat_conn")
    page.get_by_test_id("meter-value").filter(has_text="V").wait_for()
    assert page.get_by_test_id("meter-class").inner_text() == out["class"].upper()
    # every reading names its source and opens its frame in one click
    item = page.get_by_test_id("evidence-item").first
    assert "Camera" in item.inner_text()
    item.get_by_test_id("thumb").click()
    full = page.get_by_test_id("frame-full")
    full.wait_for()
    page.wait_for_function("() => document.querySelector('[data-testid=frame-full]').naturalWidth > 0")
    page.get_by_test_id("frame-modal").click(position={"x": 5, "y": 5})
    assert page.get_by_test_id("frame-modal").count() == 0
    # the ledger shows what moved each fault
    assert "SHORT" in page.get_by_test_id("ledger").inner_text()


def test_the_power_banner_counts_down_and_ends_on_barge_in(page, engine):
    sid = "se_ui_power"
    open_screen(page, engine, sid)
    started(engine, sid)
    gate = tool(engine, sid, "request_power_step", step="connect_boot_cable")
    assert gate["approved"] is True
    banner = page.get_by_test_id("banner-power")
    banner.wait_for()
    text = banner.inner_text()
    assert "POWER STEP APPROVED" in text and "Connect the boot cable now." in text and "4.00 V" in text and "1.00 A" in text
    first = int(page.get_by_test_id("power-left").inner_text().split()[1])
    page.wait_for_timeout(1600)
    assert int(page.get_by_test_id("power-left").inner_text().split()[1]) < first
    drive(engine, sid, {"type": "event", "name": "barge_in"})
    banner.wait_for(state="detached")
    page.get_by_test_id("interruption").wait_for()


def test_a_pending_power_step_shows_the_values_the_camera_must_confirm(page, engine):
    sid = "se_ui_pending"
    open_screen(page, engine, sid)
    started(engine, sid)
    tool(engine, sid, "get_reading", kind="meter", point_id="vbat_conn")
    step = tool(engine, sid, "next_step")
    assert step["step"] == "power"
    drive(engine, sid, {"type": "event", "name": "barge_in"})  # ends the approval; the step still needs power
    pending = page.get_by_test_id("banner-power-pending")
    pending.wait_for()
    assert "4.0 V" in pending.inner_text() and "1.0 A" in pending.inner_text()


def to_boot_step(engine: str, sid: str, step: dict) -> dict:
    """Answer steps as the voice model would until the engine asks for the boot-current watch."""
    for _ in range(12):
        if step.get("measurement") == "boot_pattern":
            return step
        if step.get("point"):
            tool(engine, sid, "get_reading", kind="meter", point_id=step["point"])
        elif step.get("ask"):
            words = "cable theek hai" if "cable" in step["measurement"] else "kuch garam nahi"
            tool(engine, sid, "record_spoken_reading", text=words, point_id=step["measurement"])
            tool(engine, sid, "record_spoken_reading", text="haan", confirm=True)
        step = tool(engine, sid, "next_step")
    raise AssertionError(f"never reached the boot watch: {step}")


def test_the_boot_current_graph(page, engine):
    sid = "se_ui_boot"
    open_screen(page, engine, sid)
    step = to_boot_step(engine, sid, started(engine, sid))
    if not step.get("approved"):
        tool(engine, sid, "request_power_step", step="connect_boot_cable")
    out = tool(engine, sid, "watch_boot_current", trigger="on_connect")
    page.get_by_test_id("boot-pattern").wait_for()
    pts = page.get_by_test_id("boot-trace").get_attribute("points").split()
    assert len(pts) >= 5 and page.get_by_test_id("boot-pattern").inner_text() == "SHORT AT CONNECT" and out["pattern"] == "SHORT_IMMEDIATE"


def test_transcript_language_tags_and_interruptions(page, engine):
    sid = "se_ui_talk"
    open_screen(page, engine, sid)
    drive(engine, sid, {"type": "transcript", "role": "user", "text": "Supply ka current batao", "t_ms": 1})
    drive(engine, sid, {"type": "transcript", "role": "assistant", "text": "Zero point zero amp.", "t_ms": 2})
    drive(engine, sid, {"type": "transcript", "role": "user", "text": "Read the meter please", "t_ms": 3})
    drive(engine, sid, {"type": "event", "name": "barge_in"})
    drive(engine, sid, {"type": "event", "name": "safety_violation", "kind": "connect_boot_cable", "text": "Boot cable lagao."})
    conv = page.get_by_test_id("conversation")
    page.get_by_test_id("interruption").nth(1).wait_for()
    tags = [t.inner_text() for t in conv.get_by_test_id("lang-tag").all()]
    assert tags == ["HINGLISH", "EN"]
    marks = [m.inner_text() for m in conv.get_by_test_id("interruption").all()]
    assert marks == ["— interrupted —", "— power instruction stopped —"]


def test_degraded_camera_banners(page, engine):
    sid = "se_ui_degraded"
    open_screen(page, engine, sid)
    started(engine, sid)
    httpx.post(f"{engine}/dev/sessions/{sid}/vision", json={"api_down": True})
    tool(engine, sid, "get_reading", kind="supply")
    tool(engine, sid, "get_reading", kind="supply")
    banner = page.get_by_test_id("banner-degraded")
    banner.filter(has_text="CAMERA READING LIMITED").wait_for()
    httpx.post(f"{engine}/dev/sessions/{sid}/vision", json={"api_down": False, "offset": [25, 0]})
    tool(engine, sid, "get_reading", kind="supply")
    banner.filter(has_text="CAMERA UNAVAILABLE").wait_for()
    assert "recalibrate" in banner.inner_text()


def test_the_language_toggle_sets_the_session_language(page, engine):
    sid = "se_ui_lang"
    open_screen(page, engine, sid)
    started(engine, sid, "en")
    lang = page.get_by_test_id("language")
    lang.get_by_role("button", name="Hinglish").click()
    page.wait_for_function("() => document.querySelector('[data-testid=language] button[aria-pressed=true]').innerText === 'Hinglish'")
    assert httpx.get(f"{engine}/dev/sessions/{sid}").json()["lang"] == "hi"
    page.get_by_test_id("next-step").filter(has_text="Battery connector ka VBAT pin").wait_for()  # labels follow the language


def test_calibration_drag_preview_and_save(page, engine):
    sid = "se_ui_cal"
    open_screen(page, engine, sid)
    started(engine, sid)
    page.get_by_test_id("open-calibration").click()
    page.get_by_test_id("cal-reading").filter(has_text="4.00").wait_for()
    crop = page.get_by_test_id("cal-crop")
    before = crop.get_attribute("src")
    h = page.get_by_test_id("handle-0").bounding_box()
    page.mouse.move(h["x"] + h["width"] / 2, h["y"] + h["height"] / 2)
    page.mouse.down()
    page.mouse.move(h["x"] + 30, h["y"] + 12, steps=6)
    page.mouse.up()
    page.wait_for_function("(b) => document.querySelector('[data-testid=cal-crop]').src !== b", arg=before)
    page.get_by_test_id("save-calibration").click()
    page.get_by_test_id("saved").wait_for()
    cal = httpx.get(f"{engine}/screen/calibration?session={sid}").json()
    tl = next(r for r in cal["rois"] if r["name"] == "supply.set_voltage")["corners"][0]
    assert tl[0] > 0 and httpx.get(f"{engine}/screen/calibration?session={sid}").status_code == 200


def test_demo_mode_hides_dev_controls_and_the_latency_dashboard(page, engine):
    sid = "se_ui_demo"
    open_screen(page, engine, sid)
    started(engine, sid)
    page.get_by_test_id("latency").wait_for()
    assert page.get_by_test_id("open-calibration").count() == 1
    open_screen(page, engine, sid, "&demo=1")
    page.get_by_test_id("supply-volts").wait_for()
    assert page.get_by_test_id("latency").count() == 0 and page.get_by_test_id("open-calibration").count() == 0


def test_themes_large_values_and_words_with_every_colour(page, engine):
    sid = "se_ui_theme"
    open_screen(page, engine, sid)
    started(engine, sid)
    page.get_by_test_id("supply-volts").wait_for()
    theme = page.get_by_test_id("theme")
    theme.get_by_role("button", name="light").click()
    light_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    theme.get_by_role("button", name="dark").click()
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") != light_bg
    size = float(page.get_by_test_id("supply-volts").evaluate("e => getComputedStyle(e).fontSize").rstrip("px"))
    assert size >= 40  # readable at about a metre
    words = page.locator(".status-word").all_inner_texts()
    assert words and all(w.strip() for w in words)


def test_readback_and_safety_stop_banners(page, engine):
    sid = "se_ui_banners"
    open_screen(page, engine, sid)
    started(engine, sid)
    tool(engine, sid, "record_spoken_reading", text="zero point zero two volt", point_id="vbat_conn")
    page.get_by_test_id("banner-readback").filter(has_text="zero point zero two").wait_for()
    drive(engine, sid, {"type": "transcript", "role": "user", "text": "the battery is swollen", "t_ms": 9})
    page.get_by_test_id("banner-stop").wait_for()
    assert page.get_by_test_id("session-state").inner_text() == "STOPPED"


def test_close_out_links_the_record_and_a_replay_shows_the_session_again(page, engine):
    sid = "se_ui_replay"
    open_screen(page, engine, sid)
    drive(engine, sid, {"type": "transcript", "role": "user", "text": "Demo phone hai, dead hai", "t_ms": 1})
    started(engine, sid)
    tool(engine, sid, "get_reading", kind="meter", point_id="vbat_conn")
    closed = tool(engine, sid, "close_repair", result="unresolved", note="ran out of time")
    banner = page.get_by_test_id("banner-record")
    banner.wait_for()
    assert closed["record"] in banner.inner_text()
    href = page.get_by_test_id("record-report").get_attribute("href")
    pdf = httpx.get(f"{engine}{href}", timeout=30)
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    assert httpx.get(f"{engine}{page.get_by_test_id('record-assessment').get_attribute('href')}", timeout=30).status_code == 200
    # the demo backup: the same session through the screen again, no voice, no API
    page.goto(f"{engine}/?screen=1&replay={sid}&speed=40&demo=1")
    page.get_by_test_id("banner-replay").filter(has_text="finished").wait_for(timeout=20000)
    assert page.get_by_test_id("supply-volts").inner_text() == "4.00"
    assert page.get_by_test_id("meter-class").inner_text() == "SHORT"
    assert "Demo phone hai" in page.get_by_test_id("conversation").inner_text()
    assert page.get_by_test_id("banner-record").count() == 1 and page.locator("[data-testid^='fault-']").count() >= 3
    page.wait_for_function("() => document.querySelector('[data-testid=reference]')?.naturalWidth === 640")
