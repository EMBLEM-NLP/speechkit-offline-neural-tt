# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Standalone tools: each works on plain files, no project folder needed.

    speak          text -> narrated WAV/MP3 + word timings (speechkit voice; Linux, or delegated)
    quick          any stage(s) on any file: makes (or reuses) a project folder and runs the planner

Each returns a JSON-able dict with at least "files". Tools that need the full pipeline build a throwaway
project under the cache folder, so they run exactly the same code as a project does.
"""
# Keeps annotations as strings (PEP 563) so `str | None` style hints work on every supported Python.
from __future__ import annotations

import contextlib  # provides the @contextmanager decorator used by scratch()
import json  # reads docmodel.json, request.json and the per-segment timing files; writes word timings
import shutil  # file copies and removal of scratch folders
import tempfile  # unique temporary folders for scratch projects and intermediate files
from pathlib import Path  # object-oriented file paths that work the same on Windows and Linux

# The `tool` decorator tags a function with its name, JSON-schema parameters and description.
# registry.load() scans this module and registers every tagged function as a standalone Tool.
from .registry import tool

# JSON-schema fragment for a file or folder parameter. The custom "format": "path" tells the registry's
# delegation code to translate the value between Windows and WSL paths when a tool runs on the other side.
PATH = {"type": "string", "format": "path"}


# Context manager that builds a throwaway project folder so a tool can reuse the exact project code.
# input_file is the source file to copy in, title the book title, settings any project.json keys to merge.
# It yields the Project; when the with-block ends, the whole temporary folder is deleted even after an error.
# The folders live under the cache (SPEECHKIT_CACHE/scratch), never inside the user's own directories.
@contextlib.contextmanager  # turns the generator below into a with-statement helper
# **settings collects keyword arguments such as subtitle=... or spoken={...}
def scratch(input_file, title: str = "", **settings):
    from . import env  # env knows the cache location
    from .project import create  # create() makes a project folder and its project.json
    base = Path(env.CACHE) / "scratch"  # all scratch projects share one parent folder in the cache
    base.mkdir(parents=True, exist_ok=True)  # make sure that parent exists (first use on a machine)
    # a fresh, uniquely named folder such as tool-k3j9x2 so parallel tools never collide
    d = Path(tempfile.mkdtemp(prefix="tool-", dir=base))
    try:  # everything after this point is cleaned up in the finally block
        # copies the input into <tmp>/project/input; without a title the file name is used, underscores as spaces
        p = create(d / "project", input_file, title=title or Path(input_file).stem.replace("_", " "))
        if settings:  # only touch project.json when extra settings were given
            # drop empty values so they do not override the defaults with blanks
            p.save_settings({k: v for k, v in settings.items() if v not in (None, "", {}, [])})
        yield p  # hand the project to the with-block; execution resumes here when the block ends
    finally:  # runs whether the block finished normally or raised
        # remove the scratch folder; ignore_errors so a locked file (common on Windows) cannot mask the real result
        shutil.rmtree(d, ignore_errors=True)


# Copies one file into a destination folder and returns the new path as text.
# src is the file, dst_dir the folder (created when missing), name an optional new file name.
# Used to lift results out of a scratch project before it is deleted.
def _copy(src: Path, dst_dir: Path, name: str | None = None) -> str:
    dst_dir.mkdir(parents=True, exist_ok=True)  # create the destination folder tree if needed
    dst = dst_dir / (name or src.name)  # keep the original name unless a new one was asked for
    shutil.copy2(src, dst)  # copy2 also keeps the modification time
    return str(dst)  # strings rather than Path objects so the tool result is JSON-ready


# Default logger for the tools: accepts any arguments and discards them.
# The CLI and MCP server pass their own log function when they want progress lines.
def _quiet(*_):
    pass  # the body is empty on purpose, so tools run silently unless a logger is passed


# Tool "speak": narrate a piece of text into one audio file plus per-word timings, without a project folder.
# Parameters: text or text_file (the words to read), out (.wav, .mp3 or .m4a), workers (parallel voice processes),
# settings (project.json settings such as {"spoken": {"code_names": true}}). Linux-only (the voice runtime); on
# Windows the registry delegates it to WSL. Returns the files, the length in seconds and the word count.
@tool("speak", params={"text": {"type": "string", "description": "text to read (paragraphs split on blank lines)"},
                       "text_file": {**PATH, "description": "or a .txt/.md file to read"},
                       "out": {**PATH, "description": ".wav, .mp3 or .m4a to write; word timings go next to it "
                                                     "as <name>.words.json"},
                       "workers": {"type": "integer", "default": 2},  # number of parallel voice workers
                       "settings": {"type": "object", "description": "project.json settings, e.g. "
                                                                     "{\"spoken\": {\"code_names\": true}}"}},
      required=["out"], platforms=["linux"], heavy=True,  # heavy marks it as a background job for the MCP server
      description="Narrate text with the speechkit neural voice: one audio file plus per-word timings")
def speak(out, text="", text_file="", workers=2, settings=None, log=_quiet):
    import numpy as np  # numpy mixes the segment audio into one buffer
    from . import audio  # pause rules, segment reading and audio writing shared with the export stage
    from .text import docmodel  # docmodel turns the text into blocks, tokens and segments
    from .voice import narrate as nar  # narrate voices the segments
    if not text and not text_file:  # at least one source of text is needed
        raise ValueError("give text or text_file")
    out = Path(out)
    fmt = out.suffix.lower().lstrip(".")
    if fmt not in ("wav", "mp3", "m4a"):
        raise ValueError(f"out must end in .wav, .mp3 or .m4a, not {out.suffix!r}")
    with tempfile.TemporaryDirectory() as td:  # holds the text file for the duration of the run
        src = Path(td) / "speech.txt"  # the scratch project needs an input file
        src.write_text(text or Path(text_file).read_text(encoding="utf-8"), encoding="utf-8")
        # scratch project titled "Speech" (the title becomes a cover block that is left out below)
        with scratch(src, "Speech", **(settings or {})) as p:
            p.ensure("book")
            body = src.read_text(encoding="utf-8").strip()
            # written straight to book.md, skipping extract and check: the text is already plain markup
            p.p("book").write_text(body + "\n", encoding="utf-8")
            docmodel.build(p)  # builds docmodel.json and request.json from book.md
            nar.narrate(p, log=log, workers=workers)  # voices every segment into work/speech
            dm = json.loads(p.p("docmodel").read_text(encoding="utf-8"))
            blocks, flat = dm["blocks"], dm["tokens"]
            segs = [s for s in dm["segments"] if blocks[s["first_block"]]["kind"] != "cover"]  # no title segment
            recs = {s["id"]: json.loads((p.p("speech") / f"{s['id']}.wav.json").read_text(encoding="utf-8"))
                    for s in segs}
            for r in recs.values():
                audio.fix_collapsed(r["words"])
            t, offs = 0.25, []  # 0.25 s of silence before the first word
            for n, s in enumerate(segs):
                if n:
                    t += audio.gap_between(blocks, segs[n - 1], s)  # the export stage's pause rules
                offs.append(t)
                t += recs[s["id"]]["durationSeconds"]
            buf = np.zeros(int(round((t + 0.4) * audio.RATE)), dtype=np.float32)  # with 0.4 s of tail
            words = []
            for s, off in zip(segs, offs):
                a = audio.read_wav(p.p("speech") / f"{s['id']}.wav")
                i = int(round(off * audio.RATE))
                buf[i:i + len(a)] = a[:len(buf) - i]
                for w in recs[s["id"]]["words"]:
                    words.append({"word": flat[w["token"]]["disp"], "start": round(off + w["start"], 3),
                                  "end": round(off + w["end"], 3)})
    out.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(np.round(buf * 32767), -32768, 32767).astype("<i2").tobytes()
    audio._write_audio(pcm, out, fmt, 96)  # wav needs nothing; mp3 and m4a run ffmpeg
    wj = out.with_name(out.stem + ".words.json")  # <name>.words.json next to the audio file
    wj.write_text(json.dumps(words, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"files": [str(out), str(wj)], "seconds": round(len(buf) / audio.RATE, 2), "words": len(words)}


# Tool "quick": runs any stages on any file through the normal planner, creating or reusing a project folder.
# Parameters: input, out (project folder), targets (stage names), title/subtitle/author for a new project,
# settings (merged into project.json), skip (stages to leave out) and accept_draft (skip the review stop).
# Returns the project path, the stages that ran, any stop reason and up to 200 files under out/.
# input is ignored when out already holds a project
@tool("quick", params={"input": {**PATH, "description": ".txt, .md or request .json (not needed if out is already a "
                                                      "project)"},
                       # project folder: created on first use, reused when it already holds project.json
                       "out": {**PATH, "description": "project folder to create or reuse"},
                       "targets": {"type": "array", "items": {"type": "string"},  # stage names to bring up to date
                                   "description": "stages to bring up to date, e.g. [\"narrate\"] or [\"export\"]; "
                                                  "default: the whole pipeline"},
                       # metadata for a newly created project
                       "title": {"type": "string"}, "subtitle": {"type": "string"}, "author": {"type": "string"},
                       # project.json settings merged in before running
                       "settings": {"type": "object", "description": "project.json settings to merge in"},
                       # stage names to leave out of this run
                       "skip": {"type": "array", "items": {"type": "string"}, "description": "stages to skip"},
                       # default True: a quick run does not pause for the draft review
                       "accept_draft": {"type": "boolean", "default": True}},
      required=["out"], heavy=True,  # heavy: the MCP server runs it as a background job
      # summary of the quick tool for listings
      description="Run any stage(s) on any file: creates (or reuses) a project folder and brings the requested "
                  "stages up to date, running only what is missing or changed")
# quick(...) -> dict; side effect: a project folder created or updated, stages run
def quick(out, input="", targets=None, title="", subtitle="", author="", settings=None,
          skip=None, accept_draft=True, log=_quiet):  # log defaults to silence
    from .project import Project, create  # Project opens an existing folder, create makes a new one
    from .registry import load, run  # the planner and runner
    out = Path(out)  # the project folder as a Path
    if (out / "project.json").exists():  # an existing project is reused as is
        p = Project(out)  # open it (title, input and the other creation arguments are not applied)
    else:  # otherwise a new project is created
        if not input:  # a new project needs an input file
            raise ValueError(f"{out} is not a project yet: give input")  # say what is missing
        # copy the input into out/input and write project.json with the given settings
        p = create(out, input, title, subtitle, author, settings=settings)
        settings = None  # already in project.json
    if settings:  # merge extra settings into an existing project, if any
        p.save_settings(settings)  # written to project.json and reloaded
    reg = load(p)  # registry including this project's plug-ins
    # None/empty targets mean the default pipeline; the planner skips anything already up to date
    r = run(p, reg, targets=targets or None, skip=skip or (), accept_draft=accept_draft, log=log)
    # every file under out/, sorted, or none when out/ does not exist yet
    files = [str(f) for f in sorted((p.root / "out").rglob("*")) if f.is_file()] if (p.root / "out").exists() else []
    # the list is capped at 200 entries to keep the result small
    return {"project": str(p.root), "ran": r["ran"], "stopped": r["stopped"], "files": files[:200]}
