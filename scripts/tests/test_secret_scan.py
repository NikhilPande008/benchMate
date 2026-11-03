from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("secret_scan", Path(__file__).parents[1] / "secret_scan.py")
secret_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(secret_scan)

# Built at runtime so this file itself never contains a key-shaped string.
FAKE_OPENAI = "sk-" + "proj-" + "A1b2C3d4" * 5
FAKE_EPHEMERAL = "ek_" + "abc123XYZ" * 3


def test_flags_openai_keys_and_client_secrets():
    assert secret_scan.scan([("a.py", f"key = '{FAKE_OPENAI}'")])
    assert secret_scan.scan([("b.ts", f"const s = '{FAKE_EPHEMERAL}'")])


def test_clean_text_passes():
    assert secret_scan.scan([("README.md", "Set OPENAI_API_KEY in .env")]) == []


def test_allow_marker_skips_line():
    assert secret_scan.scan([("t.py", f"x = '{FAKE_OPENAI}'  # secret-scan: allow")]) == []


def test_forbidden_paths_blocked_regardless_of_content():
    for path in [".env", ".env.local", ".claude/settings.json", "data/benchmate.sqlite", "frames/a.jpg", "x.sqlite"]:
        assert secret_scan.scan([(path, "")]), path
    assert secret_scan.scan([(".env.example", "OPENAI_API_KEY=")]) == []


def test_leading_dot_slash_paths_are_normalised():
    assert secret_scan.scan([("./.env.example", "OPENAI_API_KEY=")]) == []
    assert secret_scan.scan([("./.env", "")])
