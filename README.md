# speechkit-offline-neural-tt

Offline neural text-to-speech kit by **EMBLEM-NLP**: a hash-pinned Piper/ONNX voice runtime (`en_US-ljspeech-high`) bundled with the Video Factory, Neural Narration and RSI plugins and the OBC Study Orchestrator. One bootstrap verifies every archive, installs offline into a new directory and proves it by synthesizing a fresh WAV. No cloud speech API and no model download.

Distribution **1.0.2** (packaging only; components unchanged: Piper 1.8.0, ONNX Runtime 1.30.0, plugins 1.0.1, orchestrator 1.0.0).

## Quick start

Requires Linux x86_64, CPython **3.13** (with `venv`/`ensurepip`), glibc ≥ 2.28, ≥ 1.5 GB free.

```sh
git clone https://github.com/EMBLEM-NLP/speechkit-offline-neural-tt.git
cd speechkit-offline-neural-tt
python3.13 bootstrap.py fetch                    # download the 232 MB runtime release asset, SHA-256 checked
python3.13 bootstrap.py verify                   # CRCs, member safety, internal SHA-256 ledgers
python3.13 bootstrap.py install --out ./installed  # new dir only; runs doctor + real synthesis
```

`fetch` is the only command that touches the network, and it rejects anything that doesn't match the pinned hash. The runtime is a Release asset because it exceeds GitHub's 100 MB file limit. You can also download it manually from the Releases page into `archives/`.

Result:

```text
installed/
  EMBLEM-NLP_Study_Automation_Tools_1_0_0/   # orchestrator + video-factory, neural-narration, video-rsi plugins
  runtime/                                   # .venv, wheels, en_US-ljspeech-high.onnx(+.json)
  smoke/runtime-ready.wav, speech.json       # fresh synthesis proof
  logs/, INSTALL_AND_SMOKE_REPORT.json
```

Synthesis success is not a naturalness, pronunciation or comprehension approval.

## Use with the renderer

```sh
TOOLS="$(pwd)/installed/EMBLEM-NLP_Study_Automation_Tools_1_0_0/EMBLEM-NLP_Reusable_Video_Plugins_1_0_1"
VOICE="$(pwd)/installed/runtime"
node "$TOOLS/emblem-nlp-video-factory/dist/cli.js" doctor
node "$TOOLS/emblem-nlp-video-factory/dist/cli.js" render \
  --project /abs/path/project.json --voice-runtime "$VOICE" --out /abs/path/new-build
```

## Repository layout

| Path | Purpose |
|---|---|
| `bootstrap.py` | `fetch` / `verify` / `install` |
| `archives/` | Tools ZIP (committed); runtime ZIP (Release asset, git-ignored) |
| `BUNDLE_MANIFEST.json`, `SHA256SUMS` | Component versions and pinned identities |
| `tests/` | Negative controls (unsafe paths, bad hashes, symlinks, fetch failures) |
| `docs/HANDOFF_PROMPT.md` | Prompt for continuing work in a new AI chat |
| `evidence/dist-1.0.1/` | Original install/synthesis proof and reports |

## Boundaries

Self-contained for the supplied neural model and its Python dependencies only. Node, FFmpeg/FFprobe, Cairo/graphics Python libraries, local fonts, project media and (for OBC packet generation) the source PDFs/database remain host or project prerequisites. No font binaries, learner data or credentials are distributed.

## License

Add a `LICENSE` before publishing; bundled components (Piper, ONNX Runtime, the LJSpeech-derived voice) carry their own notices inside the runtime archive.
