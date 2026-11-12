# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Comment maps: the explanations for one source file, kept apart from the file and applied mechanically.

A map is a UTF-8 text file. Every entry names a line of the BASELINE file (the sealed snapshot) and says where its
comment goes:

    12^ Text of a comment line placed directly above baseline line 12.
    12^ A second line of the same block (entries for one line keep their order).
    13> Text of a comment appended to the end of baseline line 13.
    ;; Lines starting with ';;' are notes for the map's author and are ignored.

Applying a map never edits code: it only inserts comment lines (with the indentation of the line they explain)
and appends trailing comments, in the comment syntax of the line's context (# for Python, PowerShell, TOML and
requirements; // for JavaScript; /* */ for CSS; <!-- --> for HTML and XML). Long above-comments are wrapped to
the width; a trailing comment that would make its line too long moves above the line when that is allowed.

Several map files may exist for one source (NAME.cmt, NAME.2.cmt, ...); their entries are merged in file order.
"""
# Keep annotations as unevaluated strings so hints such as `str | None` also work on older Python 3 releases.
from __future__ import annotations

import re  # regular expressions for the entry syntax and forbidden template text
import textwrap  # wraps long comments onto several lines within the width limit
from dataclasses import dataclass  # generates the constructor for the small Entry record below
from pathlib import Path  # map files are located and read as Path objects

from .langs import Analysis  # per-line placement facts produced by the language analysers in langs.py

# One map entry: a baseline line number, '^' (comment above) or '>' (comment at the end), one optional space, then the
# text. Leading and trailing whitespace of the map line is ignored.
ENTRY = re.compile(r"^\s*(\d+)\s*([\^>])\s?(.*?)\s*$")
WIDTH = 120  # maximum line length in characters before comments wrap or move
# Comment opener and closer for each comment context: '#' for Python, PowerShell, TOML and requirements files, '//' for
# JavaScript, and paired markers for CSS, HTML and XML.
PREFIX = {"py": ("# ", ""), "ps": ("# ", ""), "toml": ("# ", ""), "req": ("# ", ""), "js": ("// ", ""),
          # paired markers for stylesheet and markup comments
          "css": ("/* ", " */"), "html": ("<!-- ", " -->"), "xml": ("<!-- ", " -->")}
# Text that would read as template syntax: double braces or an upper-case __NAME__ marker. It is refused in comments of
# template files, because those files are filled in by plain text replacement for every book.
TEMPLATE_MARKERS = re.compile(r"\{\{|\}\}|__[A-Z][A-Z_]*__")


# One parsed map entry. line is the baseline line number (1-based), kind is '^' or '>', text is the comment without any
# comment markers, and src names the map file and line for error messages. read_entries creates these and apply consumes
# them.
@dataclass
class Entry:  # plain record; the decorator adds __init__, equality and a readable repr
    line: int  # baseline line number the comment belongs to, counted from one
    kind: str          # "^" above, ">" trailing
    text: str  # the explanation itself, without any comment markers
    src: str           # map file and line, for messages


# Find every map file for one source file. maps_dir is the run's maps folder and rel the source's repository-relative
# path, for example 'emblem_rsi/code/langs.py'. Returns the existing files in merge order: NAME.cmt first, then
# NAME.2.cmt, NAME.3.cmt and so on until a number is missing. Only checks whether files exist.
def map_files(maps_dir: Path, rel: str) -> list[Path]:
    base = maps_dir / (rel + ".cmt")  # the first map file is the source's relative path plus '.cmt'
    out = [base] if base.exists() else []  # start the list with it only when that file actually exists
    k = 2  # numbered continuation files start at two
    while True:  # keep probing numbered files until one is absent
        p = maps_dir / f"{rel}.{k}.cmt"  # candidate continuation file such as langs.py.2.cmt
        if not p.exists():  # a gap ends the sequence; higher numbers are ignored
            break  # stop probing once the numbered sequence ends
        out.append(p)  # collect the existing continuation file in order
        k += 1  # try the next number in the sequence
    return out  # all map files for the source, in merge order


# Parse map files into entries. files is the list from map_files. Returns (entries in file order, error messages); blank
# lines and ';;' notes are skipped, and any other line that does not match the entry syntax becomes an error rather than
# being dropped silently. Reads the files as UTF-8.
def read_entries(files: list[Path]) -> tuple[list[Entry], list[str]]:
    entries, errors = [], []  # parsed entries and syntax error messages, both initially empty
    for f in files:  # map files are merged in the order given
        # number each map line from one for error messages
        for i, raw in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip() or raw.lstrip().startswith(";;"):  # blank lines and ';;' author notes carry no entry
                continue  # skip them without raising any error
            m = ENTRY.match(raw)  # match against the 'N^ text' or 'N> text' syntax
            if not m:  # the line is neither blank, a note, nor an entry
                # report file, line and the first 80 characters of the offending text
                errors.append(f"{f.name}:{i}: not an entry (expected 'LINE^ text' or 'LINE> text'): {raw[:80]!r}")
                continue  # keep going so every bad line is reported at once
            # line number, placement kind, comment text and source location
            entries.append(Entry(int(m.group(1)), m.group(2), m.group(3), f"{f.name}:{i}"))
    return entries, errors  # both lists go back to measure.build, which reports the errors


# Refuse comment text that would be unsafe or forbidden in its context. text is the comment, ctx the comment syntax it
# will be written in ('py', 'css', 'html', ...), template whether the target file is a template. Returns an error
# message, or None when the text is acceptable. Pure check with no side effects.
def check_text(text: str, ctx: str, template: bool) -> str | None:
    if not text.strip():  # whitespace-only text would produce an empty comment marker
        return "empty comment"  # error message for a comment without any words
    if any(ord(c) < 32 for c in text):  # any ASCII control character, newlines and tabs included
        return "control character in comment"  # a newline would end the comment and spill text into the code
    if ctx == "css" and "*/" in text:  # CSS comment text must not contain the star-slash closer
        return "'*/' would end the CSS comment"  # it would close the CSS comment early and leave broken code behind
    # XML forbids '--' inside a comment and HTML parsers treat it oddly; a final '-' is refused as well, as a precaution
    # next to the closing marker.
    if ctx in ("html", "xml") and ("--" in text or text.endswith("-")):
        # message naming the double-hyphen rule for markup comments
        return "'--' is not allowed inside an HTML/XML comment"
    # template files must not gain anything that looks like a placeholder
    if template and TEMPLATE_MARKERS.search(text):
        # the filler would try to substitute it when building a book's page
        return "template markers ({{ }}, __NAME__) are not allowed in a template's comments"
    # a closing script or style tag inside a template comment
    if template and re.search(r"</(script|style)", text, re.I):
        # browsers would end the element right there, breaking the page's code
        return "'</script' or '</style' would end the element"
    return None  # the text is safe in this comment context


# Wrap comment text in the syntax of a context. ctx is a key of PREFIX ('py', 'css', ...) and text the comment. Returns
# the complete comment, for example '# text' or '/* text */'.
def render(ctx: str, text: str) -> str:
    a, b = PREFIX[ctx]  # opening and closing markers for this comment syntax
    return a + text + b  # marker, text and closer joined into one comment string


# Insert the comments of a map into a baseline text. base_text is the baseline file, an its analysis (placement rules
# per line), entries the parsed map, template whether template-specific text must be refused, and width the maximum line
# length. Returns (new text, origin, errors); origin gives each output line's baseline line number, or 0 for an inserted
# comment line. Pure function: measure.build calls it in memory and measure.apply writes the result.
def apply(base_text: str, an: Analysis, entries: list[Entry], template: bool = False,
          width: int = WIDTH) -> tuple[str, list[int], list[str]]:  # maximum line width in characters, normally 120
    """Insert the comments. Returns (new text, baseline line number for every new line (0 = inserted), errors)."""
    errors = []  # problems found with individual entries, reported to the author
    lines = an.lines  # baseline Line records carrying the placement flags
    n = len(lines)  # number of baseline lines, used for range checking
    above: dict[int, list[str]] = {}  # baseline line number to the comment texts going above it
    trail: dict[int, list[str]] = {}  # baseline line number to the texts appended at its end
    for e in entries:  # validate each entry and sort it into above or trail
        if not 1 <= e.line <= n:  # line numbers must refer to lines of the baseline file
            # the entry points past the end of the baseline
            errors.append(f"{e.src}: line {e.line} is outside the file (1..{n})")
            continue  # skip the entry; the rest of the map still applies
        ln = lines[e.line - 1]  # the baseline line the entry belongs to
        # above-comments use the line's start syntax, end comments its end syntax
        ctx = ln.ctx_start if e.kind == "^" else ln.ctx_end
        bad = check_text(e.text, ctx, template)  # check the text against the rules for that comment syntax
        if bad:  # the text was refused for this comment context
            errors.append(f"{e.src}: line {e.line}: {bad}")  # report which entry was refused and the reason
            continue  # drop the refused entry and continue with the others
        if e.kind == "^":  # a comment line to be placed above the baseline line
            if not ln.above:  # the analyser forbids inserting a comment line here
                # state the reason (inside a string, a tag or a continuation) and suggest the end form
                errors.append(f"{e.src}: line {e.line}: a comment cannot go above this line ({ln.why_exempt or 'it continues the line before'}); use '>'")
                continue  # skip the misplaced entry and keep checking others
            above.setdefault(e.line, []).append(e.text)  # queue the text above this line, preserving map order
        else:  # otherwise the entry is an end-of-line comment
            if not ln.trail:  # this line cannot take an appended comment
                # explain the refusal and point to the above form instead
                errors.append(f"{e.src}: line {e.line}: a comment cannot be appended to this line"
                              # mention an existing end comment when that is the reason
                              + (" (it already has one)" if ln.trailing else "") + "; use '^'")
                continue  # skip the entry that cannot be appended here
            trail.setdefault(e.line, []).append(e.text)  # queue the text for the end of this line
    out, origin = [], []  # output lines, and for each its baseline line number or zero
    for ln in lines:  # rebuild the file line by line from the baseline
        ctx_a, ctx_t = ln.ctx_start, ln.ctx_end  # comment syntax for above and for end comments on this line
        texts = list(above.get(ln.n, []))  # copy of the queued above-comments for this line
        tline = None  # the line with its end comment, once one is built
        if ln.n in trail:  # this line has end-comment entries waiting
            t = " ".join(trail[ln.n])  # several end entries for one line join into one comment
            # trailing spaces trimmed, two spaces, then the rendered comment
            cand = ln.text.rstrip() + "  " + render(ctx_t, t)
            # Too long for the width: move the text above the line instead, provided a comment may go there and both
            # positions use the same syntax (on a <style> or <script> opening line they do not).
            if len(cand) > width and ln.above and ctx_t == ctx_a:
                texts.append(t)  # the end comment joins the above-comments list instead
            else:  # otherwise accept the line even when it is long
                tline = cand  # use the line with its end comment appended
        if texts:  # there are comment lines to insert above this line
            ind = ln.indent if ln.text.strip() else ""  # indent like the code line; a blank line gets no indentation
            a, b = PREFIX[ctx_a]  # opener and closer of the line's start syntax
            room = max(40, width - len(ind) - len(a) - len(b))  # room for text per line, never less than 40 characters
            for t in texts:  # each queued above-comment text in map order
                # Wrap on spaces only, never inside a word or at a hyphen, so identifiers and paths stay intact; an
                # empty wrap result falls back to the text itself.
                for piece in textwrap.wrap(t, room, break_long_words=False, break_on_hyphens=False) or [t]:
                    out.append(ind + a + piece + b)  # one indented comment line per wrapped piece
                    origin.append(0)  # inserted lines have no baseline line, recorded as zero
        out.append(tline if tline is not None else ln.text)  # the baseline line itself, possibly with an end comment
        origin.append(ln.n)  # remember which baseline line this output line came from
    return "\n".join(out), origin, errors  # the new text joined with LF line endings, plus origins and errors
