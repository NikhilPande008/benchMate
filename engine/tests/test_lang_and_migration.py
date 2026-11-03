from __future__ import annotations

import sqlite3

import pytest

from engine.telemetry import lang_tag, make_engine


@pytest.mark.parametrize(
    "text, tag",
    [
        ("मीटर पढ़ो।", "hi"),
        ("डायोड mode में मीटर voltage drop दिखाता है।", "hi"),
        ("Supply kitna dikha raha hai?", "hinglish"),
        ("VBAT matlab battery ki main supply line.", "hinglish"),
        ("Zero point three five volts, diode mode.", "en"),
        ("Do you want me to read the meter?", "en"),
        ("", "unknown"),
    ],
)
def test_lang_tag(text, tag):
    assert lang_tag(text) == tag


def test_old_database_gets_new_columns(tmp_path):
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE realtime_sessions (id VARCHAR PRIMARY KEY, run_id VARCHAR, model VARCHAR, prompt_version VARCHAR, created_ms FLOAT)")
    con.commit()
    con.close()
    make_engine(db)
    cols = {r[1] for r in sqlite3.connect(db).execute("pragma table_info(realtime_sessions)")}
    assert {"turn_mode", "prompt", "max_output_tokens", "secret_source"} <= cols
