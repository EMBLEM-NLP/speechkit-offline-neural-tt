# Licensing and commercial model

*Not legal advice; have counsel confirm before relying on this.*

## License: GPL-3.0-or-later

This repository is GPL-3.0-or-later because it has to be. [`speechkit/src/voice.py`](../speechkit/src/voice.py) imports Piper (`from piper import PiperVoice`), and the pinned Piper 1.8.0 wheel (which embeds espeak-ng) is GPL-3.0-or-later. Distributing the adapter together with Piper makes the combination a GPL work, so a permissive (MIT/Apache) or proprietary label on it would be inaccurate. Source-available or non-commercial licenses (BUSL, PolyForm Noncommercial, CC-NC) are incompatible with the GPL and are not an option here.

The runtime release asset ships the corresponding source for Piper and espeak-ng (`sources/`) and all third-party notices, which satisfies the GPL source obligation for the binary wheels. Keep them in every release.

## What is not encumbered

- **Generated audio** is not covered by the GPL; customers can sell narrated videos, courses and products. The voice's dataset (LJ Speech) is stated public domain in its model card.
- **Software that only runs the CLI and reads [`speech.json`](../speechkit/schemas/speech.schema.json)** (see [INTEROP.md](INTEROP.md)) is a separate program and can use any license, including a proprietary commercial one.

## How EMBLEM-NLP earns from it

Open core: this repo is the free, public engine. Revenue comes from what sits around it, not from restricting it.

1. **Commercial products in separate repositories** (Video Factory, OBC Study Orchestrator, review/scoring tools) under a proprietary license, calling SpeechKit only through the CLI + [`speech.json`](../speechkit/schemas/speech.schema.json). Never import `piper` or any GPL code into them.
2. **Hosted service / API.** Running GPL software as a network service does not require releasing your changes; you can sell narration-as-a-service.
3. **Support, SLAs, integration and custom voices** for businesses.
4. **Trademark.** The EMBLEM-NLP name and logo are not licensed by the GPL; forks must rebrand.
5. **Dual licensing of your own code** is possible only for code you wholly own and that does not include Piper/espeak-ng. Keep a contributor agreement (DCO or CLA) if you accept outside contributions, or that option disappears.

## Housekeeping before going public

- Confirm the copyright holder named in headers (currently "EMBLEM-NLP"; use the legal entity or individual if different).
- Keep [`LICENSE`](../LICENSE), [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) and the runtime's `sources/` and `notices/` intact in every release.
- Check each new voice model's license individually before adding it.
