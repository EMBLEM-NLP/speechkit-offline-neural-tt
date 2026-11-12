# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Plain-text and Markdown input -> blocks.

Markdown: # / ## / ### (and setext === / ---) headings, > quotes, -/*/+ bullets, 1. numbered items,
pipe tables with a separator row; inline emphasis, code ticks, links and images are reduced to their
text; lines ending in a hard break (two spaces or a backslash) make verse lines.

Plain text: paragraphs are separated by blank lines and hard-wrapped lines are joined. Headings are
found heuristically: "Chapter 3" / "CHAPTER III" / "Part One" / "Prologue"..., lines underlined with
=== or ---, and short ALL-CAPS or Title-Case lines without closing punctuation. Short-line
paragraphs (poems, addresses) become verse lines; indented paragraphs become quotes.
"""
from __future__ import annotations  # hints such as list[str] | None are kept as strings and never evaluated

import re  # all parsing here is line-based regular-expression work
import statistics  # median line lengths and indents, used to detect hard-wrapped text

from .common import known, zipf  # dictionary and frequency lookups, used to decide whether a line-end hyphen is soft

# ------------------------------------------------------------------ inline

# Reduce Markdown inline markup in one line or block to plain text, as book.md has no inline formatting.
# s is the text (str); returns it with links, images, code ticks, emphasis, HTML tags and comments removed and spaces
# collapsed.
# Backslash-escaped characters are protected first so that "\*" stays a literal asterisk.
def strip_inline(s: str) -> str:
    esc = []  # the characters that were backslash-escaped, in order of appearance

    # Store one escaped character and leave a placeholder made of private-use characters, which no regex below touches.
    # m is the regex match; returns the placeholder text with the stored item's index.
    def keep(m):
        esc.append(m.group(1))  # remember the escaped character itself
        return f"\ue000{len(esc) - 1}\ue001"  # <index> marks where it goes back later
    s = re.sub(r"\\([\\`*_{}\[\]()#+\-.!>|])", keep, s)          # backslash escapes are literal
    s = re.sub(r"<!--.*?-->", "", s)  # HTML comments vanish entirely
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", "", s)                     # images
    s = re.sub(r"\[\^[^\]]+\]", "", s)                                # footnote refs
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)                    # [text](url)
    s = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", s)                   # [text][ref]
    s = re.sub(r"<((?:https?|mailto):[^>]+)>", r"\1", s)              # <http://...>
    s = re.sub(r"</?[A-Za-z][^>]*>", "", s)                           # html tags
    s = re.sub(r"`{1,3}([^`]+)`{1,3}", r"\1", s)  # `code`, ``code`` and ```code``` keep only the code text
    # **bold** and __bold__ keep their text; the lookarounds require non-space at both inner ends
    s = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"\2", s)
    # *italic* keeps its text, unless the asterisks touch letters (as in 2*3*4)
    s = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"\1", s)
    # _italic_ keeps its text, unless the underscores sit inside a word such as snake_case
    s = re.sub(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])", r"\1", s)
    s = re.sub(r"~~(.+?)~~", r"\1", s)  # ~~strikethrough~~ keeps its text
    # put the escaped characters back as plain characters
    s = re.sub("\ue000(\\d+)\ue001", lambda m: esc[int(m.group(1))], s)
    # decode the few HTML entities that appear in Markdown; &amp; goes first, so "&amp;lt;" ends up as "<"
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", s).strip()  # collapse whitespace runs and trim, giving a single clean line


# Join the lines of one hard-wrapped paragraph into a single line of text.
# lines is a list of str; a hyphen at a line end is removed when the joined word is real ("exam-" + "ple"),
# and kept when both halves are words but the join is not ("well-" + "known"). Returns the joined str.
# A line ending in an unspaced em dash is joined with no space, matching how the dash was printed.
def join_lines(lines: list[str]) -> str:
    """Join hard-wrapped lines, removing soft line-end hyphens ("exam-\\nple" -> "example")."""
    out = ""  # the paragraph built so far
    for ln in lines:  # each physical line in order
        ln = ln.strip()  # indentation and trailing spaces are irrelevant once joined
        if not ln:  # blank lines contribute nothing
            continue  # a blank line in the list adds neither text nor an extra space
        if not out:  # the first line starts the result
            out = ln  # the first non-blank line starts the result, with no leading space
            continue  # move on to the following physical line
        m = re.search(r"([A-Za-z’']+)-$", out)  # a word ending in a hyphen at the end of the text so far
        f = re.match(r"([a-z][A-Za-z’']*)", ln)  # the next line starts with a lower-case word
        if m and f and not out.endswith("--"):  # both found, and not a double-hyphen dash
            joined = m.group(1) + f.group(1)  # the word that would result from removing the hyphen
            # a real or common word, or halves that are not both words: the hyphen was a soft line-end break
            if known(joined) or zipf(joined) >= 2.5 or not (known(m.group(1)) and known(f.group(1))):
                out = out[:-1] + ln  # drop the hyphen and glue the lines together
                continue  # hyphen removed and lines fused, so the space-joining below is skipped
            out += ln  # both halves are words and the join is not: a real compound, so keep the hyphen
            continue  # the compound is glued as written: "well-" + "known" stays "well-known"
        if out.endswith("—") and not out.endswith(" —"):  # a closed em dash (no space before it)
            out += ln  # continues straight on without a space
        else:  # any other line end is an ordinary word boundary
            out += " " + ln  # lines are separated by one space
    return out  # the paragraph as one line


# ------------------------------------------------------------------ markdown

# Markdown table separator row such as |---|:---:|, with optional outer pipes and alignment colons.
# two or more dashes per column are required
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# thematic break: three or more *, - or _ (spaces allowed between them); it is skipped
HR = re.compile(r"^\s{0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$")
# ATX heading: 1 to 6 #, the title, and an optional closing run of #
ATX = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
# bullet item marked with -, *, + or a bullet character; group 1 is the indent, group 3 the text
BUL = re.compile(r"^(\s*)([-*+•])\s+(.*)$")
# numbered item "12." or "12)"; groups: indent, number, delimiter, text
NUM = re.compile(r"^(\s*)(\d{1,4})([.)])\s+(.*)$")


# Split one Markdown table line into plain-text cells.
# line is the raw line (str); returns a list of cell strings with inline markup removed.
def _cells(line: str) -> list[str]:
    s = line.strip()  # trim the row so its outer pipes sit at the very start and end
    if s.startswith("|"):  # a row may begin with a pipe, which marks no cell of its own
        s = s[1:]  # without the leading pipe
    if s.endswith("|") and not s.endswith("\\|"):  # optional trailing pipe, unless it is an escaped literal "\|"
        s = s[:-1]  # without the trailing pipe
    # split on pipes not preceded by a backslash, then clean each cell
    return [strip_inline(c) for c in re.split(r"(?<!\\)\|", s)]


# Parse a Markdown file into book blocks.
# text is the whole file (str); notes is a list that collects remarks for the report (code blocks, images dropped).
# code_blocks is project.json ingest.code_blocks: "keep" (a fenced block stays a code block, never spoken) or "read".
# Returns a block list of {"kind", "text"} or {"kind", "cells"} dicts for the structure and clean steps.
# Nested lists are flattened, h4 to h6 become h3, and hard-break lines in a plain paragraph become verse lines.
def parse_md(text: str, notes: list, code_blocks: str = "keep") -> list:
    # unify Windows and old Mac line endings, and drop a UTF-8 byte order mark
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)  # remove HTML comments, including ones spanning several lines
    lines = text.split("\n")  # work line by line from here on
    if lines and lines[0].strip() == "---":                       # YAML front matter
        for k in range(1, min(len(lines), 80)):  # look for the closing marker within the first 80 lines
            if lines[k].strip() in ("---", "..."):  # "---" or "..." ends the front matter
                lines = lines[k + 1:]  # drop the metadata block entirely
                break  # stop at the first closing marker
    blocks = []  # finished block dicts in reading order, the function's result
    para: list[str] = []  # lines of the paragraph being collected
    item = None            # current list item block
    quote: list[str] | None = None  # lines of the block quote being collected, None when not in a quote

    # Turn whatever is being collected (paragraph, list item, block quote) into finished blocks.
    # Called whenever a structural change or a blank line ends the current element.
    def flush():
        nonlocal para, item, quote  # these are assigned below, so they must refer to parse_md's variables
        if para:  # a paragraph is pending
            # which lines end with a hard break: two or more spaces or a backslash
            hard = [bool(re.search(r"( {2,}|\\)$", ln)) for ln in para]
            if len(para) >= 2 and all(hard[:-1]):  # every line except the last ends in a hard break: a poem or address
                for ln in para:  # each line becomes its own verse block
                    t = strip_inline(re.sub(r"\\$", "", ln))  # remove the backslash marker and inline markup
                    if t:  # skip lines that end up empty
                        blocks.append({"kind": "verse", "text": t})  # a centred verse line
            else:  # an ordinary wrapped paragraph
                t = strip_inline(join_lines(para))  # joined into one line with markup removed
                if t:  # skip if nothing remains
                    # an ordinary paragraph block, its wrapped lines joined into one
                    blocks.append({"kind": "p", "text": t})
            para = []  # start a fresh paragraph
        if item is not None:  # a list item is pending
            # join its lines and remove the temporary "_lines" field
            item["text"] = strip_inline(join_lines(item.pop("_lines")))
            if item["text"]:  # an item whose text vanished after stripping markup is dropped
                blocks.append(item)  # the item dict becomes a block as it is
            item = None  # no item pending any more
        if quote is not None:  # a block quote is pending
            # quoted verse (hard breaks) or quoted paragraphs separated by "> " blank lines
            chunks, cur = [], []
            for ln in quote:  # each line collected for the pending quote, in order
                if not ln.strip():  # a blank quote line (a bare ">") separates the quote's paragraphs
                    if cur:  # only when the chunk being collected has lines in it
                        chunks.append(cur)  # keep the finished chunk as one paragraph's lines
                    cur = []  # start collecting the next chunk
                else:  # a quote line with text
                    cur.append(ln)  # belongs to the chunk being collected
            if cur:  # lines after the last blank quote line are still pending
                chunks.append(cur)  # keep them as the final chunk
            for ch in chunks:  # each chunk becomes one quote block; hard breaks inside it are joined, not kept as verse
                t = strip_inline(join_lines(ch))  # the chunk's lines joined into one string, with inline markup removed
                if t:  # skip a chunk whose text vanished once the markup was stripped
                    # one quote block per chunk, so quoted paragraphs stay separate
                    blocks.append({"kind": "quote", "text": t})
            quote = None  # the quote is finished; the next ">" line starts a new one

    i = 0  # index of the line being examined, advanced by hand inside the loop
    n = len(lines)  # total line count: the loop's end and the bound for look-ahead checks
    while i < n:  # manual index loop, because some constructs consume several lines at once
        ln = lines[i]  # the raw line with its indentation, which some patterns depend on
        s = ln.strip()  # the line without surrounding whitespace
        if re.match(r"^\s{0,3}(```|~~~)", ln):  # a fenced code block opening with ``` or ~~~
            fence = s[:3]  # the fence characters that will close it
            flush()  # close any paragraph, item or quote before the code
            code = []  # lines inside the fence
            i += 1  # step past the opening fence
            # until a line starting with the same fence (or the end of the file)
            while i < n and not lines[i].strip().startswith(fence):
                code.append(lines[i])  # keep the code line
                i += 1  # advance inside the fence
            if code_blocks == "read":  # ingest.code_blocks "read": its non-empty lines become one spoken paragraph
                t = " ".join(c.strip() for c in code if c.strip())
                if t:
                    blocks.append({"kind": "p", "text": t})  # kept as a paragraph so nothing silently disappears
                    notes.append("code block read as a paragraph")  # the report tells the user about it
            elif any(c.strip() for c in code):  # "keep": a code block, lines and indentation kept, never spoken
                blocks.append({"kind": "code", "text": "\n".join(c.rstrip() for c in code).strip("\n")})
                notes.append("code block kept in the text, not read aloud")
            i += 1  # step past the closing fence
            continue  # resume the main loop after the fence
        if not s:  # an empty or whitespace-only line, which ends most Markdown elements
            if quote is not None:  # the blank line falls inside a quote being collected
                # a blank line ends the quote unless the next line continues it
                if i + 1 < n and lines[i + 1].lstrip().startswith(">"):  # the quote continues after the blank line
                    quote.append("")  # record a blank so the chunks stay separate
                    i += 1  # skip the blank line
                    continue  # keep collecting the quote
            flush()  # a blank line ends any paragraph, item or quote
            i += 1  # past the blank line
            continue  # nothing else to do for a blank line
        # setext headings
        # a line of = or - under paragraph text, while not inside an item or quote
        if para and len(para) >= 1 and re.match(r"^\s{0,3}(=+|-+)\s*$", ln) and item is None and quote is None:
            level = "h1" if s.startswith("=") else "h2"  # = makes a chapter title, - a section title
            t = strip_inline(join_lines(para))  # the paragraph lines are the heading text
            para = []  # those lines became the heading text, so the pending paragraph is cleared
            blocks.append({"kind": level, "text": t})  # an h1 or h2 block holding the underlined text
            i += 1  # step past the === or --- underline so it is not parsed again
            continue  # the underline is fully handled
        if HR.match(ln):  # a thematic break such as *** or ---
            flush()  # ends the current element
            i += 1  # the break itself is not part of the book
            continue  # back to the loop
        m = ATX.match(ln)  # try an ATX heading
        if m:  # the line is an ATX heading
            flush()  # close whatever was open before the heading
            lvl = min(3, len(m.group(1)))  # number of # signs, capped at 3 because book.md only has h1 to h3
            t = strip_inline(m.group(2))  # heading text without markup
            if t:  # skip a bare "#"
                blocks.append({"kind": f"h{lvl}", "text": t})  # a heading block of level h1, h2 or h3
            i += 1  # past the heading line
            continue  # the heading line is done; resume with the following line
        if s.startswith(">"):  # a block quote line
            if quote is None:  # starting a new quote
                flush()  # close the paragraph or item before the quote
                quote = []  # begin collecting quote lines
            # the line without its ">" markers; nested quotes are flattened into one level
            quote.append(re.sub(r"^\s*(>\s?)+", "", ln))
            i += 1  # past this quote line
            continue  # wait for more quote lines
        if quote is not None:  # a non-quote, non-blank line straight after quote lines
            # The line has no ">" but still belongs to the quote (CommonMark lazy continuation).
            quote.append(ln)                                     # lazy continuation
            i += 1  # past the continuation line
            continue  # the quote is still open
        # tables
        # a line with pipes followed by a separator row: a pipe table
        if "|" in s and i + 1 < n and TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            flush()  # close the paragraph before the table
            hdr = _cells(ln)  # header row split into cleaned cells; its length sets the table width
            blocks.append({"kind": "th", "cells": hdr})  # the table header row block
            i += 2  # skip the header and the separator
            # body rows continue while lines contain pipes and are not blank
            while i < n and "|" in lines[i] and lines[i].strip():
                cells = _cells(lines[i])  # cells of this row
                # pad short rows to the header width; longer rows keep their extra cells
                cells = (cells + [""] * len(hdr))[: max(len(hdr), len(cells))]
                blocks.append({"kind": "tr", "cells": cells})  # one body row block
                i += 1  # on to the following table line
            continue  # continue after the table
        if re.fullmatch(r"!\[[^\]]*\]\([^)]*\)", s):  # a line that is only an image
            flush()  # closes the current element
            notes.append("image dropped: " + s[:60])  # images cannot be narrated, so this is recorded in the notes
            i += 1  # step past the image line without producing a block
            continue  # the image line is fully handled
        m = BUL.match(ln)  # is the line a bullet item (-, *, + or a bullet character)?
        mn = NUM.match(ln)  # is it a numbered item such as "3." or "3)"?
        # A bullet always starts an item; a number does so unless it would interrupt a paragraph with something other
        # than "1".
        if m or (mn and not (para and mn.group(2) != "1")):     # only "1." may interrupt a paragraph
            flush()  # finish the previous element
            if m:  # the line is a bullet item: start one with the text after the marker
                item = {"kind": "bullet", "_lines": [m.group(3)]}  # start a bullet item with its first line
            else:  # the line is a numbered item: start one that keeps its number
                # the number is kept in the text, as book.md expects ("* 1. First")
                item = {"kind": "num", "_lines": [f"{mn.group(2)}. {mn.group(4)}"]}
            i += 1  # past the marker line
            continue  # the item may gain continuation lines
        if item is not None:  # a plain line while an item is open
            item["_lines"].append(ln)  # continues that item, whatever its indentation
            i += 1  # step past the continuation line
            continue  # the item stays open
        # an ordinary paragraph line, kept with its trailing spaces for hard-break detection
        para.append(ln.rstrip("\n"))
        i += 1  # advance to the following line
    flush()  # finish whatever is still pending at the end of the file
    return blocks  # every Markdown element as a block dict, in reading order


# ------------------------------------------------------------------ plain text

# A chapter or part number: digits, a Roman numeral, a number word up to fifty, an ordinal up to tenth, or "last".
# It is a non-capturing group, so the patterns below keep simple group numbers.
WORDNUM = r"(?:\d{1,3}|[ivxlcdm]{1,7}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|" \
          r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty(?:[- ](?:one|two|three|four|five|six|" \
          r"seven|eight|nine))?|thirty|forty|fifty|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|" \
          r"tenth|last)"
# "Chapter 3", "Chap. IV", "Letter twelve", "Section 2: Title"; group 2 is any text after the number.
# Any trailing text is accepted, and the Roman class [ivxlcdm] also matches plain words made of those letters.
# case-insensitive, so CHAPTER and chapter are equal
CHAPTER = re.compile(rf"^(chapter|chap\.|letter|section)\s+{WORDNUM}\b\s*[.:—–-]?\s*(.*)$", re.I)
# "Part One", "Book II", "Volume 3": the same shape as CHAPTER with other leading words
PART = re.compile(rf"^(part|book|volume)\s+{WORDNUM}\b\s*[.:—–-]?\s*(.*)$", re.I)
# named front and back matter that always gets chapter level
SPECIAL = re.compile(r"^(prologue|epilogue|preface|foreword|introduction|afterword|acknowledg(e)?ments|"
                     r"(table of )?contents|appendix(\s+\w+)?|conclusion|dedication|about the author|"
                     # optional final period or colon only
                     r"author'?s note|notes|glossary|bibliography|index)\s*[.:]?$", re.I)
# short words that may stay lower-case in a Title Case heading
SMALL = set("a an the of and or but in on at to for by with from as into vs via nor so yet".split())


# Does a line look like a Title Case heading ("The Garden at Night")?
# s is the line (str); returns True when it has at most 10 words and 80 characters, starts with a capital,
# and at least 75% of its significant words are capitalised.
def _titleish(s: str) -> bool:
    # words made of letters and apostrophes (hyphenated words count as one)
    words = re.findall(r"[A-Za-z’'][A-Za-z’'-]*", s)
    if not words or len(words) > 10 or len(s) > 80:  # no words, or too long to be a heading
        return False  # an empty or overlong line cannot be a Title Case heading
    caps = [w for w in words if w[0].isupper()]  # the words that begin with a capital letter
    big = [w for w in words if w.lower() not in SMALL]  # words that are not small connecting words
    # first word capitalised and most significant words too
    return words[0][0].isupper() and len(caps) >= max(1, 0.75 * len(big))


# Rank a single line as a possible heading in plain text.
# s is the line; underline is "=" or "-" when the next line underlines it, else None.
# Returns 0 (chapter level), 1 (ALL CAPS), 2 (underlined with - or "Contents") or 3 (Title or sentence case), or None.
# parse_txt() later turns the ranks actually used into h1, h2 and h3.
def _heading_rank(s: str, underline: str | None) -> int | None:
    """0 chapter-level, 1 ALL CAPS, 2 --- underlined, 3 Title Case; None = not a heading."""
    t = s.strip()  # the line without indentation
    if not t or len(t) > 100:  # empty, or far too long for a heading
        return None  # blank, or longer than 100 characters: never a heading
    if underline == "=":  # the next line is a === underline, the setext style for a top-level title
        return 0  # a === underline always means chapter level
    # the line is just "Contents" or "Table of contents", with an optional . or :
    if re.fullmatch(r"(table of )?contents[.:]?", t, re.I):
        return 2  # section level, so it never becomes a chapter
    # "Chapter 3", "Part One" or "Prologue" style lines; checked before the punctuation test, so "Part one of it." also
    # qualifies
    if CHAPTER.match(t) or PART.match(t) or SPECIAL.match(t):
        return 0  # chapter rank for Chapter, Part and named front or back matter
    if underline == "-":  # underlined with dashes, the weaker setext style
        return 2  # section rank for a dash-underlined line
    # ends like a sentence or clause, and is not a chapter/part line
    if re.search(r"[.,;:]$", t) and not re.search(r"\b(chapter|part)\b", t, re.I):
        return None  # a sentence, so it is body text
    letters = re.sub(r"[^A-Za-z]", "", t)  # only the ASCII letters, to test whether the line is all capitals
    # at least three letters, all capitals, at most ten words
    if len(letters) >= 3 and letters.isupper() and len(t.split()) <= 10:
        return 1  # an ALL CAPS heading
    if t.endswith(("!", "?", "”", '"')):  # ends with ! ? or a closing quote: dialogue, not a heading
        return None  # a line ending in ! ? or a closing quote is treated as speech
    if _titleish(t) and len(t.split()) <= 8:  # a Title Case line of up to eight words
        return 3  # a Title Case line gets the weakest heading rank
    # any capitalised short line without closing punctuation also gets rank 3
    if t[:1].isupper() and len(t.split()) <= 7 and len(t) <= 60 and not re.search(r"[.,;:!?”\"')\]]$", t):
        return 3                          # a short sentence-case line on its own: "Reading the light"
    return None  # none of the rules matched


# Remove the Project Gutenberg licence header and footer from an e-text.
# text is the whole file; notes collects a remark when something was removed. Returns the text between the markers.
def strip_gutenberg(text: str, notes: list) -> str:
    # the "*** START OF THE PROJECT GUTENBERG ..." line, in multiline mode
    m1 = re.search(r"^\*\*\*\s*START OF (THE|THIS) PROJECT GUTENBERG.*?\*\*\*\s*$", text, re.M | re.I)
    # the "*** END OF ..." line, if present
    m2 = re.search(r"^\*\*\*\s*END OF (THE|THIS) PROJECT GUTENBERG.*$", text, re.M | re.I)
    if m1:  # only when the start marker exists
        # keep the text after the start marker and before the end marker (or to the end of the file)
        text = text[m1.end(): m2.start() if m2 else len(text)]
        notes.append("Project Gutenberg header/footer removed")  # the report tells the user the licence text was cut
    return text  # unchanged when the file is not a Gutenberg text


# plain-text bullet: indent, marker, text that starts with a non-space
TXT_BUL = re.compile(r"^(\s*)([-*•+])\s+(\S.*)$")
TXT_NUM = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(\S.*)$")  # plain-text number "1." or "1)" with up to three digits


# Normalise a line for comparison with the project title, subtitle and author.
# Returns lower-case words without punctuation, separated by single spaces.
def _norm_line(s: str) -> str:
    # straight apostrophe, punctuation removed, whitespace collapsed
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", s.lower().replace("’", "'"))).strip()


# Drop leading lines that only repeat the project title, subtitle or author ("THE HARBOUR LIGHTS", "by A. Writer").
# text is the plain-text file; names are the project title, subtitle and author; notes collects a remark.
# Returns the text without those lines (at most six), because the cover block already shows them.
def drop_front_lines(text: str, names: list, notes: list) -> str:
    """Leading lines that just repeat the project title/subtitle/author ("THE HARBOUR LIGHTS") are dropped:
    the cover already shows them."""
    keys = {_norm_line(n) for n in names if n and n.strip()}  # comparison keys for every non-empty name
    keys |= {"by " + k for k in list(keys)}  # also accept "by <name>" lines
    if not keys:  # nothing to compare against
        return text  # leave the text alone
    lines = text.split("\n")  # the file as lines, so the leading ones can be examined one at a time
    k = 0  # index of the line being examined
    dropped = []  # lines dropped so far
    while k < len(lines) and len(dropped) < 6:  # stop after six drops, so the body of the book is never eaten
        t = lines[k].strip()  # the line without surrounding spaces
        if not t:  # blank lines before the title lines
            k += 1  # a blank line is skipped and does not count toward the limit of six
            continue  # look at the next line
        if _norm_line(t) in keys:  # the line repeats a name
            dropped.append(t)  # remember it for the note
            k += 1  # step past the dropped name line
            continue  # continue with the next candidate line
        break  # the first line that is not a name ends the front lines
    if dropped:  # at least one leading line repeated a name
        notes.append("title lines dropped: " + " / ".join(dropped))  # recorded for the report
        return "\n".join(lines[k:])  # the text from the first non-name line on, with the blank lines before it removed
    return text  # unchanged when nothing matched


# Read a paragraph as a pipe table when it has 2+ lines with the same number of "|" separators.
# p is the paragraph as a list of lines; returns a list of rows (lists of cell strings) or None.
def _pipe_table(p: list) -> list | None:
    """2+ lines with the same number of "|" separators -> rows of cells (markdown separator lines skipped)."""
    # drop Markdown separator rows such as |---|---|
    rows = [ln for ln in p if not re.fullmatch(r"\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*", ln)]
    if len(rows) < 2:  # a single line is not a table
        return None  # fewer than two content rows cannot form a table
    # remove the outer pipes only when the line has both of them
    cut = [ln.strip().strip("|") if ln.strip().startswith("|") and ln.strip().endswith("|") else ln.strip()
           for ln in rows]  # otherwise keep the trimmed line as it is
    n = {c.count("|") for c in cut}  # the set of separator counts across rows
    if len(n) != 1 or n.pop() < 1:  # rows disagree, or no separator at all
        return None  # no consistent column count
    return [[c.strip() for c in ln.split("|")] for ln in cut]  # split every row into trimmed cells


# Split one blank-line paragraph at its list items. p is the list of lines; wrapped says the text is hard-wrapped;
# width is the typical full line length in characters. Returns a list of ("para", lines) and ("bullet"|"num", text)
# tuples.
def _segments(p: list, wrapped: bool, width: int) -> list:
    """Split a blank-line paragraph at list items: [("para", lines) | ("bullet"/"num", text)]. An item
    continues on lines indented deeper than its marker (or, in hard-wrapped text, on a lowercase line after
    a full line); any other line ends the list and starts a new paragraph."""
    # finished pieces, plain lines being collected, and the open item as (kind, lines, indent)
    out, para, item = [], [], None

    # Finish the open list item, joining its lines into one text.
    # It appends (kind, text) to out and clears item; nothing happens when no item is open.
    def flush_item():
        nonlocal item  # item is reassigned, so it must refer to the enclosing variable
        if item:  # an item is open
            out.append((item[0], join_lines(item[1])))  # stored as (kind, joined text)
        item = None  # no item open any more

    # Finish the run of plain lines as one paragraph piece.
    # The raw lines are kept unjoined, because para_block() still needs them to detect verse, quotes and headings.
    def flush_para():
        nonlocal para  # para is reassigned in this helper
        if para:  # some plain lines are waiting
            out.append(("para", para))  # stored with its raw lines for para_block()
        para = []  # start a new run
    prev = None  # the previous line, used to spot wrapped years and lazy continuations
    for ln in p:  # each line of the paragraph
        m, mn = TXT_BUL.match(ln), TXT_NUM.match(ln)  # bullet or number match for this line
        # a "1990." number at the start of a line is only a list number at the paragraph start, inside a list, or after
        # a finished sentence
        if mn and not (prev is None or item is not None or re.search(r"[.:;!?]$", prev.strip())):
            mn = None                       # "...in\n1990. Then": a wrapped year, not a list number
        if m or mn:  # the line starts a bullet or a numbered item
            flush_para()  # ends the plain run
            flush_item()  # and any open item
            ind = len((m or mn).group(1))  # indent of the marker, used to recognise continuation lines
            # a new open item; numbers keep their "N." in the text
            item = ("bullet", [m.group(3)], ind) if m else ("num", [f"{mn.group(2)}. {mn.group(3)}"], ind)
        elif item is not None:  # a line while an item is open
            ind = len(ln) - len(ln.lstrip())  # leading spaces of this line, compared with the item marker's indent
            # hard-wrapped text: a lower-case line after a nearly full line continues the item
            lazy = wrapped and prev is not None and len(prev.strip()) >= 0.75 * width and ln.lstrip()[:1].islower()
            if ind > item[2] or lazy:  # deeper indent than the marker, or a lazy wrap
                item[1].append(ln)  # becomes part of the item
            else:  # neither indented deeper nor a lazy wrap
                flush_item()  # close the open item before this line
                para = [ln]  # and starts a new plain run
        else:  # no item is open and the line is not a list marker
            para.append(ln)  # add the line to the current run of plain text
        prev = ln  # remember it for the next line's checks
    flush_item()  # finish the last item
    flush_para()  # finish the last plain run
    return out  # the pieces in order


# Parse a plain-text book into blocks, finding headings, lists, tables, quotes and verse heuristically.
# text is the whole file; notes collects remarks; names are title, subtitle and author for drop_front_lines().
# Returns the block list; heading candidates are ranked first and turned into h1 to h3 at the end.
def parse_txt(text: str, notes: list, names: list | None = None) -> list:
    # unify line endings, drop a byte order mark, expand tabs to four spaces
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").replace("\t", "    ")
    text = strip_gutenberg(text, notes)  # cut a Project Gutenberg licence header and footer, if present
    text = drop_front_lines(text, names or [], notes)  # drop leading title lines
    paras, cur = [], []  # paragraphs as lists of lines, and the paragraph being collected
    for ln in text.split("\n"):  # every line of the file
        if ln.strip():  # the line has visible text, so it belongs to the current paragraph
            cur.append(ln.rstrip())  # collected without trailing spaces
        elif cur:  # a blank line after text
            paras.append(cur)  # a blank line closes the paragraph being collected
            cur = []  # start the next one
    if cur:  # the file did not end with a blank line
        paras.append(cur)  # keep the last paragraph
    # lengths of every line except the last of each multi-line, non-table paragraph
    lens = [len(ln.strip()) for p in paras if len(p) >= 2 and not _pipe_table(p) for ln in p[:-1]]
    # hard-wrapped text has short full lines (median under 100 characters); one-paragraph-per-line text does not
    wrapped = bool(lens) and statistics.median(lens) < 100
    # typical full line length: the 90th percentile, or a huge number when not wrapped
    width = sorted(lens)[int(0.9 * (len(lens) - 1))] if wrapped else 10 ** 6
    indents = [len(ln) - len(ln.lstrip()) for p in paras for ln in p]  # indentation of every line
    # the usual indentation, so only extra indent marks a quote
    base_indent = statistics.median(indents) if indents else 0

    cand = []           # (index in blocks, rank)
    blocks = []  # finished blocks, appended in reading order

    # Turn one paragraph (a list of lines) into one or more blocks, registering heading candidates in cand.
    # p is the list of raw lines; the blocks are appended to the enclosing blocks list and nothing is returned.
    # Heading candidates are stored as placeholder paragraphs whose kind is set once all ranks are known.
    def para_block(p):
        # underlined heading: "Title\n=====" or "Title\n-----"
        # a two-line paragraph whose second line is ===, --- (3 or more)
        if len(p) == 2 and re.fullmatch(r"\s*(=+|-+)\s*", p[1]) and len(p[1].strip()) >= 3:
            r = _heading_rank(p[0], p[1].strip()[0])  # rank it with the underline character in mind
            # record it as a candidate; an unranked underline counts as section level
            cand.append((len(blocks), r if r is not None else 2))
            # stored as a paragraph for now; the kind is set after ranking
            blocks.append({"kind": "p", "text": p[0].strip()})
            return  # the underlined heading is registered
        if len(p) == 1:  # a one-line paragraph, the usual shape of a plain-text heading
            r = _heading_rank(p[0], None)  # rank it as a possible heading
            if r is not None:  # the heuristics gave the line a heading rank
                cand.append((len(blocks), r))  # remember the candidate and its rank
                blocks.append({"kind": "p", "text": p[0].strip()})  # placeholder block, kind assigned later
                return  # the one-line candidate is registered
        m0 = CHAPTER.match(p[0].strip()) or PART.match(p[0].strip())  # the first line is "Chapter N" or "Part N"
        # Two lines: a chapter or part line with no title of its own, then a short Title Case line that is not
        # itself a chapter line. Together they make one heading.
        if len(p) == 2 and m0 and not m0.group(2).strip() and len(p[1]) < 70 and _titleish(p[1].strip()) \
                and not (CHAPTER.match(p[1].strip()) or PART.match(p[1].strip())):  # line 2 is not a chapter line
            cand.append((len(blocks), 0))                      # "CHAPTER I" + title on the next line
            # merged as "CHAPTER I. Title", avoiding a doubled period
            blocks.append({"kind": "p", "text": f"{p[0].strip()}. {p[1].strip()}".replace("..", ".")})
            return  # the chapter heading pair is registered
        tab = _pipe_table(p)  # maybe a pipe table
        if tab:  # the paragraph is a table
            width_ = max(len(r) for r in tab)  # the cell count of the widest row, the width every row is padded to
            for k, row in enumerate(tab):  # each row, the first being the header
                row = (row + [""] * width_)[:width_]  # pad short rows with empty cells to the full width
                blocks.append({"kind": "th" if k == 0 else "tr", "cells": row})  # header row first, then body rows
            return  # the table rows are emitted
        if all(ln.lstrip().startswith(">") for ln in p):  # every line starts with ">": an email-style quote
            # one quote block with the markers removed
            blocks.append({"kind": "quote", "text": join_lines([re.sub(r"^\s*>\s?", "", ln) for ln in p])})
            return  # the email-style quote is emitted
        ind = min(len(ln) - len(ln.lstrip()) for ln in p)  # smallest indentation in the paragraph
        # several lines all clearly shorter than a full line: a poem or an address
        short = len(p) >= 2 and max(len(ln.strip()) for ln in p) < 0.75 * width
        # short lines, or any multi-line paragraph in text that is not hard-wrapped
        if short or (not wrapped and len(p) >= 2):
            for ln in p:  # every line of the poem or address keeps its own line
                blocks.append({"kind": "verse", "text": ln.strip()})  # becomes a verse line
            return  # the verse lines are emitted
        if ind >= base_indent + 2 and ind >= 2:  # indented at least two spaces more than usual: a block quote
            # the indented paragraph becomes one quote block with its lines joined
            blocks.append({"kind": "quote", "text": join_lines(p)})
            return  # the indented quote is emitted
        blocks.append({"kind": "p", "text": join_lines(p)})  # an ordinary paragraph with its wrapped lines joined

    for p in paras:  # every paragraph of the file
        # contains list markers and is not a table
        if any(TXT_BUL.match(ln) or TXT_NUM.match(ln) for ln in p) and not _pipe_table(p):
            for kind, val in _segments(p, wrapped, width):  # split into plain runs and list items
                if kind == "para":  # a run of plain lines
                    para_block(val)  # handled like any other paragraph
                else:  # a bullet or numbered item produced by _segments()
                    blocks.append({"kind": kind, "text": val})  # becomes a bullet or num block
        else:  # the paragraph has no list markers, or it is a pipe table
            para_block(p)  # handled as a whole
    # heading levels by the ranks used in the body (front matter before the first "Chapter"/"Part"
    # heading, e.g. an ALL-CAPS title page, does not push the chapters down a level)
    first = next((k for k, r in cand if r == 0 and (CHAPTER.match(blocks[k]["text"]) or
                                                     # the first chapter-level candidate that really says Chapter or
                                                     # Part
                                                     PART.match(blocks[k]["text"]))), None)
    # ranks used from that point on, or all ranks when there is no such heading
    body = sorted({r for k, r in cand if first is None or k >= first}) or sorted({r for _, r in cand})
    # the used ranks in order become h1, h2, h3 (anything deeper is h3)
    level = {r: min(3, i + 1) for i, r in enumerate(body)}
    for k, r in cand:  # each heading candidate: its block index and its rank
        # front-matter ranks not used in the body get a level by how many body ranks are above them
        lv = level.get(r) or (1 if r == 0 else min(3, 1 + sum(1 for x in body if x < r)))
        blocks[k]["kind"] = f"h{max(1, lv)}"  # the placeholder becomes a heading of that level
    # "CHAPTER I" heading followed directly by a one-line heading: one title
    out = []
    for b in blocks:  # each block in order
        # The previous block is a heading such as "CHAPTER I" with no title of its own, and this block is a different
        # heading:
        # they are merged into a single chapter title.
        if out and out[-1]["kind"].startswith("h") and b["kind"].startswith("h") \
                and CHAPTER.match(out[-1]["text"]) and not CHAPTER.match(b["text"]) \
                and not CHAPTER.match(out[-1]["text"]).group(2):  # the chapter line has no title after it
            # "CHAPTER I. The Title" replaces the previous heading
            out[-1] = dict(out[-1], text=out[-1]["text"].rstrip(".:") + ". " + b["text"])
            continue  # this heading is consumed
        out.append(b)  # keep every other block
    for b in out:  # final cleanup of text blocks
        if "text" in b:  # only blocks with text
            # strip Markdown-style _emphasis_ or *emphasis* only when the text seems to contain some
            b["text"] = strip_inline(b["text"]) if re.search(r"(^|\W)_\S|\*\S", b["text"]) else b["text"]
    return out  # the plain-text blocks, with heading levels assigned and chapter titles merged


# Read a text input file and parse it with the Markdown or the plain-text rules, chosen by the file extension.
# path is the input file; notes collects remarks; names (title, subtitle, author) are used for plain text only.
# Returns the block list. Called by extract.extract_blocks() for .txt, .text, .md and .markdown inputs.
def parse(path, notes: list, names: list | None = None, code_blocks: str = "keep") -> list:
    """names: project title, subtitle, author (leading .txt lines repeating them are dropped)."""
    from pathlib import Path  # imported here because only this function needs it
    # UTF-8 with replacement, so a file in another encoding gets U+FFFD characters instead of an error
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    # Markdown by extension, case-insensitive; every other file is read as plain text
    if str(path).lower().endswith((".md", ".markdown")):
        # Markdown parser; the names are not used there (structure.drop_title handles title lines)
        return parse_md(raw, notes, code_blocks)
    return parse_txt(raw, notes, names)  # every other extension is plain text
