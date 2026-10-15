# Narration requests

Put a script here, then run the **narrate** workflow from the Actions tab with its path.

- `*.txt`: plain text, one paragraph per segment (blank-line separated). Converted by [`text_to_request.py`](../speechkit/src/text_to_request.py).
- `*.json`: a full request (`schemaVersion`, `id`, `segments[]`); see [docs/INTEROP.md](../docs/INTEROP.md).

Example: [`example.txt`](example.txt). Output is a downloadable run artifact containing `speech.json` and one WAV per segment. Voice approval stays `PENDING` until a human listens.
