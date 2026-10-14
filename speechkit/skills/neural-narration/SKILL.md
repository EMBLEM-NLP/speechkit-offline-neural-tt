---
name: neural-narration
description: Generate neural narration using local identity-checked source assets and explicit project manifests.
disable-model-invocation: true
---

Read the repository README and the user's brief. Treat this as trusted executable code, not a prompt-only renderer. Invoke `python "${CLAUDE_PLUGIN_ROOT}/src/voice.py"` with the documented arguments. Check actual local files and dependencies first. Do not infer content from prior projects. Do not present silent output as a narrated deliverable.

Keep independent naturalness and pronunciation review separate from technical synthesis success; synthesis alone never approves a voice. Respect runtime platform constraints and model licenses. Do not download paid services silently.
