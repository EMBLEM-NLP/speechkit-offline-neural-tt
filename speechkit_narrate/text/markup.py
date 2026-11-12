# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""The book.md format (work/book.md): the single source of the narration script.

Blocks are separated by a blank line. The first characters of a block say what it is:

    # Title                     h1   chapter title (starts a chapter)
    ## Title                    h2   section title
    ### Title                   h3   sub-section title
    > text                      quote   (a boxed quotation)
    | text                      verse   (a centred line of a prayer/poem; consecutive lines are read as one
                                         sentence until one ends with . ! or ?)
    - text                      bullet  (one list item)
    * text                      num     (one numbered item; write the number yourself: "* 1. First step")
    TABLE a | b | c             th      (table header row; cells separated by |)
    ROW a | b | c               tr      (table row)
    NOTE text                   note    (small italic note, e.g. a disclaimer)
    FIGURE file.png | caption   figure  (a figure reference; only the caption is narrated)
    DESC text                   desc    (a description of the figure just above, read after its caption, so a
                                         listener who cannot see it can follow it)
    LINK title | https://...    link    (one source in a list of links; shown, not narrated unless read_links)
    ```                         code    (a code block: lines and indentation kept, never narrated;
    lines                                it may contain blank lines and ends at the next ``` line)
    ```
    anything else               p       (paragraph)

Block 0 is always the cover: the title, read first unless spoken.read_cover is false. The silent cover remains
in the document model so chapter boundaries and display tokens retain their meaning. Inline formatting is not supported: text is narrated exactly
as written (smart quotes and dashes are fine). FIGURE and LINK split at " | " (a bar
with spaces on both sides) - FIGURE at the first, LINK at the last - so a bare bar inside an address stays in it.
"""
# postpones evaluation of type hints such as list[dict], so they are never executed at import time
from __future__ import annotations

import re  # regular expressions, used to collapse the line breaks inside a block

# Every block kind the book.md format knows, in the order the docstring lists them.
# "cover" is never written by hand: parse() always inserts it as block 0 from the project title.
# other modules can check a kind against this tuple instead of hard-coding the list
KINDS = ("cover", "h1", "h2", "h3", "quote", "verse", "bullet", "num", "th", "tr", "note", "figure", "desc", "link",
         "code", "p")
FENCE = "```"  # a line starting with three backticks opens or closes a command block


# Cut book.md text into raw blocks: blank lines separate blocks, except inside a ``` fence, which is kept whole
# (blank lines and indentation included). Returns a list of str; a fenced block keeps its two fence lines.
def _raw_blocks(text: str) -> list[str]:
    out, cur, fenced = [], [], False  # finished blocks, lines of the block being built, inside a fence
    for line in text.replace("\r\n", "\n").split("\n"):  # CRLF normalised to LF, then line by line
        if fenced:  # inside a command block every line belongs to it, blank or not
            cur.append(line)  # kept exactly, indentation included
            if line.strip().startswith(FENCE):  # the closing fence
                out.append("\n".join(cur))  # the whole fenced block, fences included
                cur, fenced = [], False  # back to ordinary blocks
            continue  # next line
        if line.strip().startswith(FENCE):  # an opening fence ends any open block and starts a command block
            if any(x.strip() for x in cur):  # text before the fence with no blank line between
                out.append("\n".join(cur))  # is a block of its own
            cur, fenced = [line], True  # the fence line opens the command block
            continue  # next line
        if not line.strip():  # a blank line ends the current block
            if any(x.strip() for x in cur):  # the block has text
                out.append("\n".join(cur))  # finish it
            cur = []  # start over
            continue  # next line
        cur.append(line)  # an ordinary line of the current block
    if any(x.strip() for x in cur):  # the last block (or an unclosed fence, kept as a command block)
        out.append("\n".join(cur))  # finish it
    return [b.strip("\n") for b in out]  # surrounding empty lines dropped; inner lines untouched


