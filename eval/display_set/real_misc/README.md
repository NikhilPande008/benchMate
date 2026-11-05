# Real phone photos of seven-segment displays (`real_misc`)

30–50 photos of any seven-segment displays (meters, supplies, clocks, appliances), taken with a
phone. They test Reader B on real optics, real fonts and real lighting. They are **never used for
tuning**: the reader is frozen before it runs on this folder, and results are reported apart from
the synthetic set.

The images are git-ignored by default (phone photos can carry GPS location in their EXIF data).
Only this README and the labels file are tracked. Remove the ignore lines in `.gitignore` if you
decide to publish the photos, after stripping EXIF.

## Labels: `labels.csv` (or `labels.jsonl` with the same keys)

```csv
file,text,corners,digits,polarity,readable,notes
IMG_0012.jpg,12.34,,4,dark,yes,multimeter in shade
IMG_0013.jpg,0.352,410 220 980 230 975 470 405 460,4,dark,yes,
IMG_0014.jpg,04.00,,4,light,yes,red LED supply
IMG_0015.jpg,OL,,4,dark,yes,
```

| Column | Meaning |
| --- | --- |
| `file` | Image file in this folder (JPEG or PNG) |
| `text` | What the display shows: digits, decimal point and minus only (`12.34`, `0.352`, `04.00`, `-1.5`, `OL`). Leave blank cells out |
| `corners` | Optional. Corners of the digit row, clockwise from top-left: `x1 y1 x2 y2 x3 y3 x4 y4` in pixels. Leave empty if the photo is already cropped to the digit row (easiest: crop it in the Photos app) |
| `digits` | Optional. Number of digit positions in that area (blank ones included). Default: the digits in `text` |
| `polarity` | Optional. `dark` = dark digits on a light background (LCD); `light` = lit digits on a dark background (LED). Default: detected |
| `readable` | Optional `yes`/`no`: whether you can read it yourself. Default `yes` |
| `notes` | Anything |

Run: `uv run python eval/display_set/run_vision_eval.py --real-misc`
