# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Test plug-in: a "stats" stage (spoken words and reading time of a project) and a "wordcount" tool.

Used by tests/narration/test_registry.py and test_mcp.py to prove that plug-in stages and tools are loaded from
SPEECHKIT_PLUGINS, --plugin and the MCP server, and that a stage option reaches the stage code.
"""
import json  # reads the docmodel and writes out/stats.json
import re  # counts words in the wordcount tool
from pathlib import Path  # the tool's input file

from speechkit_narrate.registry import stage, tool

WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*")  # a word, with an apostrophe inside it kept ("Don't")


@stage("stats", needs=["script"], inputs=["docmodel"], outputs=["out/stats.json"],
       options={"wpm": {"type": "integer", "default": 160, "description": "reading speed in words per minute"}},
       description="spoken words and reading time")
def stats(project, ctx):
    dm = json.loads(project.p("docmodel").read_text(encoding="utf-8"))
    words = sum(1 for t in dm["tokens"] if t["spoken"])  # every spoken token is a word
    wpm = int(ctx.opt("wpm", 160))
    out = {"words": words, "wpm": wpm, "reading_minutes": round(words / wpm, 1)}
    (project.root / "out").mkdir(exist_ok=True)
    (project.root / "out" / "stats.json").write_text(json.dumps(out), encoding="utf-8")
    ctx.log(f"stats: {out}")
    return out


@tool("wordcount", params={"file": {"type": "string", "format": "path", "description": "a text file"}},
      required=["file"], description="Count the words in a text file")
def wordcount(file, log=print):
    return {"files": [], "words": len(WORD.findall(Path(file).read_text(encoding="utf-8")))}
