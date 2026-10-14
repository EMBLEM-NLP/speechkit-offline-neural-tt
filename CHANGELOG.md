# Changelog

## 1.1.0 — standalone release
- Repository is now a standalone tool: the narration CLI lives in [`speechkit/`](speechkit/); the Video Factory, RSI adapter and OBC Study Orchestrator are no longer bundled or referenced. Integration is via [`docs/INTEROP.md`](docs/INTEROP.md) only.
- Licensed GPL-3.0-or-later (Piper/espeak-ng are GPL and imported in-process); see [`docs/LICENSING.md`](docs/LICENSING.md). SPDX headers added; plugin metadata updated.
- [`bootstrap.py`](bootstrap.py): single pinned runtime archive; new `fetch` command (HTTPS, SHA-256 enforced); install no longer extracts a tools archive. End-to-end install and 13.5 s fresh synthesis verified.
- Voice, model, wheels and runtime archive are byte-identical to prior distributions (same SHA-256 pin).
- Tests: standalone-independence check, license check, fetch negative controls.
