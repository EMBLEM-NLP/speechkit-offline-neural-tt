# Authored Blender explainer narration

These inputs preserve the original displayed narration and stable scene/cue IDs.
They contain only narration, not source-report paths or citations. `pilot.json`
contains scenes C02-S04, C02-S09 and C02-S11 (15 cues); `full.json` contains the
complete Chapter 2 narration (87 cues). Both use the same reviewed source text.
`glossary.json` is a separate eight-cue, 90-word audition fixture, not chapter text.

After installing the documented SpeechKit voice runtime, run from the repository:

```sh
python scripts/explainer_narration.py \
  --input-package examples/explainer_chapter02/pilot.json \
  --output-root artifacts/explainer_pilot \
  --profile ljspeech-high --workers 1 --audit
```

Use `--stage script` to check structure and cue identity without generating audio;
use `--stage validate` to check an existing result. The full input uses the same
command with `full.json` and a separate output folder.

`--audit` measures pronunciation without automatically proposing repairs. It reads
the adjacent `pronunciation_watchlist.json` (or explicit `--watchlist`), expands
acronyms through the document model's spoken forms, and requires complete primary
recognition plus second-recognizer coverage for every present watched token. The
saved audit summary distinguishes coverage from pronunciation approval. Optional
`--autofix` is separate and bounded to three words, two occurrences per word, one
take and zero retakes.

The adapter disables title narration, keeps original display spelling, verifies
ordered block/segment/token mappings, and checks completed WAV bytes against their
sealed timing records. Its artifacts include the native project, source sidecar,
cue map, raw-audio manifest, stage logs and final run status. Native chapter export
is also retained. Animation timing must be rebuilt from the actual segment samples;
native export pauses are not the explainer's scene timing.

`ljspeech-high` and the explicit JSON-to-Jason pronunciation are initial audition
settings. The pronunciation watchlist is a listening checklist, not a list of
automatically applied replacements. Technical validation does not grant voice or
pronunciation approval: those remain `PENDING` until human review.
