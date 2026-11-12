# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Read the project's input into work/draft.md (the text markup every later stage reads) plus a review report.

Two kinds of input:
    .txt / .md     plain text or Markdown (textin.py): headings, paragraphs, lists, quotes, verse, tables and fenced
                   code blocks (kept, never spoken)
    .json          a SpeechKit request (schemaVersion 1.0.0): each segment becomes one paragraph of its spokenText
                   (or text); a segment whose id starts with "h1-" or "chapter" becomes a chapter heading instead

    run(project)            writes work/draft.md and work/extract/report.json, returns a short summary
    read_blocks(project)    -> (blocks, context) without writing anything
"""
from __future__ import annotations

import json  # the request input and the report are JSON
import time  # wall-clock timing for the report

from ..project import ProjectError  # a malformed request is a project problem the user can fix
from ..text import markup  # the draft's markup format; validate() checks the finished draft
from . import clean, report, structure  # spacing and glued-word repairs, report helpers, structure repairs


# Turn a SpeechKit request (the dict from request.json) into blocks. Raises ProjectError when it is not a request.
# Each segment's spokenText (or text when spokenText is empty) becomes a paragraph, so the narration says exactly what
# the request says; an id beginning "h1-" or "chapter" makes the segment a chapter heading.
def request_blocks(req: dict) -> list:
    if not isinstance(req, dict) or not isinstance(req.get("segments"), list):
        raise ProjectError("a .json input must be a SpeechKit request with a \"segments\" list")
    out = []
    for n, seg in enumerate(req["segments"]):
        if not isinstance(seg, dict):
            raise ProjectError(f"request segment {n} is not an object")
        text = str(seg.get("spokenText") or seg.get("text") or "").strip()
        if not text:  # an empty segment has nothing to say
            continue
        sid = str(seg.get("id", "")).lower()
        kind = "h1" if sid.startswith(("h1-", "chapter")) else "p"
        # a paragraph keeps its line breaks as spaces; the markup has one block per paragraph
        out.append({"kind": kind, "text": " ".join(text.split())})
    if not out:
        raise ProjectError("the request has no segment with text")
    return out


# Read the input into blocks and run the same structure and spacing repairs for every kind of input.
# Returns (blocks, ctx); ctx collects what the report needs: parser notes, structure decisions, automatic fixes and
# the project's text_fixes counts. Writes nothing.
def read_blocks(project) -> tuple[list, dict]:
    src = project.input_file()  # absolute path of the input file; raises ProjectError when missing
    kind = project.input_kind()  # "text" or "request"
    opts = project.cfg.get("ingest") or {}
    ctx = {"kind": kind, "notes": [], "structure": [], "fixes": [], "text_fixes": []}
    if kind == "request":
        try:
            req = json.loads(src.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ProjectError(f"{src}: not valid JSON ({e})") from None
        blocks = request_blocks(req)
    else:
        from . import textin  # the plain-text and Markdown reader
        # the names list lets the reader drop leading lines that repeat the title, subtitle or author
        blocks = textin.parse(src, ctx["notes"], [project.title, project.subtitle, project.cfg.get("author", "")],
                              code_blocks=opts.get("code_blocks", "keep"))
    log = ctx["structure"]
    # drop title-page lines that repeat the project title, subtitle or author (the title is read once, from the cover)
    blocks = structure.drop_title(blocks, project.title, project.subtitle, project.cfg.get("author", ""), log)
    blocks = structure.contents(blocks, log)  # turn a "Contents" section into list items without page numbers
    blocks = structure.chapter_numbers(blocks, log)  # numbered h1s that restart at 1 inside a chapter become h2s
    blocks = structure.promote(blocks, log)  # with no h1 at all, shift h2/h3 up a level so chapters exist
    for b in blocks:  # repair spacing inside every text block ("intense.The" becomes "intense. The")
        if "text" in b and b["kind"] != "code":  # table rows have cells; code blocks stay exactly as typed
            b["text"] = clean.fix_spacing(b["text"])
    if opts.get("autofix", True) and kind == "text":  # typed text: glued words are only flagged, never split
        blocks = clean.split_glued(blocks, ctx["fixes"], mode="text")
    # apply the project's own whole-word replacements last so they always win
    blocks = clean.apply_text_fixes(blocks, project.cfg.get("text_fixes") or {}, ctx["text_fixes"])
    blocks = structure.tidy(blocks)  # drop empty blocks and escape paragraphs that would read as markup
    return blocks, ctx


# Plain sentences for the report's "review" list: what a person should look at first, most urgent first.
def _review_notes(rep: dict) -> list:
    out = [f"Markup problem: {p}" for p in rep.get("validate", [])]  # these would break later stages
    n = sum(f["count"] for f in rep.get("autofixes", []))
    if n:
        out.append(f"{n} automatic repair(s): see autofixes; undo any with text_fixes.")
    if rep.get("suspects"):
        out.append(f"{len(rep['suspects'])} word(s) to check: see suspects; fix them with project.json text_fixes, "
                   "or give them a reading with spoken.say.")
    for r in rep.get("numbering", []):  # runs of list items that all print the same number ("1. 1. 1.")
        out.append(f"{r['items']} numbered items in a row all read \"{r['number']}\"; kept as written - correct the "
                   "numbers if they should count up.")
    if rep.get("code_blocks"):
        out.append(f"{rep['code_blocks']} code block(s) kept in the text and not read aloud; set spoken.code_names "
                   "to read the code names in the prose the way a programmer says them.")
    if rep.get("structure"):
        out.append(f"{len(rep['structure'])} heading/list decision(s) made from the text's structure: see structure.")
    return out or ["Nothing flagged. Skim the draft once before narration."]


# The extract stage: write work/draft.md and work/extract/report.json and return a short summary.
def run(project) -> dict:
    """Write draft.md and report.json; return a short summary."""
    t0 = time.time()
    project.ensure("extract_dir", "draft")  # create work/extract and the folder of work/draft.md
    blocks, ctx = read_blocks(project)
    text = structure.render(blocks)  # serialise the blocks to the markup, one block per paragraph
    problems = markup.validate(text, project.title)  # check the markup the same way later stages will read it
    draft = project.p("draft")
    draft.write_text(text, encoding="utf-8")  # UTF-8 explicitly so curly quotes and dashes survive on Windows too
    sus = clean.suspects([b for b in blocks if b["kind"] != "code"])  # doubtful words to review (code excluded)
    rep = {
        "input": str(project.input_file().name),  # file name only, not the full path
        "kind": ctx["kind"],
        "draft": str(draft),
        "blocks": len(blocks),
        "words": report.words(blocks),  # word count over text and table cells
        "kinds": report.kinds(blocks),  # block counts per kind
        "code_blocks": sum(1 for b in blocks if b["kind"] == "code"),
        "validate": problems,
        "structure": ctx["structure"],
        "numbering": structure.repeated_numbers(blocks),
        "autofixes": report.group_fixes(ctx["fixes"], blocks),
        "text_fixes": ctx["text_fixes"],
        "suspects": sus,
        "notes": sorted(set(ctx["notes"])),  # parser notes, deduplicated and sorted for a stable report
    }
    rep["review"] = _review_notes(rep)
    rep["seconds"] = round(time.time() - t0, 1)
    project.p("extract_report").write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"draft": str(draft), "blocks": len(blocks), "words": rep["words"], "kinds": rep["kinds"],
            "suspects": len(sus), "autofixes": sum(f["count"] for f in rep["autofixes"]), "problems": problems,
            "seconds": rep["seconds"]}
