# Model packs

`general_dead_phone.json` and one folder per phone model (`<model_id>/pack.json`; later also photos and `boot_ref.json`).
Packs contain only our own reference measurements and observed faults: no customer data, no schematics (CLAUDE.md rule 8).

- **Schema:** `mcp_evidence/packs/schema.py` (version 1, `schema_version`). The exported JSON Schemas are in `packs/schema/`. Regenerate them with `uv run python scripts/export_schemas.py`; a test fails if they are stale.
- **Placeholders:** every object that carries a number has `"placeholder": true|false`. `demo_model` is placeholder throughout: its structure is real, its numbers are invented. Replace them with measurements from the reference phone before trusting any result.
- **Healthy class:** each measurement names the class a healthy board gives (`healthy`; points default to `normal`, the boot pattern to `NORMAL_BOOT`, port current to `normal`). Observations must name it. A fix is accepted only when the reading that pointed at the fault comes back healthy.

## Completeness rule

A fault must predict `"normal"` (the healthy class) at every point where it leaves the board healthy.

A missing prediction means "no opinion" and scores 1/K for any reading. A spelled-out `"normal"` scores c + (1−c)/K on a normal reading. So on readings that say nothing about the real cause, the fault that spells out more normal readings wins. With `power_key_line`'s normal predictions removed, five normal readings on a power-key board put `bad_boot_cable` in the lead, even though the cable reads OK.

Leave a prediction out only when the fault really says nothing about the measurement. In both packs the only such gap is charging-port current for dead-phone faults.

Enforced by `engine/tests/test_packs.py`:
- `test_every_fault_predicts_every_point`
- `test_only_port_current_may_be_left_without_a_prediction`
- `test_a_gap_in_the_predictions_picks_the_wrong_fault` (the failure the rule prevents)
