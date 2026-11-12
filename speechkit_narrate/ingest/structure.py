# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Block-list repairs shared by PDF and text input.

Blocks are dicts: {"kind": h1|h2|h3|quote|verse|bullet|num|th|tr|note|figure|link|code|p, "text" | "cells", "page",
...}; a figure also has "src" (its image file) and a link "href" (its web address).
"""
# Postpone evaluation of annotations so hints such as "list" on parameters stay cheap at import time.
from __future__ import annotations

import difflib  # SequenceMatcher gives a 0..1 similarity ratio between two strings
import re  # regular expressions for numbering, page numbers and markup prefixes
from functools import lru_cache  # memoises the chapter-numbering search

from .common import words_key  # normalises text to lowercase words for similarity comparisons

# A heading that introduces a table of contents, in English and a few other languages (Spanish, French, German).
CONTENTS = re.compile(r"^(table of )?contents$|^contents:?$|^índice$|^sommaire$|^inhalt$", re.I)
# A list number at the start of a line: "12. ", "(iv) ", "a) ". Case-insensitive, so Roman numerals and single
# letters of either case count.
NUM_PREFIX = re.compile(r"^\(?(\d{1,3}|[ivxlc]{1,6}|[A-Za-z])[.)]\s+", re.I)
# A trailing page number, either after dot leaders ("Prayer ....... 14") or after plain spaces ("Prayer 14").
# It also accepts Roman page numbers, so a title that ends in a Roman numeral ("Part II") matches as well.
LEADER = re.compile(r"(\s*\.{2,}\s*|\s+)(\d{1,4}|[ivxlc]{1,6})$", re.I)


# How alike two short texts are, from 0.0 (nothing in common) to 1.0 (identical), ignoring case and punctuation.
# Used to compare title-page lines with the project title and contents entries with later headings.
def _similar(a: str, b: str) -> float:
    a, b = words_key(a), words_key(b)  # reduce both to lowercase letters, digits and spaces
    if not a or not b:  # an empty side (only punctuation, or an empty setting) never matches
        return 0.0  # zero similarity, so an empty setting can never cause a line to be dropped
    # autojunk=False stops difflib from ignoring frequent characters, which would distort short strings
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


# Remove title-page lines that only repeat the project's title, subtitle or author, because the book's cover
# block already shows them. blocks is the block list, title/subtitle/author come from project.json, and log
# receives one {"removed", "why"} entry per dropped block (the report's "structure" section).
# Returns a new list; only the first few blocks before the first real paragraph are candidates.
def drop_title(blocks: list, title: str, subtitle: str, author: str, log: list) -> list:
    """Title-page lines that repeat the project title/subtitle/author: the cover block already shows them."""
    out = list(blocks)  # work on a copy so the caller's list is untouched
    i = 0  # counter of kept blocks; incremented below but never read
    first_text = None  # index of the first real paragraph, if any
    for k, b in enumerate(out):  # find where the book's prose starts
        # a paragraph of more than 12 words is real text, not title-page material
        if b["kind"] == "p" and len(b.get("text", "").split()) > 12:
            first_text = k  # index where the prose begins; title-page candidates must come before it
            break  # only the first one matters
    # candidates are the first six blocks at most, and never past the first real paragraph
    limit = min(len(out), 6 if first_text is None else min(first_text, 6))
    page_words = {}  # 1-based page number -> words on that page
    for b in out:  # count words per page over all blocks
        if b.get("page"):  # text input blocks have no page and are skipped here
            # add this block's words to its page total
            page_words[b["page"]] = page_words.get(b["page"], 0) + len(b.get("text", "").split())
    keep = []  # the blocks that will be returned, in their original order
    for k, b in enumerate(out):  # second pass: decide each block
        t = b.get("text", "")  # table rows have no "text" and count as empty here
        # a page with at most 60 words looks like a title page; blocks without a page always qualify
        title_page = not b.get("page") or page_words.get(b["page"], 0) <= 60
        # A candidate is a short (at most 14 words) heading, paragraph, note or verse line near the start, on a sparse
        # page,
        # that is neither a numbered item nor a "Chapter/Part/Book ..." heading, which must always be kept.
        if k < limit and title_page and b["kind"] in ("h1", "h2", "h3", "p", "note", "verse") \
                and len(t.split()) <= 14 \
                and not NUM_PREFIX.match(t) and not re.match(r"(?i)(chapter|part|book)\s+\w+", t):  # no number or label
            # best similarity against title, subtitle, author, title plus subtitle, and "by" plus author
            best = max((_similar(t, x) for x in (title, subtitle, author, f"{title} {subtitle}",
                                                    # empty settings are skipped; default=0 covers the case where every
                                                    # setting is empty
                                                    f"by {author}") if x), default=0)
            if best >= 0.85:  # 85% similar or more: the line repeats the cover text (small OCR differences allowed)
                # record what was dropped and why
                log.append({"removed": t, "why": "title page (matches project title/subtitle/author)"})
                continue  # leave it out of the result
        keep.append(b)  # any other block is kept
        i += 1  # bookkeeping only; nothing uses this count
    return keep  # the blocks without title-page duplicates


# Turn a table of contents into a list. When a "Contents" heading is found, the blocks after it are entries until
# the book proper starts (a heading that repeats an entry, a long paragraph, a table, or a heading followed by
# prose). Entries become "num" or "bullet" items and trailing page numbers are removed when most entries have
# them. blocks is the block list, log receives one {"block", "was", "now", "why"} entry per changed block.
def contents(blocks: list, log: list) -> list:
    """A "Contents" heading is followed by entries until the first heading that repeats an entry.
    Entries become list items (numbered when most are numbered); trailing page numbers are dropped."""
    out = list(blocks)  # copy so replacements do not alter the caller's list
    for i, b in enumerate(out):  # look at every block; entries changed below are seen with their new kind
        # only a heading whose text is a contents title starts the work
        if not (b["kind"].startswith("h") and CONTENTS.match(b.get("text", "").strip())):
            continue  # anything else is skipped
        entries = []  # indexes of the blocks that are contents entries
        j = i + 1  # the first candidate entry follows the heading
        seen = []  # entry texts without numbers or page numbers, to recognise the chapter headings later
        # scan forward, at most 120 blocks, so a missed end cannot swallow the book
        while j < len(out) and j - i <= 120:
            c = out[j]  # the block being tested as a possible contents entry
            t = c.get("text", "")  # its text ("" for table rows)
            if c["kind"] in ("th", "tr"):  # a table is never part of a contents list
                break  # a table marks the end of the contents list
            # the entry's bare title: dot leaders, page number and list number removed
            base = NUM_PREFIX.sub("", LEADER.sub("", t)).strip()
            # a long paragraph, quote or note (over 16 words) is prose, so the contents list has ended
            if c["kind"] in ("p", "quote", "note") and len(t.split()) > 16:
                break  # end of the list
            # a heading needs more care: it may be an entry set in heading type, or the first chapter
            if c["kind"].startswith("h"):
                twin = _has_later_twin(out, j)  # does the same heading appear again later in the book?
                # it repeats an entry and has no later copy: this is the chapter itself
                if any(_similar(base, s) >= 0.9 for s in seen) and not twin:
                    # That heading is where the book proper starts.
                    break                    # a heading repeating an entry: the book proper starts here
                if not twin:  # a heading with no later copy may still be an entry
                    nxt = out[j + 1] if j + 1 < len(out) else None  # the block after it, if any
                    # nothing follows, or prose of more than 12 words follows
                    if nxt is None or (nxt["kind"] in ("p", "quote", "verse", "note")
                                       # the second half of that test: the next block is a long text block
                                       and len(nxt.get("text", "").split()) > 12):
                        # Such a heading starts real content.
                        break                # a section of its own (followed by prose), not an entry
            entries.append(j)  # accept the block as an entry
            seen.append(base)  # remember its bare title
            j += 1  # move on to test the following block as an entry
        if len(entries) < 2:  # fewer than two entries is not a contents list
            continue  # leave the blocks unchanged
        # how many entries start with a list number
        nums = sum(1 for k in entries if NUM_PREFIX.match(out[k].get("text", "")))
        # numbered list when at least half are numbered, otherwise a bullet list
        kind = "num" if nums >= 0.5 * len(entries) else "bullet"
        # how many entries end with a page number
        pagenos = sum(1 for k in entries if LEADER.search(out[k].get("text", "")))
        # strip page numbers only when at least 60% of entries carry one, so ordinary titles are not cut
        strip_pages = pagenos >= 0.6 * len(entries)
        for k in entries:  # convert each accepted entry into a list item
            t = out[k].get("text", "")  # the entry's text before any page number is stripped
            if strip_pages:  # page numbers are being removed
                # drop the trailing page number; keep the original if nothing would be left
                t = LEADER.sub("", t).strip() or t
            if out[k]["kind"] != kind or t != out[k].get("text"):  # log only real changes (kind or text)
                # record the before and after kinds for the report
                log.append({"block": t, "was": out[k]["kind"], "now": kind, "why": "contents entry"})
            out[k] = dict(out[k], kind=kind, text=t)  # replace the block with a copy carrying the new kind and text
    return out  # the repaired block list


# Does the heading at index j have a near-identical heading later in the list? Contents entries set in heading type
# have such a twin (the chapter heading itself); the first chapter heading after the contents usually does not.
def _has_later_twin(out, j) -> bool:
    t = NUM_PREFIX.sub("", out[j].get("text", ""))  # compare titles without their list numbers
    # a later heading at least 90% similar
    return any(x["kind"].startswith("h") and _similar(NUM_PREFIX.sub("", x.get("text", "")), t) >= 0.9
               for x in out[j + 1:])  # searches every block after j


# Fix chapter levels when the PDF set numbered sub-sections in the same type as chapters. Numbered h1 headings
# should count 1, 2, 3...; numbers that restart at 1 inside a chapter are sub-sections and become h2.
# blocks is the block list, log receives one entry per demoted heading. Returns a new list, or the same list when
# there are too few numbered h1s or no restart.
def chapter_numbers(blocks: list, log: list) -> list:
    """Numbered h1s should count 1, 2, 3...; a run that restarts at 1 inside a chapter is a set of
    sub-sections (demoted to h2). Solved as a small search for the labelling with fewest breaks."""
    idx = [i for i, b in enumerate(blocks) if b["kind"] == "h1"]  # positions of all h1 blocks
    nums = []  # the number each h1 starts with, or None
    for i in idx:  # read the leading number of each h1 heading
        # "3. Title", "3) Title", "Chapter 3: Title"; the added space lets a bare number match too
        m = re.match(r"^(?:chapter\s+)?(\d{1,3})[.):]?\s", blocks[i]["text"] + " ", re.I)
        nums.append(int(m.group(1)) if m else None)  # keep the number as an int, None for unnumbered headings
    seq = [(i, n) for i, n in zip(idx, nums) if n is not None]  # (block index, number) for the numbered h1s only
    # needs at least 4 numbered h1s and a later one that restarts at 1
    if len(seq) < 4 or not any(n == 1 for _, n in seq[1:]):
        return blocks  # no restart to explain, so the original list is returned as is

    # Dynamic programming over the numbered headings: best(k, chap, sub) is the cheapest labelling of headings k..end
    # given the last chapter number chap and the last sub-section number sub (0 when none is open).
    # It returns (cost, labels) where each label is True for "demote to h2". Memoised, so each state is solved once.
    @lru_cache(maxsize=None)
    def best(k, chap, sub):  # recursive helper closed over seq
        if k == len(seq):  # past the last heading
            return 0.0, ()  # nothing left costs nothing
        n = seq[k][1]  # the number on heading k
        opts = []  # candidate (cost, labels) pairs
        # keeping it as a chapter is free only when it continues the chapter count
        c_cost = 0.0 if n == chap + 1 else 1.0
        c, path = best(k + 1, n, 0)  # label the rest with this heading as the latest chapter and no sub-section open
        # the 0.01 bonus prefers keeping a heading as a chapter when both choices cost the same
        opts.append((c + c_cost - 0.01, (False,) + path))
        # it can be a sub-section if it starts a new run at 1 or continues the open run
        if n == 1 or (sub and n == sub + 1):
            c, path = best(k + 1, chap, n)  # chapter count unchanged; this number becomes the open sub-section
            opts.append((c, (True,) + path))  # demote to h2 with no cost added, so restarts inside a chapter are cheap
        return min(opts, key=lambda t: t[0])  # the cheaper option wins
    import sys  # needed only for the recursion limit
    old = sys.getrecursionlimit()  # current recursion limit, restored afterwards
    # the search recurses once per heading, plus wrapper frames; raise the limit for long books
    sys.setrecursionlimit(max(old, 4 * len(seq) + 100))
    try:  # make sure the limit is restored even on error
        _, labels = best(0, 0, 0)  # labels for all numbered headings, from the starting state "no chapter yet"
    finally:  # runs even if the search raises, for example a RecursionError
        sys.setrecursionlimit(old)  # put the original limit back
    out = list(blocks)  # copy before changing kinds
    for (i, n), demote in zip(seq, labels):  # pair each numbered heading with its label
        if demote:  # labelled as a sub-section
            out[i] = dict(out[i], kind="h2")  # a copy of the block with kind h2
            log.append({"block": out[i]["text"], "was": "h1", "now": "h2",  # record the demotion for the report
                        "why": "numbering restarts inside a chapter (sub-section)"})
    return out  # the corrected block list


# Ensure chapters exist: when the book has no h1 at all but does have h2 or h3 headings, every heading moves up one
# level (or h3 straight to h1 when there is no h2). Later stages start chapters at h1 blocks.
# Logs one entry describing the mapping; returns a new list, or the input when nothing needs changing.
def promote(blocks: list, log: list) -> list:
    """No h1 at all but h2/h3 present: shift every heading up one level (chapters must be h1)."""
    kinds = {b["kind"] for b in blocks}  # the set of kinds present
    if "h1" in kinds or not ({"h2", "h3"} & kinds):  # there are chapters already, or no headings at all
        return blocks  # nothing to promote: the caller's list comes back untouched
    m = {"h2": "h1", "h3": "h2"}  # the usual shift: h2 becomes chapter, h3 becomes section
    if "h2" not in kinds:  # only h3 headings exist
        m = {"h3": "h1"}  # make them chapters directly
    # one log line such as "promoted h2->h1, h3->h2"
    log.append({"why": "no top-level headings: promoted " + ", ".join(f"{a}->{b}" for a, b in m.items())})
    # copy each block with its kind mapped; other kinds keep theirs
    return [dict(b, kind=m.get(b["kind"], b["kind"])) for b in blocks]


# Final clean-up before the draft is written: drop empty blocks and make every block safe to write as one
# book.md paragraph. Table cells lose "|" (the cell separator), paragraphs that begin like markup are escaped,
# and a table row without a header above it becomes a header. Returns a new list of block dicts.
def tidy(blocks: list) -> list:
    """Drop empty blocks and make every block one line that cannot be mistaken for another kind."""
    out = []  # blocks after the first pass
    # the line starts that markup.parse would treat as another block kind
    pre = re.compile(r"^(#{1,3} |> |\| |- |\* |TABLE |ROW |NOTE |FIGURE |DESC |LINK |```)")
    for b in blocks:  # first pass: clean each block's text or cells
        if b["kind"] in ("th", "tr"):  # table header and table rows
            # collapse whitespace, turn "|" into "/" so it cannot split a cell, and fill an empty cell with an em dash
            cells = [re.sub(r"\s+", " ", c.replace("|", "/")).strip() or "—" for c in b["cells"]]
            out.append(dict(b, cells=cells))  # a copy with the cleaned cells
            continue  # cells done; next block
        if b["kind"] == "code":  # a command block keeps its lines and indentation; only line ends are trimmed
            lines = [x.rstrip() for x in b.get("text", "").split("\n")]  # each line without trailing spaces
            while lines and not lines[0]:  # empty lines at the top
                lines.pop(0)  # dropped
            while lines and not lines[-1]:  # empty lines at the bottom
                lines.pop()  # dropped
            if lines:  # an empty command block is dropped
                out.append(dict(b, text="\n".join(lines)))  # the cleaned command block
            continue  # next block
        t = re.sub(r"\s+", " ", b.get("text", "")).strip()  # one line with single spaces
        if not t or not re.search(r"\w", t):  # no letters or digits at all
            # Such a block is dropped.
            continue                    # empty, or an ornament / scene break such as "* * *"
        if b["kind"] == "num":  # a numbered list item whose text may repeat its own number
            # Drop a list marker duplicated by a numbered title.
            t = re.sub(r"^(\d+)\.\s+\1\.\s+", r"\1. ", t)     # "1. 1. Title": list marker + numbered title
        if b["kind"] == "p" and pre.match(t):  # a paragraph that would be misread as markup
            # a paragraph that happens to start like markup ("- ", "> ", "# "...): keep it a paragraph
            # "- " becomes an en dash with a space; otherwise the first space becomes a no-break space so the prefix no
            # longer matches.
            t = "\u2013 " + t[2:] if t.startswith("- ") else t.replace(" ", "\u00a0", 1)
        out.append(dict(b, text=t))  # keep the cleaned block
    # table rows must follow a header
    fixed = []  # blocks after the second pass
    for b in out:  # second pass over the cleaned blocks to check the table row order
        # a row with no header (or row) before it would fail markup.validate
        if b["kind"] == "tr" and (not fixed or fixed[-1]["kind"] not in ("th", "tr")):
            b = dict(b, kind="th")  # promote it to a header row
        fixed.append(b)  # keep the block, possibly changed into a header row
    return fixed  # the tidied block list


# Find runs of numbered items that all print the same number, such as three items each reading "1.".
# The source restarted the list at every item; the run is reported for a person to check, never renumbered.
# Returns one dict per run with the page, item count, the repeated number and the first 60 characters.
def repeated_numbers(blocks: list) -> list:
    """Runs of numbered items that all carry the same number ("1. 1. 1."): the source restarted the list
    at every item. Reported, never renumbered."""
    out, run = [], []  # results, and the current run of (number, block) pairs
    for b in blocks + [{"kind": "end"}]:  # a sentinel block at the end flushes the last run
        # leading "N." of a numbered item; None for any other block
        m = re.match(r"^(\d+)\.", b.get("text", "")) if b["kind"] == "num" else None
        if m and (not run or run[-1][0] == m.group(1)):  # a numbered item that starts a run or repeats the run's number
            run.append((m.group(1), b))  # this item repeats the run's number, so it joins the run
            continue  # the run is still open; check the following block
        if len(run) >= 2:  # the run just ended; two or more equal numbers are worth reporting
            # page of the first item, run length, the repeated number
            out.append({"page": run[0][1].get("page"), "items": len(run), "number": run[0][0] + ".",
                        "first": run[0][1]["text"][:60]})  # start of the first item's text for context
        run = [(m.group(1), b)] if m else []  # start a new run with this item, or none for other kinds
    return out  # one dict per suspicious run, stored under numbering in the extraction report


# Serialise blocks into book.md markup: one block per paragraph, separated by blank lines, each starting with its
# kind's prefix (see text/markup.py). This duplicates markup.dump() except that there is no cover block to skip.
def render(blocks: list) -> str:
    # prefix per kind; "* " marks a numbered item whose number is part of the text
    pre = {"h1": "# ", "h2": "## ", "h3": "### ", "quote": "> ", "verse": "| ", "bullet": "- ", "num": "* ",
           "note": "NOTE ", "desc": "DESC ", "p": ""}
    lines = []  # one markup paragraph per block
    for b in blocks:  # write each block as one markup paragraph
        k = b["kind"]  # the block kind decides the line prefix
        if k == "th":  # a table header row is written as a TABLE line
            lines.append("TABLE " + " | ".join(b["cells"]))  # "TABLE" then the cells separated by " | "
        elif k == "tr":  # a table body row is written as a ROW line
            # the ROW keyword and the cells, separated as in the header line
            lines.append("ROW " + " | ".join(b["cells"]))
        elif k == "figure":  # an image and its caption, as markup.dump writes it
            lines.append(f"FIGURE {b['src']} | {b['text']}")  # file name, then the caption
        elif k == "link":  # a source title and its web address
            lines.append(f"LINK {b['text']} | {b['href']}")  # the address after the last "|"
        elif k == "code":  # a command block between ``` fences, lines kept
            lines.append("```\n" + b["text"] + "\n```")  # fenced so its blank lines and indentation survive
        else:  # headings, quotes, verse, list items, notes and paragraphs
            lines.append(pre[k] + b["text"])  # prefix plus the block's single-line text
    return "\n\n".join(lines) + "\n"  # blank line between blocks and a final newline
