# Integrating with other projects

SpeechKit is a leaf tool: other projects call it, it never imports them. The integration surface is the command line plus one file format, so any consumer (including proprietary or differently licensed software) integrates at arm's length by running the CLI as a separate process and reading its output files.

## Input: request.json (schema 1.0.0)

```json
{"schemaVersion":"1.0.0","id":"my-project","segments":[
  {"id":"intro","text":"Original text.","spokenText":"Text as it should be pronounced."}]}
```

## Output: speech.json ([`speechkit/schemas/speech.schema.json`](../speechkit/schemas/speech.schema.json))

Status `NEURAL_AUDIO_GENERATED_REVIEW_PENDING`, plus per segment: `id`, original `text`, `spokenText`, WAV filename, `sha256`, `samples`. Audio is mono PCM16 at 22050 Hz. Consumers should verify each WAV's SHA-256 against the manifest and treat `voiceApproval: PENDING` as unapproved.

## Calling it

```sh
RUNTIME/.venv/bin/python speechkit/src/voice.py synthesize --runtime RUNTIME \
  --profile speechkit/profiles/ljspeech-high.json --script request.json --out OUT
```

Exit code is nonzero on any failure. There is no fallback voice: a failed synthesis produces no audio rather than robotic audio.

EMBLEM-NLP's separate Video Factory and OBC Study Orchestrator use exactly this contract; nothing else ties them to this repository.

## The narration pipeline's export

[`speechkit_narrate`](NARRATION.md) (in this repository) adds a second, chapter-level format in `out/narration/`:
one audio file per chapter, `<slug>_ch-NN.words.json` (`word`, `start`, `end` in seconds, `sentence`) and a
`manifest.json` with every file's SHA-256 and the same `NEURAL_AUDIO_GENERATED_REVIEW_PENDING` / `voiceApproval:
PENDING` status. Consumers should verify the hashes and treat the narration as unapproved until a person has
listened to it, exactly as for `speech.json`.

