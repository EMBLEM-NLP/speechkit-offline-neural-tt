# speechkit-voice-tts-offline-pipeline

**SpeechKit Voice TTS Offline Pipeline by EMBLEM-NLP.** Offline neural text-to-speech and narration pipeline with pronunciation auditing, resumable synthesis, word timings and chapter exports. Combines a hash-pinned Piper/ONNX voice runtime with CLI, MCP and PowerShell interfaces. The runtime and optional recognizers are provisioned during setup; inference uses local models.

Version **1.3.0** (voice runtime unchanged since 1.1.0) · License **GPL-3.0-or-later** (see [docs/LICENSING.md](docs/LICENSING.md)) · Linux x86_64, CPython 3.13, glibc ≥ 2.28, ≥ 1.5 GB free.

The canonical Python package and command are `speechkit-voice-tts-offline-pipeline`; the existing `speechkit-narrate` command and `speechkit_narrate` imports remain supported. See [migration and compatibility](docs/RENAMING.md).

## Quick start

```sh
git clone https://github.com/EMBLEM-NLP/speechkit-voice-tts-offline-pipeline.git
cd speechkit-voice-tts-offline-pipeline
python3.13 bootstrap.py fetch                      # 232 MB runtime release asset, SHA-256 checked
python3.13 bootstrap.py verify                     # CRCs, member safety, internal SHA-256 ledgers
python3.13 bootstrap.py install --out ./installed  # new dir only; runs doctor + real synthesis
```

Within the base bootstrap, `fetch` is the network step and rejects anything that doesn't match the pinned hash. The runtime is a Release asset because it exceeds GitHub's 100 MB file limit; you can also place it manually in [`archives/`](archives/). Narration-pipeline dependency installation and recognizer setup are additional provisioning steps; the base runtime's storage requirement does not include those models.

## Synthesize

```sh
echo '{"schemaVersion":"1.0.0","id":"demo","segments":[{"id":"intro","text":"Follow the signal.","spokenText":"Follow the signal."}]}' > request.json
./installed/runtime/.venv/bin/python speechkit/src/voice.py synthesize \
  --runtime ./installed/runtime --profile speechkit/profiles/ljspeech-high.json \
  --script request.json --out ./speech-out
```

Output: `speech-out/speech.json` (text, hashes, sample counts) and one WAV per segment. Re-running into the same folder resumes only when request, profile and recorded WAV hashes agree. Use [`voice.py doctor`](speechkit/src/voice.py) to check imports and weights. The format is documented in [docs/INTEROP.md](docs/INTEROP.md), so any program (video renderers, course tools, game pipelines) can consume it.

Synthesis success is not a naturalness or pronunciation approval; `voiceApproval` stays `PENDING` until a human reviews it.

## Narrate a whole text: the narration pipeline

[`speechkit_narrate`](speechkit_narrate/) builds on the voice above to narrate long texts: word timings, parallel
resumable voicing that re-voices only what changed, a pronunciation audit that hears every word back with two
offline recognisers and fixes what was misheard, incremental stages, per-chapter export with a SHA-256 manifest,
reading names from source code aloud, a PowerShell module and an MCP server. See [docs/NARRATION.md](docs/NARRATION.md).

```powershell
pip install -e ".[voice,mcp]"; python -m speechkit_narrate setup
python -m speechkit_narrate new .\projects\guide --input .\guide.md --title "The Guide"
python -m speechkit_narrate run .\projects\guide --accept-draft   # out\narration: one audio file per chapter + timings
```

## Run without installing anything

Add a script under [`requests/`](requests/README.md), then in GitHub open **Actions → narrate → Run workflow** and enter its path. The WAVs and `speech.json` appear as a downloadable run artifact.

## Layout

| Path | Purpose |
|---|---|
| [`bootstrap.py`](bootstrap.py) | `fetch` / `verify` / `install` |
| [`speechkit/`](speechkit/) | CLI ([`src/voice.py`](speechkit/src/voice.py), [`src/install_offline.py`](speechkit/src/install_offline.py)), voice profile, schema, optional Claude plugin metadata |
| [`docs/INTEROP.md`](docs/INTEROP.md) | The [`speech.json`](speechkit/schemas/speech.schema.json) contract for integrating other projects |
| [`docs/LICENSING.md`](docs/LICENSING.md) | License rationale and commercial model |
| [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) | Bundled component licenses |
| [`requests/`](requests/README.md) | Narration scripts for the [`narrate`](.github/workflows/narrate.yml) workflow |
| [`speechkit_narrate/`](speechkit_narrate/) | The narration pipeline ([docs/NARRATION.md](docs/NARRATION.md)) |
| [`emblem_rsi/`](emblem_rsi/) | The sealed-rubric measure/promote/veto loop behind the pronunciation audit |
| [`powershell/SpeechKitVoiceTtsOfflinePipeline/`](powershell/) | PowerShell module for the pipeline (Windows with WSL, or any platform) |
| [`archives/`](archives/) | Runtime ZIP lands here (Release asset, git-ignored) |

## Boundaries

Self-contained for the supplied model and Python dependencies. Nothing here renders video, reads PDFs or tracks study state. No fonts, credentials or user data are distributed. The pronunciation audit downloads two open recogniser models (sherpa-onnx releases) when `setup` runs.
