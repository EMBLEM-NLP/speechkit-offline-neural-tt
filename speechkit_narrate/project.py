# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""A narration project: one text, one folder.

    <project>/
      project.json        settings (title, input, voice, spoken forms, chapters, export) - see DEFAULTS below
      input/              the source .txt, .md or SpeechKit request .json
      work/               everything intermediate (draft, book.md, script, narration, pronunciation audit runs)
      out/                finished narration: one audio file per chapter, word timings, manifest

Every stage reads and writes through Project.p(<name>) so file locations are defined in one place.
"""
# allows hints such as Path | None on Python versions that cannot evaluate them at runtime
from __future__ import annotations

import copy  # deep copies, so the shared DEFAULTS dict is never modified by one project
import json  # project.json is read and written as JSON
import re  # regular expressions for slugs
import shutil  # file copying when a new project imports its input file
from pathlib import Path  # object-oriented file paths that work the same on Windows and Linux
from .locking import locked_project

# Every setting a project can have, with its default value. project.json only stores what the user changed;
# Project merges that file over this dict, so a missing key always falls back to the value here.
DEFAULTS = {  # nested sections are merged key by key, not replaced wholesale
    "title": "Untitled",
    "subtitle": "",
    "author": "",
    "slug": "",                      # identifier used for the request id and output file names; from the title when empty
    "language": "en",
    "input": "",                     # input/<file>.txt | .md | .json (a SpeechKit request)
    "voice": {  # the speechkit voice: which profile and how many parallel workers
        "profile": "ljspeech-high",  # speechkit profile name (speechkit/profiles/<name>.json) or a path to one
        # number of parallel voice processes, which share the segments; each loads its own model, so more uses more RAM
        "workers": 2,
    },
    "spoken": {  # how the text is turned into spoken text (see text/docmodel.py)
        "read_cover": True,
        "scripture_refs": "auto",    # "23:1" after a Bible book name -> "23 verse 1"; "auto" | true | false
        "lowercase_allcaps_min": 5,  # ALL-CAPS words of at least this many letters are read as normal words
        # how numeric dates are written: "dmy" (03/10/2024 is 3 October), "mdy" (US), or "" to leave them alone;
        # ISO dates (2026-03-14) are always read as dates when this is set
        "dates": "",
        "ordinal_days": False,       # "October 30" is read "October 30th" (also "Nov 10 or 11" -> "10th or 11th")
        "years": False,              # 2026 is read "twenty twenty-six", 1960 "nineteen sixty" (US style)
        # reading rules applied to each displayed word before anything else, in order, each to the result of the one
        # before: ["regex", "spoken"] or {"match", "say", "prev", "next", "spell_digits"} (see text/docmodel.py)
        "say": [],
        # read names from source code the way a programmer says them: snake_case, dotted module and file names,
        # paths, --options, camelCase and PowerShell cmdlet names are split into words, file extensions and
        # abbreviations get fixed readings (text/codenames.py). Rules in "say" still win.
        "code_names": False,
        "silent": [],                # exact phrases shown in the text but not spoken, e.g. "(Bible Hub)"
        "silent_columns": [],        # table columns (by header text) shown but not spoken, e.g. ["Source"]
        # a regex for a cross-reference that ends a block after a finished sentence ("... this guide. Section 8"):
        # kept in the text but not spoken; "" turns it off
        "silent_trailing": "",
        "read_links": False,         # LINK blocks (a list of sources) are kept but not spoken unless this is true
        # a short pause before and after a remark in brackets ("Grupo Nexa Brasil (unrelated)" -> "Brasil, unrelated")
        "bracket_pause": False,
        # "cells": the header row is read, then every cell as its own sentence; "rows": the header row is not read,
        # and each row is read as a sentence with its column names ("Stripe. Role: Payment processor.")
        "table_reading": "cells",
    },
    # the prevoice stage (audit/prevoice.py): heteronyms that espeak would read in the wrong role get a per-token fix
    # before narration, so the pronunciation loop only has to verify them; off by default
    "prevoice": {
        "enabled": False,            # opt-in; off, the stage is skipped and the script is built exactly as before
        # how the reading a sentence wants is decided: "v1+tagger" (the v1 rules, confirmed by spaCy's tagger when it
        # is installed, else the rules alone), "v1", "v2", "v2+tagger", "tagger", "v1-then-tagger", "v2-then-tagger"
        "rules": "v1+tagger",
    },
    "audit": {"watch": [], "max_fix_words": None, "max_occurrences_per_word": 4},
    "chapters": {  # how the text is divided into chapters (one audio file per chapter)
        "markers": ["h1"],           # block kinds that start a chapter
        "extra_titles": [],          # h2 titles that also start a chapter (e.g. "Before you begin")
        "label": "Chapter",          # the word before a numbered chapter's number ("Section" suits a report)
    },
    "export": {  # the export stage: the finished narration in out/narration
        "format": "wav",             # "wav" (no extra tools), "mp3" or "m4a" (both need ffmpeg)
        "kbps": 96,                  # bit rate for mp3 and m4a
        "normalize": True,           # one gain for the whole text: speech at -19 dBFS RMS, peaks at most 0.97
        "lead": 0.6,                 # seconds of silence before the first word of each chapter
        "tail": 1.2,                 # seconds of silence after the last word of each chapter
    },
    "ingest": {  # how the input text is read into work/draft.md
        "autofix": True,             # repair glued words and spacing slips (every repair is reported)
        # a fenced ``` block in Markdown: "keep" keeps it in the text as a code block that is never read aloud (the
        # prose explains it; spoken.code_names reads the names it mentions), "read" reads its lines as a paragraph
        "code_blocks": "keep",
    },
    "text_fixes": {},                # whole-word replacements applied to the draft (typing slips)
    "plugins": [],                   # extra stage/tool modules or project-relative .py files (see registry.load)
}

# Standard locations, relative to the project folder.
PATHS = {
    "config": "project.json",
    "input_dir": "input",
    "work": "work",
    "out": "out",
    # reading the input
    "extract_dir": "work/extract",
    "draft": "work/draft.md",
    "book": "work/book.md",
    "extract_report": "work/extract/report.json",
    # script and narration
    "docmodel": "work/docmodel.json",
    "request": "work/request.json",
    "pronunciations": "work/pronunciations.json",
    "speech": "work/speech",
    # pronunciation audit
    "rsi": "work/rsi",
    # finished narration
    "out_narration": "out/narration",
}

# Input file extensions and the kind of reader each one goes to (see ingest/__init__.py).
INPUT_KINDS = {".txt": "text", ".text": "text", ".md": "text", ".markdown": "text", ".json": "request"}


# Recursively merge one settings dict over another, as used for DEFAULTS plus project.json.
# base is the dict of defaults, over the user's values (may be None); both are plain JSON-style dicts.
# Returns a new dict: nested dicts are merged key by key and every other value from over replaces the one in base.
# A null (None) value over a section (a dict in base) keeps the section's defaults: `"voice": null` in project.json
# means "no changes to voice", never "no voice settings at all", which would make every later lookup fail.
# Neither input is modified, because everything is deep-copied.
def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)  # start from a private copy so DEFAULTS is never mutated
    for k, v in (over or {}).items():  # "or {}" lets a missing or null section be merged as empty
        if v is None and isinstance(out.get(k), dict):  # a null section keeps the defaults it would have replaced
            continue
        # both sides are dicts: merge inside instead of replacing the whole section
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)  # recursion handles sections nested at any depth
        else:  # lists, strings, numbers and null replace the default outright
            out[k] = copy.deepcopy(v)  # copied so later edits to the result cannot reach back into the caller's dict
    return out  # a fresh dict: the defaults overridden by the user's values at every level


# Make a URL- and file-safe identifier from a title, such as "the-quiet-garden".
# s is any str; returns lower-case ASCII letters and digits separated by single hyphens.
def slugify(s: str) -> str:
    # every run of other characters becomes one hyphen, with no hyphen left at either end
    s = re.sub(r"[^A-Za-z0-9]+", "-", s.strip()).strip("-").lower()
    return s or "narration"  # a title with no ASCII letters or digits at all still gets a usable slug


# The one error type for problems the user can fix in the project: a missing project.json, input file or setting.
# The command line catches it and prints the message instead of a traceback.
class ProjectError(Exception):
    """A project setting or file is missing or wrong; the message says which."""


# Merge the user's settings over DEFAULTS and fill in the derived values; raises ProjectError when a section that must
# be a dict is not one. raw is the parsed project.json. Used by Project.__init__ and, before anything is written, by
# save_settings, so a bad update is refused instead of being saved and then failing on the next load.
def _settings(raw) -> dict:
    if not isinstance(raw, dict):  # project.json must hold one JSON object
        raise ProjectError("project.json must hold a JSON object")
    cfg = deep_merge(DEFAULTS, raw)  # the settings every stage reads: defaults with the user's values on top
    for k, v in DEFAULTS.items():  # every section of the defaults must still be a section
        if isinstance(v, dict) and not isinstance(cfg[k], dict):
            raise ProjectError(f"project.json: \"{k}\" must be an object, not {json.dumps(cfg[k])[:40]}")
    if not cfg["slug"]:  # no explicit slug configured
        cfg["slug"] = slugify(cfg["title"])  # derive one from the title
    if not isinstance(cfg["spoken"].get("read_cover"), bool):
        raise ProjectError('project.json: "spoken.read_cover" must be a boolean')
    audit = cfg["audit"]
    if not isinstance(audit.get("watch"), list) or any(not isinstance(w, str) or not w.strip() for w in audit["watch"]):
        raise ProjectError('project.json: "audit.watch" must be a list of nonempty words')
    for key in ("max_fix_words", "max_occurrences_per_word"):
        value = audit.get(key)
        if key == "max_fix_words" and value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ProjectError(f'project.json: "audit.{key}" must be a positive integer')
    return cfg


# One narration project on disk: its folder, its merged settings and the standard file locations.
# Every stage receives a Project and reads settings through cfg (or project["key"]) and paths through p(name).
class Project:
    # Load project.json from a project folder and fill in all derived settings.
    # root is the project folder as a str or Path; raises ProjectError when the folder has no project.json.
    # Afterwards raw holds exactly what the file says and cfg holds the defaults merged with it plus derived values.
    def __init__(self, root):
        # an absolute path, so later relative paths do not depend on the current directory
        self.root = Path(root).resolve()
        cfg_path = self.root / PATHS["config"]  # the project's settings file, project.json directly inside the folder
        if not cfg_path.exists():  # a folder that was never set up with "new"
            # the message tells the user the exact command that creates one
            raise ProjectError(f"{cfg_path} not found. Create a project with: "
                               "python -m speechkit_narrate new <folder> --input <file>")
        try:  # a hand edit can leave the file unreadable
            self.raw = json.loads(cfg_path.read_text(encoding="utf-8"))  # the user's own settings exactly as saved
        except json.JSONDecodeError as e:
            raise ProjectError(f"{cfg_path}: not valid JSON ({e})") from None
        self.cfg = _settings(self.raw)  # merged and checked

    # ---- settings ----
    # Dictionary-style access to a merged setting, so project["voice"] equals project.cfg["voice"].
    def __getitem__(self, k):
        return self.cfg[k]  # read from the merged settings, never from raw

    # The title from the merged settings ("Untitled" when project.json has none).
    @property
    def title(self) -> str:
        return self.cfg["title"]

    # The subtitle, an empty string when none is set.
    @property
    def subtitle(self) -> str:
        return self.cfg["subtitle"]

    # The project's URL-safe identifier, either set by the user or derived from the title; the request id.
    @property
    def slug(self) -> str:
        return self.cfg["slug"]  # always non-empty because _settings fills it in

    # ---- paths ----
    # Full path of one standard project file or folder, looked up by its PATHS name.
    # name is a key of PATHS such as "book" or "docmodel"; returns a pathlib.Path under the project root, which may not
    # exist yet.
    def p(self, name: str) -> Path:
        return self.root / PATHS[name]  # a KeyError for an unknown name catches typos early

    # Absolute path of the source file named in project.json "input".
    # Raises ProjectError when the setting is empty or the file is missing, so stages fail with a clear message.
    def input_file(self) -> Path:
        if not self.cfg["input"]:  # input is "" in the defaults until a file is set
            raise ProjectError("project.json has no \"input\" (the .txt, .md or request .json file)")
        f = self.root / self.cfg["input"]  # the setting is stored relative to the project folder, e.g. input/text.md
        if not f.exists():  # the file was moved, renamed or deleted
            raise ProjectError(f"input file {f} does not exist")
        return f

    # Classify the input file by its extension: "text" (.txt, .md) or "request" (a SpeechKit request .json).
    # Any other extension raises ProjectError.
    def input_kind(self) -> str:
        suf = self.input_file().suffix.lower()  # the lower-case extension, so .MD and .md are the same
        if suf not in INPUT_KINDS:
            raise ProjectError(f"unsupported input type {suf}: use .txt, .md or a SpeechKit request .json")
        return INPUT_KINDS[suf]

    # Kept for the registry's "@key" input specs: the path of an optional file setting, or None when it is unset.
    # A setting that names a file which does not exist raises ProjectError instead of quietly counting as unset.
    def optional(self, key: str) -> Path | None:
        v = self.cfg.get(key) or ""  # null and "" are both treated as not set
        if not v:
            return None
        f = self.root / v  # resolve against the project folder
        if not f.exists():
            raise ProjectError(f"project.json \"{key}\" names {f}, which does not exist")
        return f

    # Create the folders needed before writing standard project files.
    # names are PATHS keys; for a file entry (one with an extension) its parent folder is created, for a folder entry
    # the folder itself.
    def ensure(self, *names) -> None:
        for n in names:
            p = self.p(n)
            # a dotted name means a file, so make its parent; parents=True builds the whole chain
            (p if not p.suffix else p.parent).mkdir(parents=True, exist_ok=True)

    # Save new settings into project.json and reload the project from it.
    # updates is a nested dict merged over the raw user settings, so defaults are never copied into the file.
    # The merged settings are checked first: an update that would break the project raises ProjectError and leaves
    # project.json as it was.
    @locked_project
    def save_settings(self, updates: dict) -> None:
        """Merge updates into project.json (only the user's own settings, not the defaults)."""
        self.__init__(self.root)  # reload after acquiring the writer lock
        raw = deep_merge(self.raw, updates)  # merge into the user's own settings only
        cfg = _settings(raw)  # raises before anything is written
        # pretty-printed with a final newline so the file stays easy to edit and diff
        config = self.root / PATHS["config"]
        tmp = config.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(config)
        self.raw, self.cfg = raw, cfg


# Create a new project folder and its project.json, then return it opened as a Project.
# root is the folder to create; input_file is the source .txt, .md or request .json.
# title, subtitle and author go into project.json; the title defaults to the input's file name with underscores as
# spaces. Every given file is checked before anything is created or copied.
def create(root, input_file, title="", subtitle="", author="", settings=None) -> Project:
    """Make a new project folder, copying the input into input/."""
    root = Path(root).resolve()  # absolute path of the new project folder
    if (root / PATHS["config"]).exists():  # never overwrite an existing project
        raise ProjectError(f"{root} already has a project.json")
    s = Path(input_file)
    if not s.is_file():  # checked before anything is created
        raise ProjectError(f"{s} does not exist")
    if s.suffix.lower() not in INPUT_KINDS:
        raise ProjectError(f"unsupported input type {s.suffix}: use .txt, .md or a SpeechKit request .json")
    cfg = deep_merge(settings or {}, {"input": f"input/{s.name}",
                                      "title": title or s.stem.replace("_", " "), "subtitle": subtitle,
                                      "author": author})
    _settings(cfg)  # refuse bad extra settings before the folder is made
    (root / "input").mkdir(parents=True, exist_ok=True)  # creates the project folder and its input/ subfolder
    dst = root / "input" / s.name  # the copy keeps its original file name inside input/
    if s.resolve() != dst.resolve():  # copying a file onto itself would fail
        shutil.copy2(s, dst)  # copy2 also keeps the file's modification time
    (root / PATHS["config"]).write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return Project(root)  # open it, which validates it and fills in the derived settings
