# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""work/extract/report.json: what extraction did and what a person should review."""
# Postpone evaluation of annotations so "list | None" is accepted as a hint on older Python 3 releases.
from __future__ import annotations

import re  # regular expressions, used to find a repaired word inside block text
# Counter tallies reasons and kinds; defaultdict groups samples and pages per reason
from collections import Counter, defaultdict


# Find a short excerpt of the draft that shows a repaired word in place, so a reviewer can judge the repair.
# blocks is the final block list, needle the text after the repair, pages the 1-based pages the repair happened on
# (an empty list means any page). Returns about 40 characters either side of the first whole-word match, with "…"
# where the excerpt was cut, or "" when the needle is blank or not found.
def _context(blocks, needle, pages):
    if not needle.strip():  # repairs that deleted something (an empty "to") have nothing to show
        return ""  # no excerpt for an empty replacement
    for b in blocks:  # scan blocks in reading order and stop at the first match
        # paragraph text, or the cells of a table row joined with " | "
        t = b.get("text") or " | ".join(b.get("cells", []))
        # skip blocks that carry page numbers none of which is one of the repair's pages
        if pages and b.get("page") and not set(b.get("pages") or [b.get("page")]) & set(pages):
            continue  # the repair cannot be in this block
        # whole-word match: no letter, digit or apostrophe directly before or after the needle
        m = re.search(r"(?<![\w’'])" + re.escape(needle) + r"(?![\w’'])", t)
        i = m.start() if m else -1  # character offset of the match, -1 when absent
        if i >= 0:  # found in this block
            ctx = t[max(0, i - 40): i + len(needle) + 40]  # the needle plus up to 40 characters of context on each side
            # mark with "…" each side where the block text continues beyond the excerpt
            return ("…" if i > 40 else "") + ctx + ("…" if i + len(needle) + 40 < len(t) else "")
    return ""  # no block on the given pages contains the replacement as a whole word, so no excerpt


# Merge the raw repair log into one entry per distinct repair for the report's "autofixes" section.
# fixlog holds dicts {"page", "from", "to", "why"} from OCR word fixes, quote fixes and glued-word splits; blocks,
# when given, are used to attach a context excerpt to each entry. Returns entries with a count and up to 12
# pages, sorted by reason, then most frequent first, then alphabetically by the original text.
def group_fixes(fixlog: list, blocks: list | None = None) -> list:
    g = {}  # (from, to, why) -> merged entry
    for f in fixlog:  # one log entry per individual repair
        k = (f["from"], f["to"], f.get("why", ""))  # identical repairs share one key
        # create the merged entry the first time this repair is seen
        e = g.setdefault(k, {"from": f["from"], "to": f["to"], "why": f.get("why", ""), "count": 0, "pages": []})
        e["count"] += 1  # count this repair, whether it is the first occurrence or a repeat
        p = f.get("page")  # 1-based page number of this occurrence (absent for some text-input repairs)
        # record each page once, at most 12, to keep the report short
        if p and p not in e["pages"] and len(e["pages"]) < 12:
            e["pages"].append(p)  # add the page so the reviewer knows where to look in the PDF
    # group by reason, most frequent first, then case-insensitive original text for a stable order
    out = sorted(g.values(), key=lambda e: (e["why"], -e["count"], e["from"].lower()))
    if blocks:  # context is only added when the caller passed the blocks
        for e in out:  # for each merged repair, look for an excerpt showing it in the final text
            # show the replacement in context, limited to the pages where it happened
            c = _context(blocks, e["to"], e["pages"])
            if c:  # only when an excerpt was found
                e["context"] = c  # attach the excerpt to the entry
    return out  # one entry per distinct repair, ready for the autofixes section of report.json


# Compress a list of page numbers into readable ranges for the report.
# pages is any iterable of ints (duplicates and order do not matter); returns a string such as "1-3, 6, 8-9",
# or "" for an empty input.
def page_ranges(pages) -> str:
    """[1, 2, 3, 6, 8, 9] -> "1-3, 6, 8-9"."""
    ps = sorted(set(pages))  # unique pages in ascending order
    out, i = [], 0  # output pieces, and the index of the current range start
    while i < len(ps):  # walk the sorted pages one range at a time
        j = i  # j will end on the last page of the current consecutive run
        while j + 1 < len(ps) and ps[j + 1] == ps[j] + 1:  # extend while the next page follows directly
            j += 1  # move the end of the run
        # a single page prints alone, a run prints as "start-end"
        out.append(str(ps[i]) if i == j else f"{ps[i]}-{ps[j]}")
        i = j + 1  # continue after the run
    return ", ".join(out)  # the ranges joined with commas, e.g. 1-3, 6, 8-9


# Count blocks per kind for the report and the CLI summary, in a fixed display order (headings first).
# blocks is the final block list; returns {kind: count} for the listed kinds that occur; other kinds are left out.
def kinds(blocks: list) -> dict:
    c = Counter(b["kind"] for b in blocks)  # tally the "kind" field of every block
    # the fixed display order of the book.md block kinds
    order = ["h1", "h2", "h3", "p", "quote", "verse", "bullet", "num", "th", "tr", "note", "figure", "code", "link"]
    return {k: c[k] for k in order if c[k]}  # skip kinds with a zero count


# Total word count of the draft: whitespace-separated tokens in each block's text, or in its table cells.
def words(blocks: list) -> int:
    # table rows have no "text", so their cells are joined and counted instead
    return sum(len((b.get("text") or " ".join(b.get("cells", []))).split()) for b in blocks)
