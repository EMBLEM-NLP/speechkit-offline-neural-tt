---
name: narration-pipeline
description: Narrate a whole text (.txt, .md or a request .json) with word timings, a pronunciation audit and per-chapter export, using the speechkit_narrate pipeline in this repository.
disable-model-invocation: true
---

Read [docs/NARRATION.md](../../../docs/NARRATION.md) and the user's brief. Run the pipeline from the repository root with `python -m speechkit_narrate` (add `--json` for machine-readable results); check the machine first with `python -m speechkit_narrate doctor`. Create a project with `new`, look at `plan`, then `run`. The first full run stops after `check` so `work/book.md` can be reviewed; pass `--accept-draft` only when the user asked to go straight through.

The voice side (prevoice, narrate, autofix, audit) needs the Linux runtime; on Windows it runs in WSL through `SPEECHKIT_DELEGATE`. Narration and the pronunciation loop take minutes to hours: run them in the background and report progress from `status`. A killed run resumes where it stopped.

The export's `manifest.json` says `NEURAL_AUDIO_GENERATED_REVIEW_PENDING`: an automatic audit is not a human approval of naturalness or pronunciation. Report the audit page (`work/rsi/pron/report/Pronunciation_Audit.html`) and the words it could not fix.
