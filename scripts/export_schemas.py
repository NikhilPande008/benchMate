"""Write the JSON Schemas for model packs and evidence records.

    uv run python scripts/export_schemas.py

engine/tests/test_packs.py fails if the committed files are out of date.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_evidence.packs.schema import SCHEMA_VERSION, Pack  # noqa: E402
from mcp_evidence.schema import EvidenceRecord  # noqa: E402

OUT = ROOT / "packs" / "schema"
COMPLETENESS_RULE = (
    "Completeness rule: a fault must predict the healthy class ('normal') at every point where it "
    "leaves the board healthy. A missing prediction means 'no opinion' (1/K for any reading), so "
    "faults that spell out their normal readings win on readings that say nothing about the real "
    "cause. Leave a prediction out only when the fault says nothing about the measurement. "
    "Enforced by engine/tests/test_packs.py::test_every_fault_predicts_every_point."
)


def schemas() -> dict[str, dict]:
    pack = Pack.model_json_schema(by_alias=True)
    pack["$id"] = f"https://benchmate.local/schema/pack.v{SCHEMA_VERSION}.json"
    pack["$comment"] = COMPLETENESS_RULE
    evidence = EvidenceRecord.model_json_schema()
    evidence["$id"] = "https://benchmate.local/schema/evidence.json"
    return {f"pack.v{SCHEMA_VERSION}.schema.json": pack, "evidence.schema.json": evidence}


def render(schema: dict) -> str:
    return json.dumps(schema, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, schema in schemas().items():
        (OUT / name).write_text(render(schema), encoding="utf-8")
        print(OUT / name)


if __name__ == "__main__":
    main()
