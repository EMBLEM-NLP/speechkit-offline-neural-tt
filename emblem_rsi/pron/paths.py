# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Where the narration being audited lives, read from environment variables once at import.

The audit works on a narration project laid out as <root>/work/{request.json, docmodel.json, speech/,
pronunciations.json}. speechkit-voice-tts-offline-pipeline sets every variable below before importing the audit. Standalone
use falls back to the current directory for the project root; the other settings have no default (None),
and the code that needs one raises an error naming the variable to set.

  EMBLEM_PRON_ROOT     project root (default: the current directory)
  EMBLEM_PIPER_PYTHON  python of the speechkit runtime (has piper + espeak bridge)
  EMBLEM_ASR_MODELS    folder holding the sherpa-onnx recognizer models
  EMBLEM_VOICE_MODEL   aligned voice ONNX used for trial takes (default <root>/work/ljspeech-high.aligned.onnx)
  EMBLEM_VOICE_CONFIG  voice config JSON
  EMBLEM_REBUILD_CMD   JSON list: command that rebuilds request/docmodel after overrides change (run in root)
  EMBLEM_REVOICE_CMD   JSON list: command that re-voices changed segments (run in root)
  EMBLEM_VOICE_CMD     JSON list with {script} {out} {worker}: voices a request file into a folder (retakes)
"""
# lets type hints such as list[str] stay unevaluated strings on older Python versions
from __future__ import annotations

import json  # parses the JSON command lists that speechkit-voice-tts-offline-pipeline passes in the EMBLEM_*_CMD variables
import os  # os.environ is where every setting below is read from
from pathlib import Path  # Path gives operating-system-neutral path joining with the / operator


# The value of an environment variable, or None when it is unset or empty. name is the variable name.
def _env(name: str):
    return os.environ.get(name) or None  # an empty string counts as unset


# A JSON command list from an environment variable, or None when it is unset or empty. name is the variable.
def _env_cmd(name: str):
    v = _env(name)  # raw JSON text, or None
    return json.loads(v) if v else None  # "null" also loads as None


# Returns value, or raises RuntimeError naming the variable when it is None. name is the EMBLEM_* variable that
# supplies it and what describes the setting. Call sites use it so a missing setting gives one clear message.
def need(value, name: str, what: str):
    if value is None:  # the variable was not set when this module was imported
        raise RuntimeError(f"{what} is not configured: set {name} (speechkit-voice-tts-offline-pipeline sets it for you)")
    return value  # the configured value, unchanged


# Root of the narration project being audited. speechkit_narrate/audit/__init__.py sets EMBLEM_PRON_ROOT to the book
# project folder before importing this package; standalone use falls back to the current directory.
ROOT = Path(_env("EMBLEM_PRON_ROOT") or Path.cwd())
WORK = ROOT / "work"  # the work folder holds everything the narration pipeline produced
# the narration script: one entry per voiced segment with its spoken tokens and token ids
REQUEST = WORK / "request.json"
# document model: every token of the book with its display text, block and optional spoken_ref
DOCMODEL = WORK / "docmodel.json"
SPEECH = WORK / "speech"  # folder of per-segment audio (<id>.wav) plus timing records (<id>.wav.json)
OVERRIDES = WORK / "pronunciations.json"  # pronunciation overrides file that `pron apply` writes and `revert` restores
# Interpreter of the speechkit runtime virtual environment, or None when unset. Only that venv has piper,
# onnxruntime and the espeak-ng bridge, so helper scripts such as _espeak_helper.py and _trial_synth.py run with it.
RUNTIME_PY = _env("EMBLEM_PIPER_PYTHON")
# folder that holds the sherpa-onnx recognizer model folders used by asr.py, or None when unset
MODELS = Path(_env("EMBLEM_ASR_MODELS")) if _env("EMBLEM_ASR_MODELS") else None
# aligned voice ONNX model used to voice trial takes, defaulting to a copy inside work/
VOICE_MODEL = Path(_env("EMBLEM_VOICE_MODEL") or WORK / "ljspeech-high.aligned.onnx")
# Piper voice configuration JSON (sample rate, espeak voice name, phoneme map) for the voice above, or None.
VOICE_CONFIG = Path(_env("EMBLEM_VOICE_CONFIG")) if _env("EMBLEM_VOICE_CONFIG") else None
# Command (a JSON list, run with cwd = ROOT) that rebuilds request.json and docmodel.json after the overrides
# change, or None. speechkit-voice-tts-offline-pipeline sets it to its own `run <root> --only script --force script`.
REBUILD_CMD = _env_cmd("EMBLEM_REBUILD_CMD")
# Command that re-voices only the segments whose spoken text changed after an apply, or None.
REVOICE_CMD = _env_cmd("EMBLEM_REVOICE_CMD")
# Command that voices a request file into a folder, used by `pron retake` for extra takes, or None.
VOICE_CMD = _env_cmd("EMBLEM_VOICE_CMD")


# Builds the concrete retake command for one worker by filling the placeholders in VOICE_CMD.
# script is the request JSON to voice (a Path or str), out is the folder that receives the WAVs, and worker is
# a "k/n" string so that n parallel processes each voice their own share of the segments.
# Returns a new list of strings ready for subprocess.Popen; nothing is started here (cli.retake does that).
# Raises RuntimeError when EMBLEM_VOICE_CMD is not set.
def voice_cmd(script, out, worker: str) -> list[str]:
    # every argument is stringified, then each {placeholder} is swapped for its real value
    return [str(a).replace("{script}", str(script)).replace("{out}", str(out)).replace("{worker}", worker)
            # the list comprehension walks the template in order, so argument positions are preserved
            for a in need(VOICE_CMD, "EMBLEM_VOICE_CMD", "the retake voicing command")]
