# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Chapters, sections and sentences of a docmodel: the export stage writes one audio file per chapter.

Chapters: a block whose kind is in cfg["chapters"]["markers"] starts a chapter, and so does an h2 whose text
is in cfg["chapters"]["extra_titles"]. The cover (block 0) and anything before the first marker belong to the
first chapter, so a marker that would leave the cover alone in a chapter does not split it off. A book
without markers is one chapter.

Sections: the next heading level inside a chapter (the highest heading level among the chapter's headings,
the chapter's own opening heading excluded).
"""
# lets the "int | None" and "list[tuple[int, int]]" hints work without evaluating them
from __future__ import annotations

import re  # regular expressions for sentence endings and "12. Title" headings

HEADINGS = ("h1", "h2", "h3")  # block kinds that count as headings; h1 is the top level
LEVEL = {"h1": 1, "h2": 2, "h3": 3}  # heading kind -> numeric level, so the smallest number is the highest level
# A token ends a sentence when it finishes with . ! or ? optionally followed by closing quotes or a bracket.
SENT_END = re.compile(r'[.!?][”’"\)]*$')  # e.g. 'end."' or 'so?)' both match; '$' anchors at the token's end
# Numbered chapter titles look like "12. The title": optional spaces, digits, a dot, at least one space, then the title.
NUM_TITLE = re.compile(r"\s*(\d+)\.\s+(.*)$")  # group 1 is the number, group 2 the remaining title text


# Normalise the chapter settings from either a Project (which has .cfg) or a plain config dict.
# project_or_cfg: Project, dict or None. Returns {"markers": [...block kinds...], "extra_titles": [...h2 texts...]}.
# Missing or empty settings fall back to markers ["h1"] and no extra titles, the same defaults as project.py.
def _cfg(project_or_cfg) -> dict:
    # a Project carries .cfg; anything else is treated as the config itself; None becomes {}
    cfg = getattr(project_or_cfg, "cfg", project_or_cfg) or {}
    ch = cfg.get("chapters", {}) if isinstance(cfg, dict) else {}  # guard against a non-dict config so .get never fails
    # copies as lists so callers can't mutate the project's settings
    return {"markers": list(ch.get("markers") or ["h1"]), "extra_titles": list(ch.get("extra_titles") or [])}


# True when a docmodel block starts a new chapter.
# block is one docmodel block dict (needs "kind", optionally "text"); chcfg comes from _cfg().
# A block starts a chapter when its kind is listed as a marker, or it is an h2 whose exact text is listed in
# extra_titles.
def is_marker(block: dict, chcfg: dict) -> bool:
    k = block["kind"]  # block kind such as "cover", "h1", "h2", "para", "verse", "tr"
    if k in chcfg["markers"]:  # with the default settings every h1 heading qualifies here
        return True  # a marker kind always opens a chapter
    # extra_titles promotes specific h2 headings (e.g. "Before you begin") to chapter starts
    return k == "h2" and block.get("text") in chcfg["extra_titles"]


# Split the whole docmodel into chapters as half-open block ranges.
# blocks is the docmodel block list; project_or_cfg supplies the marker settings.
# Returns [(first_block, end_block), ...] in reading order; together they cover every block exactly once.
# Block 0 (the cover) always belongs to the first chapter, so a book without markers is one chapter.
def chapter_ranges(blocks: list, project_or_cfg) -> list[tuple[int, int]]:
    """[(first_block, end_block), ...] covering every block, in order."""
    chcfg = _cfg(project_or_cfg)  # marker settings in normalised form
    starts = [0]  # the first chapter always starts at block 0
    for i, b in enumerate(blocks):  # walk every block looking for chapter starts
        if i == 0 or not is_marker(b, chcfg):  # block 0 is already a start, and ordinary blocks never start a chapter
            continue  # skip to the next block, leaving the list of chapter starts unchanged
        # a marker right after the cover (nothing but the cover so far) opens the first chapter instead
        # The check looks at the current chapter so far, from its start to just before this marker.
        # if it holds only cover blocks, splitting here would leave the cover in a chapter of its own
        if all(blocks[j]["kind"] == "cover" for j in range(starts[-1], i)):
            continue  # so the marker joins the cover's chapter instead of opening a new one
        starts.append(i)  # a real chapter boundary: remember where it starts
    # Pair each start with the next start (or the block count for the last chapter) to make half-open ranges.
    # end_block is exclusive, like Python's range()
    return [(a, starts[k + 1] if k + 1 < len(starts) else len(blocks)) for k, a in enumerate(starts)]


# Find the block whose text names a chapter, used for its title and number.
# blocks is the docmodel list, b0/b1 the chapter's half-open range, project_or_cfg the marker settings.
# Returns a block index: the chapter's marker, else its first heading of any level, else b0 (normally the cover).
def opening_block(blocks: list, b0: int, b1: int, project_or_cfg) -> int:
    """The block that names the chapter: its marker, else its first heading, else the cover."""
    chcfg = _cfg(project_or_cfg)  # marker settings in normalised form
    for bi in range(b0, b1):  # first pass: look for the marker that opened this chapter
        # bi is truthy only above 0, so the cover block never counts as the marker
        if bi and is_marker(blocks[bi], chcfg):
            return bi  # the marker heading names the chapter
    for bi in range(b0, b1):  # second pass: a chapter without a marker (e.g. the first one) uses its first heading
        if blocks[bi]["kind"] in HEADINGS:  # the first heading of any level (h1, h2 or h3) names the chapter
            return bi  # first heading found, in reading order
    return b0  # no heading at all: fall back to the first block of the range


# Pick the section headings inside one chapter, shown as sub-entries in the reader's Contents.
# blocks is the docmodel list, b0/b1 the chapter range and opening the block already used as the chapter title.
# Sections are the headings at the highest level present (smallest LEVEL number), the opening heading excluded.
# Returns their block indices in order, or [] when the chapter has no other headings.
def section_blocks(blocks: list, b0: int, b1: int, opening: int) -> list[int]:
    # every heading in the chapter except the one that names it
    heads = [bi for bi in range(b0, b1) if blocks[bi]["kind"] in HEADINGS and bi != opening]
    if not heads:  # nothing besides the chapter title
        return []  # a chapter with only its title heading gets no section list
    # the top heading level used inside this chapter, e.g. 2 when it has h2 and h3 headings
    top = min(LEVEL[blocks[bi]["kind"]] for bi in heads)
    # keep only headings at that level; lower levels are not listed
    return [bi for bi in heads if LEVEL[blocks[bi]["kind"]] == top]


# Split a numbered chapter heading into its number and title.
# text is the heading text (may be None or empty). Returns (number, rest) such as (12, "The title"), or (None, text).
def chapter_number(text: str) -> tuple[int | None, str]:
    """'12. The title' -> (12, 'The title'); otherwise (None, text)."""
    m = NUM_TITLE.match(text or "")  # match at the start only; None is turned into "" so match() never fails
    # int() turns the digits into a number; unnumbered titles come back unchanged
    return (int(m.group(1)), m.group(2)) if m else (None, text)


# Describe every chapter of the docmodel; the export stage (audio.py) calls it.
# blocks is the docmodel block list and project_or_cfg supplies the chapter settings.
# Returns one dict per chapter: b0/b1 block range, opening block, title text, number (only for numbered h1 titles) and
# section block indices.
def chapters(blocks: list, project_or_cfg) -> list[dict]:
    """One dict per chapter: range, opening block, title, number, section blocks."""
    out = []  # one dict per chapter, in reading order
    for b0, b1 in chapter_ranges(blocks, project_or_cfg):  # walk the half-open block ranges
        op = opening_block(blocks, b0, b1, project_or_cfg)  # find the marker or first heading that names this chapter
        head = blocks[op]  # the naming block's dict, holding its kind and text
        # only an h1 title like "3. Name" yields a number; an h2 extra title or the cover never does
        num = chapter_number(head.get("text", ""))[0] if head["kind"] == "h1" else None
        # The title keeps its "12. " prefix here; chapter_number splits it off where a caller needs the bare title.
        # the cover has no "text", so it yields an empty title
        out.append({"b0": b0, "b1": b1, "opening": op, "title": head.get("text", ""), "num": num,
                    "sections": section_blocks(blocks, b0, b1, op)})  # block indices of this chapter's section headings
    return out  # chapter dicts in reading order, one per range


# Group the docmodel's tokens into sentences (the export's word timings carry each word's sentence number).
# blocks is the docmodel block list and flat the docmodel's flat token list (each has "disp", its display text).
# Returns (sent_of, count): sent_of maps token index -> sentence id (0, 1, 2 ...), and count is the number of sentences.
# Rules: punctuation ends a sentence, every block or table cell ends one too, except that verse lines continue it.
# The verse exception is checked after a block's tokens, so an unpunctuated verse line also runs into the first sentence
# of the next block, even a non-verse one.
def sentences(blocks: list, flat: list) -> tuple[dict, int]:
    """token index -> sentence id. A block (a table cell) ends a sentence, except wrapped verse lines."""
    sent_of, count, cur = {}, 0, None  # cur is the open sentence id, None when the next token starts a new sentence
    for b in blocks:  # every docmodel block, in reading order
        # table rows carry one token list per cell; other blocks have a single "tokens" list
        for cell in b.get("cell_tokens") or [b["tokens"]]:
            for i in cell:  # token indices into the flat token list, in reading order
                if cur is None:  # the previous token closed a sentence (or this is the first token)
                    cur = count  # open a new sentence with the next free id
                    count += 1  # the counter always holds the next unused sentence id
                sent_of[i] = cur  # record which sentence this token belongs to
                if SENT_END.search(flat[i]["disp"]):  # the displayed token ends with sentence punctuation
                    cur = None  # so the next token starts a new sentence
            # leaving a cell or a non-verse block closes the sentence; wrapped verse lines keep it open
            if b["kind"] != "verse":
                cur = None  # the next block or cell starts a fresh sentence
    return sent_of, count  # mapping and total number of sentences