# Turn the text of a book.md file into a list of block dicts, the shape every later stage consumes.
# text is the whole file as a str (Windows CRLF endings are accepted); title is the project title (str) used for the
# cover block.
# Returns [{"kind": ..., "text": ...}] for prose blocks, [{"kind": "th"|"tr", "cells": [...]}] for table rows,
# {"kind": "figure", "src", "text"}, {"kind": "link", "text", "href"} and {"kind": "code", "text"}, cover first.
# It has no side effects; validate(), docmodel.build() and the "check" stage all call it on the same file.
def parse(text: str, title: str) -> list[dict]:
    raw = _raw_blocks(text)  # blocks split on blank lines, command blocks kept whole
    # block 0 is always the cover, so every book has a title screen even with an empty body
    blocks = [{"kind": "cover", "text": title}]
    for b in raw:  # walk the raw blocks in reading order; each one becomes exactly one output block
        if b.lstrip().startswith(FENCE):  # a command block: lines between the fences, exactly as written
            lines = b.split("\n")[1:]  # drop the opening fence line
            if lines and lines[-1].strip().startswith(FENCE):  # and the closing one, when present
                lines = lines[:-1]  # the command lines only
            blocks.append({"kind": "code", "text": "\n".join(x.rstrip() for x in lines)})  # trailing spaces trimmed
            continue  # next block
        b = re.sub(r"\s*\n\s*", " ", b.strip())  # a block is one logical unit; inner line breaks are spaces
        # "FIGURE " prefix: an image file and its caption, split at the first " | " (file names have no "|")
        if b.startswith("FIGURE "):
            body = b[7:]  # split at the first " | " (a bar with spaces), or at the first bare "|" when there is none
            src, _, cap = body.partition(" | ") if " | " in body else body.partition("|")
            blocks.append({"kind": "figure", "src": src.strip(), "text": cap.strip()})  # caption is the text
            continue  # next block
        # "LINK " prefix: a source title and its address, split at the last " | " so a title may contain "|"
        if b.startswith("LINK "):
            body = b[5:]  # split at the last " | " (a bar with spaces), so a bare "|" inside an address stays in it
            ttl, _, href = body.rpartition(" | ") if " | " in body else body.rpartition("|")
            blocks.append({"kind": "link", "text": ttl.strip(), "href": href.strip()})  # title is the text
            continue  # next block
        # "### " prefix: a sub-section title (h3); the 4-character prefix is cut off
        if b.startswith("### "): blocks.append({"kind": "h3", "text": b[4:].strip()})
        # "## " prefix: a section title (h2); "## x" never matches "# ", so the tests do not overlap
        elif b.startswith("## "): blocks.append({"kind": "h2", "text": b[3:].strip()})
        # "# " prefix: a chapter title (h1), which later starts a new chapter in video, web and print
        elif b.startswith("# "): blocks.append({"kind": "h1", "text": b[2:].strip()})
        # "> " prefix: a boxed quotation
        elif b.startswith("> "): blocks.append({"kind": "quote", "text": b[2:].strip()})
        # "| " prefix: one centred verse line; docmodel joins consecutive verse lines into one sentence
        elif b.startswith("| "): blocks.append({"kind": "verse", "text": b[2:].strip()})
        # "- " prefix: one bulleted list item
        elif b.startswith("- "): blocks.append({"kind": "bullet", "text": b[2:].strip()})
        # "* " prefix: one numbered item; the writer types the number in the text itself
        elif b.startswith("* "): blocks.append({"kind": "num", "text": b[2:].strip()})
        # "TABLE " prefix: the header row; each "|"-separated piece becomes a trimmed cell string
        elif b.startswith("TABLE "): blocks.append({"kind": "th", "cells": [c.strip() for c in b[6:].split("|")]})
        # "ROW " prefix: a body row of the table started by the last TABLE line
        elif b.startswith("ROW "): blocks.append({"kind": "tr", "cells": [c.strip() for c in b[4:].split("|")]})
        # "NOTE " prefix: a small italic aside such as a disclaimer
        elif b.startswith("NOTE "): blocks.append({"kind": "note", "text": b[5:].strip()})
        # "DESC " prefix: the spoken description of the figure just above it
        elif b.startswith("DESC "):
            blocks.append({"kind": "desc", "text": b[5:].strip()})  # the text after the prefix
        # no recognised prefix: an ordinary paragraph, kept exactly as written
        else: blocks.append({"kind": "p", "text": b})
    return blocks  # the cover plus one dict per non-empty block of the file


