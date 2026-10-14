# Third-party components

Delivered inside the runtime release asset [`EMBLEM-NLP_Offline_Neural_Runtime_Linux_Py313.zip`](https://github.com/EMBLEM-NLP/speechkit-offline-neural-tt/releases/download/v1.1.0/EMBLEM-NLP_Offline_Neural_Runtime_Linux_Py313.zip) (full license texts under its `notices/`, corresponding source under its `sources/`).

| Component | Version | License |
|---|---|---|
| Piper TTS (`piper-tts`, embeds espeak-ng) | 1.8.0 | GPL-3.0-or-later (g2pW portions Apache-2.0); source for Piper and espeak-ng included |
| ONNX Runtime | 1.30.0 | MIT |
| NumPy | 2.3.5 | BSD |
| protobuf | 7.36.2 | BSD-3-Clause |
| flatbuffers | 25.12.19 | Apache-2.0 |
| pathvalidate | 3.3.1 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| Voice `en_US-ljspeech-high` | — | Trained on LJ Speech, stated public domain in the model card |

SpeechKit's own code ([`bootstrap.py`](bootstrap.py), [`speechkit/`](speechkit/)) is GPL-3.0-or-later, Copyright (C) 2026 EMBLEM-NLP.
