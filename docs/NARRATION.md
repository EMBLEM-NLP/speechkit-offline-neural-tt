# SpeechKit Voice TTS Offline Pipeline

Canonical package and command: `speechkit-voice-tts-offline-pipeline`. The `speechkit-narrate` command and `python -m speechkit_narrate` remain supported. See [the rename and installation guide](RENAMING.md).

`speechkit_narrate` turns a text into a finished, checked narration with the SpeechKit voice:

```
.txt / .md / request.json ──extract──▶ draft.md ──check──▶ book.md ──script──▶ request.json
   ──prevoice (opt-in)──▶ narrate (N workers, resumable) ──▶ autofix (pronunciation loop) ──▶ report
   ──▶ export: out/narration/<slug>_ch-NN.wav|mp3|m4a + <slug>_ch-NN.words.json + manifest.json
```

It uses SpeechKit's own voice code unchanged (the voice runtime, the pinned model and `speechkit/src/voice.py`) and
adds what a long narration needs: word timings, parallel resumable voicing, a pronunciation audit that hears every
word back and fixes what was misheard, incremental runs and per-chapter export.

## What it adds to SpeechKit

**Synthesis** uses the base `voice.py synthesize` sentence split, 0.38 s sentence pauses, profile pacing retries, volume 0.8 and speech signal gate.
Unlike the base CLI, a take still over the pace limit is retained with `paceReview: REQUIRED`; it is not approved.
On top of that:

- **Word timings.** The voice is loaded from a copy of the pinned model with one extra output, the per-phoneme
  durations (same weights; built once into the cache). Each segment's record (`work/speech/sNNNN.wav.json`) carries
  token spans derived from that waveform. Token matching can be approximate: `alignmentQuality` records
  per-piece match ratios and duration discrepancies; every fallback word span has `estimated: true`.
  Whole-cue captions are supported without claiming precise word alignment. Review flagged spans before using word highlights.
- **Several workers that share the work.** `voice.workers` processes take the next free segment, longest first, so
  no worker sits idle while another finishes.
- **Resumable.** A finished segment is reused only when its audio, timing record and synthesis identity validate. After an edit,
  a segment whose spoken text is unchanged keeps its audio even when an earlier edit renumbered it; a change that
  gives the voice identical phonemes ("Gods" → "God's") only updates the record's text.

**Pronunciation**

- **`prevoice`** (opt-in: `"prevoice": {"enabled": true}`) fixes heteronyms that espeak would read in the wrong
  role for their sentence ("close" the verb read as the adjective) before anything is voiced.
- **`autofix`** sends every present segment to a NeMo CTC recogniser, then sends segments containing flagged,
  near-match or watched words to Parakeet TDT for confirmation. Function words contribute to WER but are not flagged.
  It checks every word against the CMU dictionary and for r-sounds the spelling does not have,
  proposes respellings, tries each one in its real sentences (several takes, both recognisers), applies the winners,
  re-voices only the changed segments, measures again and promotes or vetoes the round under a sealed rubric. Words
  still misheard get extra takes and the best take is kept. Fixes are written to `work/pronunciations.json`, which
  you can edit by hand.
- **`report`** writes `work/rsi/pron/report/Pronunciation_Audit.html`: every misheard word, the fix and where it
  occurs.

**Running it**

- **Stages that run only what changed.** Each stage's code, settings and input files are fingerprinted; `run`
  brings everything up to date and skips the rest. A stage whose last run failed, or was killed before it could
  record a result, is always rebuilt (its outputs are never taken as current).
- **A PowerShell module** (`powershell/SpeechKitVoiceTtsOfflinePipeline`) and **an MCP server** (`python -m speechkit_narrate mcp`)
  drive the same commands; see [`powershell/README.md`](../powershell/README.md).
- **Platforms.** The pipeline runs on Python 3.11 or newer on any platform. The voice side (prevoice, narrate,
  autofix, audit, the `speak` tool) needs the Linux x86-64 / CPython 3.13 runtime; on Windows it runs in WSL
  (`SPEECHKIT_DELEGATE`, or `Set-NarrationBackend Auto` in PowerShell).

**Code read aloud.** `"spoken": {"code_names": true}` reads names from source code in the prose the way a
programmer says them: `emblem_rsi/pron/asr.py` → "emblem R S I slash pron slash A S R dot pie", `--accept-draft`
→ "dash dash accept draft", `HTMLParser` → "H T M L parser", `Get-ChildItem` → "get child item". Fenced code blocks
in Markdown stay in the text and are not read (`"ingest": {"code_blocks": "read"}` reads them as a paragraph).

