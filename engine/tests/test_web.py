"""The browser's SDK pin, the data-channel stall test and the power guardrail, inside the Python suite.

agents-realtime is pinned at exactly 0.21.0 (CLAUDE.md, docs/NOTES.md): 0.21.0 requests every
transcribed user item, which stalled the WebRTC event channel for 2-6 s, and the page drops that
request. An upgrade must pass apps/web/src/realtime/channelFilter.test.ts (run here) first.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "apps" / "web"
PINNED = "0.21.0"


def test_agents_sdk_is_pinned_exactly():
    deps = json.loads((WEB / "package.json").read_text())["dependencies"]
    assert deps["@openai/agents-realtime"] == PINNED  # exact: no ^ or ~
    lock = json.loads((WEB / "package-lock.json").read_text())["packages"]
    assert lock["node_modules/@openai/agents-realtime"]["version"] == PINNED
    assert lock["node_modules/@openai/agents-core"]["version"] == PINNED
    assert lock["node_modules/@openai/agents-realtime"]["dependencies"]["@openai/agents-core"] == PINNED


needs_node = pytest.mark.skipif(shutil.which("node") is None or not (WEB / "node_modules" / "@openai").is_dir(),
                                reason="needs node and apps/web/node_modules")


@needs_node
@pytest.mark.parametrize("test_file", ["src/realtime/channelFilter.test.ts", "src/realtime/powerGuard.test.ts"])
def test_web_unit_tests_pass(test_file):
    run = subprocess.run(["node", "--test", test_file], cwd=WEB, capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]
    assert "fail 0" in run.stdout


def _gate_sentences() -> tuple[dict[str, dict[str, str]], list[str]]:
    """The gate's approved sentences by step, and every block sentence it can say."""
    from datetime import timedelta

    from engine.safety import APPROVED, check_power
    from mcp_evidence.packs import load_pack
    from mcp_evidence.schema import EvidenceRecord, FieldValue, utcnow

    pack = load_pack("demo_model")

    def supply(v=4.0, lim=1.0, vc=0.95, lc=0.95, age=0.0, volts=True):
        fields = {"current_limit": FieldValue(value=lim, unit="A", confidence=lc)}
        if volts:
            fields["set_voltage"] = FieldValue(value=v, unit="V", confidence=vc)
        return EvidenceRecord(session_id="s", kind="supply", source="camera", confidence=min(vc, lc), fields=fields,
                              created_at=utcnow() - timedelta(seconds=age))

    cases = [
        ("nope", supply(), "removed", None), ("connect_boot_cable", supply(), "removed", "stopped"),
        ("connect_boot_cable", supply(), "connected", None), ("connect_boot_cable", None, "removed", None),
        ("connect_boot_cable", supply(age=30), "removed", None), ("connect_boot_cable", supply(volts=False), "removed", None),
        ("connect_boot_cable", supply(vc=0.5), "removed", None), ("connect_boot_cable", supply(lim=0), "removed", None),
        ("connect_boot_cable", supply(lc=0.5), "removed", None), ("connect_boot_cable", supply(v=5.0), "removed", None),
        ("connect_boot_cable", supply(lim=3.0), "removed", None),
    ]
    blocked = []
    for step, sup, battery, stopped in cases:
        res = check_power(step, sup, pack, battery, stopped)  # type: ignore[arg-type]
        assert not res.approved
        blocked += list(res.say.values())
    return APPROVED, blocked


@needs_node
def test_the_guardrail_knows_every_engine_sentence():
    """Approved gate sentences are instructions (caught without approval, passed while allowed); block
    sentences and the engine's safety messages are never caught."""
    from engine.safety import DISCONNECT_NOW, REFUSE_DEVICE, STOP_MESSAGE

    approved, blocked = _gate_sentences()
    harmless = blocked + list(STOP_MESSAGE.values()) + list(DISCONNECT_NOW.values()) + list(REFUSE_DEVICE.values())
    script = """
      import { findViolation } from './src/realtime/powerGuard.ts';
      const { approved, harmless } = JSON.parse(process.argv[1]);
      const out = { unapproved: {}, approved: {}, harmless: {} };
      for (const [step, say] of Object.entries(approved)) for (const s of Object.values(say)) {
        out.unapproved[s] = findViolation(s)?.kind ?? null;
        out.approved[s] = findViolation(s, Object.values(say))?.kind ?? null;
      }
      for (const s of harmless) out.harmless[s] = findViolation(s)?.kind ?? null;
      console.log(JSON.stringify(out));
    """
    run = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps({"approved": approved, "harmless": harmless})],
                         cwd=WEB, capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-2000:]
    out = json.loads(run.stdout)
    for step, say in approved.items():
        for s in say.values():
            assert out["unapproved"][s] == step, (s, out["unapproved"][s])
            assert out["approved"][s] is None, s
    assert {s: k for s, k in out["harmless"].items() if k} == {}
