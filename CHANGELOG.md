# Changelog

## 1.2.0 — narration pipeline
- New [`speechkit_narrate`](speechkit_narrate/) package ([docs/NARRATION.md](docs/NARRATION.md)): the narration part of
  EMBLEM-NLP's Bookcast toolkit 2.3.0, refactored to stand on SpeechKit alone (no PDF, video, web reader, print or
  packaging). Stages extract, check, script, prevoice, narrate, autofix, audit, report and the new export; tools
  speak and quick; CLI, MCP server and the PowerShell module [`SpeechKitNarrate`](powershell/).
- Synthesis uses SpeechKit's sentence split, 0.38 s pauses, pace retry, volume and signal gate, plus token timings
  mapped from model phoneme durations, workers that share segments longest first, and resumable runs. Estimated
  token spans carry quality flags. At the pace ceiling the narration driver retains a take with review required;
  the original standalone CLI still raises its pace error.
- New `export` stage: one audio file per chapter (wav, or mp3/m4a through ffmpeg) with word timings and a SHA-256
  manifest (`status: NEURAL_AUDIO_GENERATED_REVIEW_PENDING`).
- New `spoken.code_names`: names from source code read the way a programmer says them; Markdown code blocks are kept
  and not read (`ingest.code_blocks`).
- Inputs: .txt, .md, or a SpeechKit request .json.
- [`emblem_rsi`](emblem_rsi/), the sealed-rubric loop behind the pronunciation audit, ships beside the pipeline.
  SpeechKit's own CLI (`speechkit/`) stays standalone and does not import either package.

Narration integration corrections:
- Segment reuse verifies synthesis identity, complete PCM bytes, hashes, sample counts and token records. Model,
  profile and renderer changes invalidate affected audio; missing or corrupted audio is regenerated.
- Export validates every manifest-owned artifact and publishes a staged complete result while preserving unrelated
  files. Stage cache identity includes declared helper dependencies and output-affecting options.
- Cross-process project writer locks protect stages and settings. Delegated execution refuses recursive delegation;
  the simulated WSL fixture explicitly selects Linux when tests run on macOS.
- Recognizer archives are hash-pinned and installed through staging with verified member receipts. The audit declares
  its resampler and keys recognition caches by model and decoder identity. Confirmation coverage is selective and
  reported separately from primary coverage; generated audio remains pending listening review.
- `spoken.read_cover=false` supports an external narration sequence without adding title speech. The explainer example
  retains source cue identities through a validated sidecar and exports native segment artifacts for a compositor.
- CI verifies source checksums, a clean installed wheel, supported core Python environments and the Linux voice path;
  the release workflow includes the narration wheel and source package alongside the unchanged runtime archive.

- PowerShell module `SpeechKitNarrate` 1.0.0 (the Bookcast module, renamed: `*-Bookcast*` cmdlets are now
  `*-Narration*`, e.g. `Invoke-NarrationPipeline`, `New-NarrationProject -Set`, `Set-NarrationBackend Auto`); 70
  Pester tests.

Fixes carried over from a line-by-line review of the Bookcast code (narration parts only):
- Editing the text no longer re-voices every later segment: a segment whose spoken text is unchanged keeps its audio
  when earlier edits renumber its tokens or its segment id; the record is renumbered.
- A sentence-opening "A" (and other one-letter words) gets its own highlight time: its sound is looked up in its
  sentence, where espeak reads the article, not the letter name.
- A stage that was killed, or died before it could record its failure (a full disk), is rebuilt on the next run
  instead of having its half-made outputs adopted as current.
- `run --from all` (and "all" as a target, `--only` or `--skip`) is a clear usage error instead of a ValueError.
- A null section in `project.json` keeps that section's defaults; `settings --set` refuses an update that would break
  the project instead of saving it; `new` checks the input before creating anything.
- `text_fixes` values are inserted literally (a backslash is no longer a regex group reference); quote balancing
  keeps the apostrophe of an elided word (goin’) inside dialogue.
- FIGURE and LINK lines split at " | " as documented, so a bar inside an address stays in it; a day after a
  free-standing dash ("Nov 10 – 11") is read as a date.
- The pronunciation page lists each word once; MCP `project_new` reports a missing `input` clearly without
  jsonschema, and tool calls wait as long as the client asked.
- emblem_rsi:
  - heteronym candidates are built per occurrence and reading, so a later occurrence that needs the other reading is
    scored against the right variants;
  - `arpabet_to_ipa` raises ValueError for any malformed symbol;
  - trial synthesis can no longer deadlock on a full stdout pipe;
  - a rubric without a name is refused when sealing (one-line error, exit status 2);
  - `pron revert` run twice goes one apply further back instead of restoring the same snapshot;
  - trial jobs drop duplicate candidates;
  - code audit: `apply` with no files no longer fails, a top-level `fixtures/` folder is skipped, XML comments are
    never placed above the declaration, and `trailing_text` returns "" for a line without a comment;
  - no developer paths remain: settings come from the EMBLEM_* variables the pipeline sets, with clear errors when
    one is missing; the code audit's list of text forbidden in template comments is now configurable
    (`EMBLEM_CODE_FORBIDDEN_TEMPLATE`) instead of naming one book.
- PowerShell:
  - `Resolve-NarrationPython` caches the Python it finds on Windows (its search loop no longer overwrites a
    parameter of the same name), and an explicit `SPEECHKIT_PYTHON` / `-Python` always wins;
  - `Set-NarrationBackend` validates every value before applying any, so a bad `-ToolkitRoot` changes nothing;
  - `Register-NarrationMcpServer` leaves no `.tmp` file behind when the final copy fails;
  - the JSON depth check tests PowerShell 6.2, not 6; steps skipped under `-WhatIf` or a declined `-Confirm` are
    reported with status `skipped`.

## 1.1.0 — standalone release
- Repository is now a standalone tool: the narration CLI lives in [`speechkit/`](speechkit/); the Video Factory, RSI adapter and OBC Study Orchestrator are no longer bundled or referenced. Integration is via [`docs/INTEROP.md`](docs/INTEROP.md) only.
- Licensed GPL-3.0-or-later (Piper/espeak-ng are GPL and imported in-process); see [`docs/LICENSING.md`](docs/LICENSING.md). SPDX headers added; plugin metadata updated.
- [`bootstrap.py`](bootstrap.py): single pinned runtime archive; new `fetch` command (HTTPS, SHA-256 enforced); install no longer extracts a tools archive. End-to-end install and 13.5 s fresh synthesis verified.
- Voice, model, wheels and runtime archive are byte-identical to prior distributions (same SHA-256 pin).
- Tests: standalone-independence check, license check, fetch negative controls.
