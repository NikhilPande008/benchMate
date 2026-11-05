# Display set

Images of instrument displays for testing the readers (Reader A, vision model; Reader B, local
seven-segment OCR) and the two-frame agreement rule.

## `synthetic/` (generated, git-ignored)

```bash
uv run python -m mcp_evidence.synth --out eval/display_set/synthetic --samples 700 --seed 7
```

- 700 samples × 3 frames (0, 150, 300 ms) = 2,100 JPEG images, reproducible from the seed
  (sample *i* depends only on the seed and *i*).
- Displays:
  - a 4-digit multimeter LCD with mode icons (AUTO, DC, diode, Ω), unit (V, mV, A, mA, Ω, kΩ,
    MΩ), OL, and a low-battery icon;
  - a bench supply with V and A rows plus a current-limit row and CV/CC lamps (LED, or LCD on
    about 20% of samples).
  Fonts, slant, stroke, spacing, colours, ghost segments and glyph variants (6, 7, 9) are
  randomised per sample.
- Distortions (one per sample, severity 0.3–1.0):
  `clean`, `perspective`, `blur`, `glare`, `low_contrast`, `flicker` (segments missing in one
  frame), `mid_update` (old and new digits at once, settling by the last frame), `hand` (a hand
  or finger over part of the display), `dim_lcd` (meter low battery), `noise` (sensor noise and
  heavy JPEG).
- `labels.jsonl`, one line per sample:
  - `truth`: per field, the settled value as displayed (`text`, `value`, `decimals`, `ol`); for
    the meter also `mode`, `unit`, `battery_low`; for the supply `cc`;
  - `frame_truth`: what each frame shows (`flicker`, `mixed`, or the text);
  - `glass`, `fields` and `cells`: corners in image pixels (the calibration ground truth);
  - `readable`: whether a careful person could read each field (false when a hand covers a digit,
    glare saturates one, or the blur or dimming is extreme).
- `meta.json`: seed, counts and generator version.

**These are synthetic stand-ins.** Results on them show that the pipeline works; they are not a
measure of accuracy on the real supply and meter. Label every number from this set as synthetic.

## `real/` (to come)

Photos of the actual supply and meter, about 200, labelled the same way. See `docs/PENDING_RUNS.md`.
