# Changelog

## 1.0.2 — repository packaging (`speechkit-offline-neural-tt`)

- Repackaged as the GitHub repository `EMBLEM-NLP/speechkit-offline-neural-tt`.
- `bootstrap.py`: new explicit `fetch` command downloads the runtime release asset over HTTPS and accepts it only if its SHA-256 matches the pin; `verify`/`install` now point to `fetch` when the runtime is absent. `distributionVersion` reports 1.0.2.
- The 232 MB runtime ZIP is a Release asset (git's per-file limit is 100 MB); the 2.7 MB tools ZIP is committed.
- Tests skip runtime-presence checks in a fresh clone; added manifest/pin consistency and fetch negative controls.
- `README_FIRST.md` → `README.md`; `NEXT_CHAT_PROMPT.md` → `docs/HANDOFF_PROMPT.md`; 1.0.1 proof/reports moved unchanged to `evidence/dist-1.0.1/`.
- Both inner archives are byte-identical to 1.0.1 (same SHA-256 pins). No change to voice, model, renderer or scoring.

## 1.0.1 — missing neural payload repaired

See `evidence/dist-1.0.1/reports/EXECUTION_REPORT.md`.
