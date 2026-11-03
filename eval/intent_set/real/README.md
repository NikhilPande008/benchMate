# Real recordings (your own voice)

Put about 20 short clips here: Hindi, English and mixed Hinglish, a mix of plain questions and
reading requests, and a few with a natural mid-sentence pause ("probe laga diya... ab meter padho").
Record with the headset you will use at the bench, in a normal room.

**Naming:** `<lang>-<kind>-<n>[-pause].<ext>`

- `lang`: `en`, `hi` or `mix` (Hinglish)
- `kind`: `plain` (a question the model answers) or `tool` (asks for a reading: "read the meter", "supply padho")
- add `-pause` if you paused mid-sentence on purpose

Examples: `hi-tool-01.m4a`, `en-plain-04.wav`, `mix-tool-02-pause.m4a`.

Any format macOS `afconvert` reads works (wav, m4a, mp3, aiff, caf, flac). Leave about half a second
of silence before and after the speech. If a name doesn't fit the pattern, add it to an optional
`labels.json`: `{"my clip.m4a": {"lang": "hi", "kind": "tool", "text": "meter padho"}}`.

Then:

```bash
uv run python eval/latency_script/make_clips.py real
```

```bash
uv run python eval/latency_script/run_experiments.py --set real --start-engine
```

The set is repeated as needed to reach 30 plain and 15 tool lines per configuration; the report
gives n and the number of unique clips. These files are git-ignored by default, because a voice
recording is personal data. Delete the two lines in `.gitignore` if you want to publish them.