# Check a book.md text for problems that would break narration, video, web or print later on.
# text is the book.md content (str); title is only needed to build the cover block, so the default "x" is a harmless
# stand-in.
# Returns a list of human-readable problem strings, empty when the file is fine; nothing is written or raised.
# The "check" stage prints these, extraction stores them in report.json, and docmodel.build() refuses to continue on
# most of them.
def validate(text: str, title: str = "x") -> list[str]:
    """Problems that would break later stages (empty file, table rows without a header, ...)."""
    probs = []  # problems found so far, in file order
    blocks = parse(text, title)  # reuse the real parser so validation sees exactly what the later stages will see
    if len(blocks) < 2:  # only the cover block exists, meaning the file is empty or whitespace
        # nothing to narrate: every downstream stage would produce an empty result
        probs.append("the book has no text blocks")
    # chapters are built from h1 blocks; without any the book is one long chapter
    if not any(b["kind"] == "h1" for b in blocks):
        # a warning more than an error: docmodel.build() filters this message out and carries on
        probs.append("no '# ' chapter titles: the whole book becomes one chapter")
    for i, b in enumerate(blocks):  # i is the block index reported to the user, counting the cover as block 0
        # a body row must directly follow the header row or another body row
        if b["kind"] == "tr" and (i == 0 or blocks[i - 1]["kind"] not in ("th", "tr")):
            # otherwise the web and print table renderers would get a row with no table around it
            probs.append(f"block {i}: ROW without a TABLE header row before it")
        # table cells must all have text, because every cell is shown and narrated
        if b["kind"] in ("th", "tr") and any(not c for c in b["cells"]):
            # an empty cell would produce a word-less cell in the reader layout
            probs.append(f"block {i}: empty table cell")
        if "text" in b and not b["text"]:  # blocks with a "text" field (everything except table rows) must not be empty
            # in practice this mostly catches a cover block built from an empty project title
            probs.append(f"block {i}: empty {b['kind']}")
        if b["kind"] == "figure" and not b.get("src"):  # a FIGURE line names its image file before the "|"
            probs.append(f"block {i}: FIGURE without an image file name")  # nothing could be shown
        # a description belongs to the figure right above it (or continues the description before it)
        if b["kind"] == "desc" and blocks[i - 1]["kind"] not in ("figure", "desc"):
            probs.append(f"block {i}: DESC without a FIGURE right above it")  # it would describe nothing on screen
        # a LINK line ends with a web (or mail) address after the last "|"
        if b["kind"] == "link" and not re.match(r"(https?://|mailto:)\S+$", b.get("href", "")):
            probs.append(f"block {i}: LINK without a web address after the last '|'")  # the link would go nowhere
    return probs  # the caller decides how serious the list is


# Write a block list back out as book.md text: the inverse of parse(), so parse(dump(parse(t))) gives the same blocks.
# blocks is a list of block dicts in parse() shape; the cover block, if present, is skipped because parse() re-creates
# it.
# Returns the book.md text as one str with blocks separated by a blank line and a final newline; nothing is written to
# disk.
# The test suite and the extraction fixtures use it; extract/structure.py has its own copy named render().
def dump(blocks: list[dict]) -> str:
    """Inverse of parse (cover block skipped)."""
    # the line prefix for each text block kind; th and tr are written separately below
    pre = {"h1": "# ", "h2": "## ", "h3": "### ", "quote": "> ", "verse": "| ", "bullet": "- ", "num": "* ",
           "note": "NOTE ", "desc": "DESC ", "p": ""}
    out = []  # one string per block, joined at the end
    for b in blocks:  # keep the given order, since book.md is read top to bottom
        k = b["kind"]  # the kind decides which prefix is written
        if k == "cover":  # the cover comes from project.json, never from the file
            continue  # skip it so it does not turn into a stray paragraph
        if k == "th":  # a table header row
            out.append("TABLE " + " | ".join(b["cells"]))  # cells joined with " | " so parse() can split them again
        elif k == "tr":  # a table body row
            out.append("ROW " + " | ".join(b["cells"]))  # same cell layout as the header
        elif k == "figure":  # an image and its caption
            out.append(f"FIGURE {b['src']} | {b['text']}")  # file name first, caption after the "|"
        elif k == "link":  # a source title and its address
            out.append(f"LINK {b['text']} | {b['href']}")  # the address goes last, after the last "|"
        elif k == "code":  # a command block, lines kept
            out.append(f"{FENCE}\n{b['text']}\n{FENCE}")  # fenced, so blank lines inside it survive
        else:  # every other kind is a prefix followed by the text
            out.append(pre[k] + b["text"])  # a KeyError here means the block kind is not part of the format
    return "\n\n".join(out) + "\n"  # blank line between blocks, plus a trailing newline so the file ends cleanly
