# speechkit-offline-neural-tt

**Standalone offline neural text-to-speech by EMBLEM-NLP.** A hash-pinned Piper/ONNX runtime with the `en_US-ljspeech-high` voice, a small CLI, and a verifying bootstrap. It turns a JSON script into hashed 22.05 kHz mono WAVs plus a [`speech.json`](speechkit/schemas/speech.schema.json) manifest. No cloud API, no model download, no dependency on any other EMBLEM-NLP project.

Version **1.1.0** · License **GPL-3.0-or-later** (see [docs/LICENSING.md](docs/LICENSING.md)) · Linux x86_64, CPython 3.13, glibc ≥ 2.28, ≥ 1.5 GB free.

## Quick start

```sh
git clone https://github.com/EMBLEM-NLP/speechkit-offline-neural-tt.git
cd speechkit-offline-neural-tt
python3.13 bootstrap.py fetch                      # 232 MB runtime release asset, SHA-256 checked
python3.13 bootstrap.py verify                     # CRCs, member safety, internal SHA-256 ledgers
python3.13 bootstrap.py install --out ./installed  # new dir only; runs doctor + real synthesis
```

`fetch` is the only command that uses the network and rejects anything that doesn't match the pinned hash. The runtime is a Release asset because it exceeds GitHub's 100 MB file limit; you can also place it manually in [`archives/`](archives/).

## Synthesize

```sh
echo '{"schemaVersion":"1.0.0","id":"demo","segments":[{"id":"intro","text":"Follow the signal.","spokenText":"Follow the signal."}]}' > request.json
./installed/runtime/.venv/bin/python speechkit/src/voice.py synthesize \
  --runtime ./installed/runtime --profile speechkit/profiles/ljspeech-high.json \
  --script request.json --out ./speech-out
```

Output: `speech-out/speech.json` (text, hashes, sample counts) and one WAV per segment. Re-running into the same folder resumes only when request, profile and recorded WAV hashes agree. Use [`voice.py doctor`](speechkit/src/voice.py) to check imports and weights. The format is documented in [docs/INTEROP.md](docs/INTEROP.md), so any program (video renderers, course tools, game pipelines) can consume it.

Synthesis success is not a naturalness or pronunciation approval; `voiceApproval` stays `PENDING` until a human reviews it.

## Layout

| Path | Purpose |
|---|---|
| [`bootstrap.py`](bootstrap.py) | `fetch` / `verify` / `install` |
| [`speechkit/`](speechkit/) | CLI ([`src/voice.py`](speechkit/src/voice.py), [`src/install_offline.py`](speechkit/src/install_offline.py)), voice profile, schema, optional Claude plugin metadata |
| [`docs/INTEROP.md`](docs/INTEROP.md) | The [`speech.json`](speechkit/schemas/speech.schema.json) contract for integrating other projects |
| [`docs/LICENSING.md`](docs/LICENSING.md) | License rationale and commercial model |
| [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) | Bundled component licenses |
| [`archives/`](archives/) | Runtime ZIP lands here (Release asset, git-ignored) |

## Boundaries

Self-contained for the supplied model and Python dependencies. Nothing here renders video, reads PDFs or tracks study state. No fonts, credentials or user data are distributed.