## Quick start (PowerShell)

```powershell
# once: the voice runtime (Linux or WSL) and the Python packages
python bootstrap.py fetch; python bootstrap.py verify; python bootstrap.py install --out .\installed
pip install -e ".[voice,mcp]"
python -m speechkit_narrate setup        # verified recogniser archives/members and the aligned voice cache
python -m speechkit_narrate doctor

# a narration
python -m speechkit_narrate new .\projects\guide --input .\guide.md --title "The Guide" --set '{"spoken": {"code_names": true}}'
python -m speechkit_narrate run .\projects\guide      # stops after check: review work\book.md, then run again
python -m speechkit_narrate run .\projects\guide      # narrate, autofix, report, export
python -m speechkit_narrate status .\projects\guide
```

Use `run --accept-draft` to go straight through. On Windows the same commands are cmdlets:

```powershell
Import-Module .\powershell\SpeechKitVoiceTtsOfflinePipeline
Set-NarrationBackend Auto
New-NarrationProject .\projects\guide -Input .\guide.md -Title 'The Guide'
Invoke-NarrationPipeline .\projects\guide -AcceptDraft
```

## Commands

| Command | What it does |
|---|---|
| `new DIR --input FILE [--title --subtitle --author --set JSON]` | make a project folder |
| `run DIR [STAGE ...] [--force S\|all] [--only S] [--skip S] [--from S] [--accept-draft] [--options JSON] [--dry-run]` | bring stages up to date |
| `plan DIR [STAGE ...]` | what `run` would do, and why |
| `status DIR` | what is done, narration progress, exported files |
| `settings DIR [--set JSON] [--effective]` | show or change `project.json` |
| `<stage> DIR [options]` | run one stage now (e.g. `narrate DIR --workers 4`, `export DIR --format mp3`) |
| `pron DIR trial\|apply\|retake\|revert\|status\|measure\|decide ...` | the pronunciation loop's own commands |
| `tool speak --text "..." --out a.wav` | narrate a piece of text into one file plus word timings, no project |
| `tool quick --input FILE --out DIR [--targets narrate,export]` | any stages on any file |
| `stages`, `tools`, `doctor`, `setup`, `mcp` | the registry, the machine, the MCP server |

Add `--json` anywhere for one JSON object on stdout (logs on stderr).

## Stages

| Stage | Reads | Writes |
|---|---|---|
| `extract` | the input (.txt, .md, request .json) | `work/draft.md`, `work/extract/report.json` (words to review, repairs) |
| `check` | `work/draft.md` | `work/book.md` (once; then it is yours to edit). Stops the first full run for review |
| `script` | `book.md`, `pronunciations.json` | `work/docmodel.json` (words and their spoken forms), `work/request.json` |
| `prevoice` | `book.md` | `work/prevoice/` heteronym fixes (opt-in) |
| `narrate` | `request.json` | `work/speech/sNNNN.wav` + `.wav.json` (timings) |
| `autofix` | `request.json`, `speech/` | `work/pronunciations.json`, re-voiced segments, `work/rsi/pron/` |
| `audit` | (by name only) one measured step without fixes, after hand edits | `work/rsi/pron/measure-NNN.json` |
| `report` | `work/rsi/pron/` | `Pronunciation_Audit.html` |
| `export` | `docmodel.json`, `speech/` | `out/narration/` (see below) |

## The text format (`work/book.md`)

Blocks are separated by a blank line: `# Chapter`, `## Section`, `### Sub-section`, `> quote`, `| verse line`,
`- bullet`, `* 1. numbered item`, `TABLE a | b`, `ROW a | b`, `NOTE text`, `LINK title | https://…`, a fenced
code block, anything else is a paragraph. A Markdown input is converted to this on `extract`; a plain `.txt` is
read with its headings, lists and tables recognised. A SpeechKit request `.json` becomes one paragraph per segment
(its `spokenText`); a segment whose id starts with `h1-` or `chapter` starts a chapter.

## Settings (`project.json`)

Only what differs from the defaults is stored. The main ones:

