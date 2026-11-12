# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Pronunciation audit for a speechkit-narrate project: the emblem-rsi sealed-rubric loop, pointed at this project.

    configure(project)   point emblem_rsi.pron at the project (env vars), return the modules
    run_dir(project)     work/rsi/<name>, sealed on first use
    measure / decide     one loop step
    autofix              propose fixes, try them acoustically, apply the winners, re-measure, retake (see autofix.py)
"""
# Type hints such as "Path | None" are kept as strings and never evaluated at import time.
from __future__ import annotations

import json  # reads and writes the measurement files and the decision log
import os  # environment variables that steer the emblem_rsi modules
import sys  # the current Python executable and the module search path
from pathlib import Path  # Path objects for the run folder and the project files

from .. import env  # where the speechkit runtime, the recognizer models and the voices are installed

# Root folder (str) of the project the audit modules were configured for in this process, or None before the first
# configure(). emblem_rsi.pron.paths reads its environment variables once, at import, so a process serves one project.
_CONFIGURED = None


# Makes emblem_rsi importable for this process and for the Pythons it starts. A toolkit folder or checkout goes first on
# PYTHONPATH and on sys.path (existing entries follow; it is added on every call). A pip-installed copy has no such
# folder: env.REPO would be site-packages, which holds every other package too and would shadow the packages of the
# speechkit runtime's own Python that the voice command starts, and emblem_rsi is importable from site-packages anyway,
# so nothing is changed. Takes no parameters and returns nothing; changes os.environ and sys.path.
def _expose_repo() -> None:
    if env.installed():  # a pip-installed copy
        return  # nothing to add
    os.environ["PYTHONPATH"] = os.pathsep.join(  # the new value, joined with the platform's path separator
        # the toolkit folder first, then every entry already on the path (empty ones dropped)
        [str(env.REPO)] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p])
    if str(env.REPO) not in sys.path:  # emblem_rsi lives in the repository, not in site-packages
        sys.path.insert(0, str(env.REPO))  # put the repository ahead of any installed copy


# Point the emblem_rsi pronunciation modules at this project and return them.
# Sets the EMBLEM_* environment variables (project root, runtime Python, recognizer models, voice model and config, and
# the commands that rebuild the script, re-voice changed segments and voice retakes), then imports the modules.
# Returns {"loop", "asr", "cli", "measure", "trial"}. Raises RuntimeError when this process already serves another
# project. Calling it again for the same project is harmless (the variables are set again, the modules are already
# imported). Side effects: os.environ and sys.path change, and the aligned voice model may be built on first use.
def configure(project):
    """Set the EMBLEM_* variables for this project and import the audit modules (one project per process)."""
    global _CONFIGURED  # the function records the configured root in the module-level variable
    root = str(project.root)  # project folder as a string (environment values must be text)
    if _CONFIGURED and _CONFIGURED != root:  # already configured, but for a different book
        # the paths are frozen at import, so switching projects would silently audit the wrong book
        raise RuntimeError("the audit is already configured for another project in this process")
    prof = project.cfg["voice"]["profile"]  # speechkit voice profile from project.json, for example ljspeech-high
    from ..voice import narrate as nr  # imported here for voice_args, which builds the karaoke_voice.py argument list
    from ..locking import lock_token, DELEGATE_TOKEN
    token = lock_token(project.root)
    trusted_prefix = ["env", f"{DELEGATE_TOKEN}={token}"] if token else []
    os.environ.update({  # merge the variables into this process's environment; child processes inherit them
        # narration project the audit reads: work/request.json, docmodel.json, speech/ and pronunciations.json
        "EMBLEM_PRON_ROOT": root,
        # Python of the speechkit runtime venv, which has Piper and the espeak bridge
        "EMBLEM_PIPER_PYTHON": str(env.runtime_python()),
        "EMBLEM_ASR_MODELS": str(env.ASR_MODELS),  # folder holding the sherpa-onnx recognizer models
        # aligned voice ONNX (outputs phoneme durations) for trial takes; built into the cache when missing
        "EMBLEM_VOICE_MODEL": str(env.aligned_model(prof)),
        # voice config JSON for the same profile (sample rate, phoneme id map)
        "EMBLEM_VOICE_CONFIG": str(env.voice_config(prof)),
        # exactly one stage each (no prerequisites): the loop itself decides what is rebuilt and when
        "EMBLEM_REBUILD_CMD": json.dumps([*trusted_prefix, sys.executable, "-m", "speechkit_narrate", "run", root, "--only", "script",
                                          "--force", "script"]),
        # re-voice command: `speechkit-narrate run <root> --only narrate --force narrate`, which re-voices segments whose text
        # changed
        "EMBLEM_REVOICE_CMD": json.dumps([*trusted_prefix, sys.executable, "-m", "speechkit_narrate", "run", root, "--only", "narrate",
                                          "--force", "narrate"]),
        # Retake voicing command: karaoke_voice.py under the runtime Python, with {script}, {out} and {worker}
        # placeholders that emblem_rsi.pron.paths.voice_cmd fills in for every take.
        "EMBLEM_VOICE_CMD": json.dumps([str(env.runtime_python()), str(Path(nr.__file__).with_name("karaoke_voice.py")),
                                        # runtime, profile, aligned model and speechkit source arguments, plus the
                                        # worker slot such as "0/2"
                                        *nr.voice_args(project, Path("{script}"), Path("{out}")), "--worker", "{worker}"]),
    })
    _expose_repo()  # let this process and its children import emblem_rsi from a toolkit folder, when there is one
    _CONFIGURED = root  # remember which project this process now serves
    # Imported only now, after the environment is set, because emblem_rsi.pron.paths reads EMBLEM_* when first imported.
    from emblem_rsi import loop  # noqa: F401
    # asr: speech recognition probe; cli: apply, retake and revert; measure: one full measurement; trial: acoustic
    # trials.
    from emblem_rsi.pron import asr, cli, measure, trial  # noqa: F401
    # modules by short name, so callers never import them before the environment is ready
    return {"loop": loop, "asr": asr, "cli": cli, "measure": measure, "trial": trial}


# The audit run directory work/rsi/<name> of this project, with the pronunciation rubric sealed on first use.
# name selects the run (default "pron"). Returns the Path. The seal (rubric.sealed.json) is written once and never
# overwritten, so the scoring rules cannot change after measurements start.
def run_dir(project, name: str = "pron") -> Path:
    m = configure(project)  # make sure the modules point at this project
    d = project.p("rsi") / name  # this run's folder inside the project: work/rsi/pron unless another name is given
    if not (d / "rubric.sealed.json").exists():  # no sealed rubric yet: this is the project's first audit
        d.mkdir(parents=True, exist_ok=True)  # create the folder, and work/rsi if needed
        # seal emblem_rsi/rubrics/pronunciation.json: copy it with a SHA-256 hash and a timestamp
        m["loop"].seal(m["measure"].RUBRIC, d)
    return d  # the run directory, ready for measurements


# Hear the whole narration back once and write measure-NNN.json into the run directory.
# note is a short text stored with the measurement; log receives progress lines (default print).
# Runs both recognizers, the dictionary probe and the r-colour probe; changes no project file. Returns the file path.
def measure(project, note: str, name: str = "pron", log=print) -> Path:
    m = configure(project)  # audit modules configured for this project's paths
    d = run_dir(project, name)  # run directory, sealed on first use
    # the script, the docmodel and the folder of narration WAVs
    return m["measure"].measure(d, project.p("request"), project.p("docmodel"), project.p("speech"),
                                # the overrides file is hashed into page_sha256, so an edited override counts as a
                                # changed page
                                project.p("pronunciations"), note=note, log=log,
                                watch=list((project.cfg.get("audit") or {}).get("watch") or []))


# Promote or veto a measurement under the sealed rubric's fixed rules and record the decision.
# change says what changed since the last loop; measurement defaults to the newest measure-*.json.
# Appends one line to log.jsonl and returns that entry (decision, score, reasons, metric rows).
def decide(project, change: str, name: str = "pron", measurement: Path | None = None) -> dict:
    m = configure(project)  # loop and measurement modules for this project
    d = run_dir(project, name)  # run directory of the named audit
    # newest measurement unless one is given; zero-padded names sort in order (IndexError when none exist)
    mpath = measurement or sorted(d.glob("measure-*.json"))[-1]
    loop = m["loop"]  # the loop module: seal, decide, log helpers
    # score against the sealed rubric, compare with the promoted history, collect veto reasons
    entry = loop.decide(loop.load_sealed(d), json.loads(mpath.read_text(encoding="utf-8")), loop.read_log(d))
    entry["change"] = change  # record what changed, for the history and the report
    entry["measurement"] = mpath.name  # and which measurement file was judged
    loop.append_log(d, entry)  # append the entry to log.jsonl
    return entry  # the decision entry, for the caller to log or act on


# The loop history: one dict per decide() call, oldest first; an empty list before the first audit. name selects the
# audit run; reading the log never seals or measures anything.
def status(project, name: str = "pron") -> list[dict]:
    m = configure(project)  # configure first so the loop module can be imported safely
    d = project.p("rsi") / name  # run directory, looked up without sealing it
    # read log.jsonl when the folder exists; otherwise there is no history yet
    return m["loop"].read_log(d) if d.exists() else []


# The newest measurement plus the probe outputs stored beside it, for autofix (the report reads the measurements itself).
# Returns {"measurement", "path", "asr", "lexicon", "rcolour"}; a probe that did not run gives None.
# Raises RuntimeError when nothing has been measured. Reads JSON files only and does not configure anything.
def latest(project, name: str = "pron") -> dict:
    """Latest measurement plus its probe outputs (asr, lexicon, rcolour)."""
    d = project.p("rsi") / name  # run directory of the named audit
    ms = sorted(d.glob("measure-*.json"))  # measurement files in order (measure-001.json, measure-002.json ...)
    if not ms:  # no measurement files exist in the run folder yet
        # the message names the command that makes a first measurement
        raise RuntimeError("no measurement yet: run `speechkit-narrate audit` first")
    m = json.loads(ms[-1].read_text(encoding="utf-8"))  # parsed newest measurement JSON (metrics, inputs, hashes)
    md = d / ms[-1].stem  # the folder with that measurement's probe outputs (measure-NNN beside measure-NNN.json)
    out = {"measurement": m, "path": ms[-1]}  # start the result with the measurement and its file path
    # probe outputs by key and path relative to that folder
    for key, rel in (("asr", "asr/asr_measure.json"), ("lexicon", "lexicon/lexicon_measure.json"),
                     ("rcolour", "rcolour.json")):  # the r-colour (unspelled r) probe writes a single JSON file
        p = md / rel  # full path of this probe's output
        # parsed JSON, or None when the probe could not run (for example without espeak or parselmouth)
        out[key] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    return out  # the measurement together with its probe outputs dictionary