| Setting | Default | Meaning |
|---|---|---|
| `voice.profile`, `voice.workers` | `ljspeech-high`, 2 | SpeechKit profile and parallel voice processes |
| `spoken.say` | `[]` | per-word reading rules: `["regex", "spoken"]` or `{"match", "say", "prev", "next", "spell_digits"}` |
| `spoken.code_names` | false | read names from source code as a programmer says them |
| `spoken.dates`, `ordinal_days`, `years` | off | how numeric dates and years are read |
| `spoken.silent`, `silent_columns`, `read_links`, `table_reading` | | what is shown but not spoken, and how tables are read |
| `prevoice.enabled` | false | fix heteronyms before voicing |
| `chapters.markers`, `extra_titles` | `["h1"]` | what starts a chapter (one audio file each) |
| `export.format`, `kbps`, `normalize`, `lead`, `tail` | `wav`, 96, true, 0.6, 1.2 | export format (mp3/m4a need ffmpeg), loudness and silences |
| `ingest.code_blocks` | `keep` | `keep` code blocks unspoken, or `read` them |
| `text_fixes` | `{}` | whole-word replacements applied to the draft |

`python -m speechkit_narrate settings DIR --effective` lists every setting with its value.

## Export (`out/narration/`)

- `<slug>_ch-NN.wav` (or `.mp3` / `.m4a`): one file per chapter, segments laid out with natural pauses (0.5 s
  between paragraphs, 0.95 s before a section, 1.4 s before a chapter), one gain for the whole text (speech at
  -19 dBFS RMS, peaks at most 0.97), mono 22050 Hz.
- `<slug>_ch-NN.words.json`: every spoken word with `word`, `start`, `end` (seconds in that chapter file) and its
  `sentence` number — enough to drive captions or a read-along highlight.
- `<slug>_script.md`: the text that was narrated.
- `manifest.json`: title, voice identity (profile, model SHA-256), every chapter with its length, word count and
  files, every file's SHA-256, the last pronunciation loop's summary, and `status:
  NEURAL_AUDIO_GENERATED_REVIEW_PENDING` / `voiceApproval: PENDING` as in SpeechKit's own `speech.json`: synthesis
  and an automatic audit are not a human approval.

## Locations

| Variable | Default |
|---|---|
| `SPEECHKIT_CHECKOUT` | the checkout this package sits in |
| `SPEECHKIT_RUNTIME` | `<checkout>/installed/runtime` |
| `SPEECHKIT_ASR_MODELS` | `<SPEECHKIT_HOME>/models` (`setup` downloads them) |
| `SPEECHKIT_HOME` | `%LOCALAPPDATA%\speechkit-narrate` on Windows, `~/.local/share/speechkit-narrate` elsewhere |
| `SPEECHKIT_CACHE` | aligned voice model, scratch projects, MCP job logs |
| `SPEECHKIT_DELEGATE` | command prefix that runs Linux-only stages elsewhere, e.g. `wsl -d Ubuntu-24.04 --exec` |
| `SPEECHKIT_PLUGINS` | extra stage/tool modules (also `--plugin`, `project.json` `plugins`, or a project's `plugins/` folder) |

## Origin

The pipeline is the narration part of EMBLEM-NLP's Bookcast toolkit (2.3.0), refactored to stand on SpeechKit
alone: the PDF reader, video, web reader, print and packaging stages were left out, and the fixes found in a
line-by-line review of that code are applied (see the changelog).

## Reproducible Linux narration

For the voice/audit worker use a Linux x86-64 CPython 3.13 virtual environment and install
`pip install -r requirements-voice.txt -c constraints-voice.txt`. The constraints pin direct dependencies;
retain the job's complete `pip freeze` and installation report with its artifacts. The separately bootstrapped
neural runtime retains its own pinned Piper and ONNX Runtime environment. SciPy is required for audit resampling;
FFmpeg is only a fallback for non-PCM input and is needed for compressed export.

Set project-scoped `SPEECHKIT_RUNTIME`, `SPEECHKIT_ASR_MODELS` and `SPEECHKIT_CACHE` locations before setup.
The packaged `asr-models.lock.json` pins each complete archive. CTC member hashes are included in the lock;
Parakeet required-member hashes are derived only after archive verification and saved in an installation receipt.
Downloads are checked before staged extraction and publication; a damaged existing model is reported and must be moved aside before setup.
Model folders containing only `tokens.txt` are not accepted as complete. Derived voice graphs have receipts
binding the original model, patch version and derived bytes.

Recognition cache identities include model bytes, implementation, decoder settings and package versions.
A changed model or resampler invalidates cached recognition. The primary-listener coverage gate requires all
segments; selective confirmation coverage remains visible. Recognition scores, timing coverage, and successful
synthesis do not approve pronunciation or naturalness. All generated narration remains pending human review.
