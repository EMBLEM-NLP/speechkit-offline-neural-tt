# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Per-language line analysis for the code-comment audit.

For every line of a source file the analysis says:
  required   the line holds code that must be explained by a comment
  above      a comment line may be inserted directly above it without changing the code
  trail      a comment may be appended to its end without changing the code
  comment    the line holds only a comment
  trailing   the line already ends with a comment after its code
  ctx_start  comment syntax at the start of the line (py, ps, js, css, html, xml, toml, req)
  ctx_end    comment syntax at the end of the line

and for the whole file an identity: a hash of the code with every comment removed, computed from the language's
own tokenizer or syntax tree (Python ast, PowerShell's parser, acorn for JavaScript, ElementTree for XML, tomllib
for TOML). Two versions of a file with the same identity contain the same code; only comments differ.

A line is exempt (not required) when there is nothing to explain or nothing can be attached to it: blank lines,
comment lines, lines inside a multi-line string or tag, lines that only close brackets, and lines that hold only a
string literal (docstrings, wrapped message text).
"""
# Store type hints as unevaluated strings, so the modern `X | None` and `list[Line]` spellings work on every Python 3
# version this toolkit supports.
from __future__ import annotations

import ast  # Python's own parser; ast.dump of a module is the identity of a .py file
import hashlib  # SHA-256 turns comment-free code into a short fingerprint
import html.parser  # standard HTML tokenizer used to walk template markup tag by tag
import io  # StringIO lets tokenize read a str as if it were a file
import json  # carries requests and replies to the node and pwsh helper processes
import re  # regexes for placeholders, style/script regions and comment stripping
import shutil  # shutil.which locates the node and pwsh executables on the PATH
import subprocess  # runs the external tokenizers as child processes
import tempfile  # scratch folders for request files and PowerShell source copies
import tokenize  # Python's lexer: separates comments, strings and code with exact positions
import xml.etree.ElementTree as ET  # canonicalize() gives XML a comment-free, whitespace-insensitive form
# dataclass writes Line/Analysis constructors; field gives each Analysis its own dict
from dataclasses import dataclass, field
from pathlib import Path  # object-style paths that behave the same on Windows and Linux

# Folder holding this module, so the helper scripts are found whatever the current working directory is.
HERE = Path(__file__).resolve().parent
# Node helper that tokenizes JavaScript with the vendored acorn parser (see js_tokens.mjs next to this file).
JS_TOKENS = HERE / "js_tokens.mjs"
# PowerShell helper that runs PowerShell's own parser over .ps1 files (see ps_tokens.ps1 next to this file).
PS_TOKENS = HERE / "ps_tokens.ps1"

# File extension to analyser key. .ps1xml (PowerShell format/type data) is plain XML and .mjs is an ES module; any
# extension missing here is not treated as code, so measure.discover skips such files.
LANG_BY_SUFFIX = {".py": "py", ".ps1": "ps", ".psm1": "ps", ".psd1": "ps", ".mjs": "js", ".js": "js", ".css": "css",
                  ".html": "html", ".htm": "html", ".ps1xml": "xml", ".xml": "xml", ".toml": "toml"}


# Decide which analyser handles a file. path is a str or Path; only its name is looked at and the file is never opened.
# Returns a short language key ('py', 'ps', 'js', 'css', 'html', 'xml', 'toml' or 'req') or None when the file is not
# audited. measure.discover uses it to pick files and analyze_many uses it to route each file to its analyser.
def lang_of(path: str | Path) -> str | None:
    p = Path(path)  # wrap in Path so str and Path input are handled alike
    # pip requirement files (requirements.txt, requirements-voice.txt) share the generic .txt suffix, so they are
    # recognised by their name before the extension lookup.
    if p.name.startswith("requirements") and p.suffix == ".txt":
        return "req"  # selects the pip requirements analyser
    # Everything else is decided by the extension alone, lower-cased so that FILE.PS1 still counts as PowerShell; an
    # unknown extension gives None.
    return LANG_BY_SUFFIX.get(p.suffix.lower())


# One source line and everything the audit knows about it. n is the 1-based line number and text the line without its
# newline. ctx_start and ctx_end name the comment syntax ('py', 'js', 'css', 'html', ...) for a comment placed above the
# line or appended to its end. The boolean flags are set by a language analyser and then by finish(); `code show` prints
# them and cmap.apply relies on them to place comments safely.
@dataclass
class Line:  # the decorator builds __init__ from the annotated fields, in this order
    n: int  # 1-based position of the line in the analysed text
    text: str  # the characters of the line, newline excluded
    ctx_start: str  # syntax for a comment inserted above (differs from ctx_end on a <style>/<script> opening line)
    ctx_end: str  # syntax for a comment appended after the line's code
    code: bool = False  # True when at least one code token sits on the line
    comment: bool = False  # True when the line holds nothing but a comment
    trailing: bool = False  # True when code here is already followed by its own comment
    required: bool = False  # set by finish(): the line must be explained before the file is complete
    above: bool = True  # may a whole comment line go directly above without changing the code?
    trail: bool = True  # may a comment be appended after the code without changing it?
    why_exempt: str = ""  # reason the line needs no comment, shown in brackets by `code show`

    # Computed attribute read as ln.indent; nothing is stored, so it can never disagree with text.
    @property
    # Leading whitespace of the line, returned as a str of spaces and tabs. cmap.apply copies it in front of an inserted
    # comment so the comment lines up with the code it explains. No side effects.
    def indent(self) -> str:
        # full length minus left-stripped length is the run of leading whitespace
        return self.text[: len(self.text) - len(self.text.lstrip())]

    # A second derived attribute, recomputed from text each time it is read.
    @property
    # True for an empty or whitespace-only line. Blank lines never need a comment, and coverage() looks past them when
    # it searches for the comment above a line. Returns a bool.
    def blank(self) -> bool:
        return not self.text.strip()  # strip() leaves an empty string when only spaces or tabs are present


# The result of analysing one file: its path, language key, one Line per physical line, whether the language's parser
# accepted it (ok, error) and its code identity. identity is a SHA-256 hex digest of the code with every comment
# removed, or None when it could not be computed. extra holds analyser-specific details such as the regions of an HTML
# page. measure.py compares baseline and current identities to prove that only comments changed.
@dataclass
class Analysis:  # what every analyse_* function and analyze() return
    path: str  # the file's path as given by the caller (repository-relative in the audit)
    lang: str  # language key from lang_of, for example 'py' or 'html'
    lines: list[Line]  # one record per physical line, in file order
    ok: bool = True  # False when the parser rejected the file
    error: str = ""  # the parser's message when ok is False, kept for reports
    identity: str | None = None  # fingerprint of the comment-free code; equal values mean only comments differ
    # default_factory gives every Analysis a fresh dict instead of one shared default
    extra: dict = field(default_factory=dict)

    # Count how many required lines are explained. A line counts when it carries a trailing comment or when the nearest
    # non-blank line above it is a comment line. Returns (covered, required, uncovered line numbers); measure.py turns
    # this into the coverage metric, and `code check` and `code show` use the list to point at lines still to explain.
    def coverage(self) -> tuple[int, int, list[int]]:
        """(covered, required, uncovered line numbers): a required line is covered by a comment on its own line or
        by a comment line directly above it (blank lines in between are allowed)."""
        req = cov = 0  # two running totals: required lines found and explained lines found
        miss = []  # numbers of required lines with no explanation yet
        prev = None  # nearest non-blank line seen so far; None before the first one
        for ln in self.lines:  # top to bottom, so prev is always the line directly above
            if ln.required:  # only lines flagged as needing an explanation are counted
                req += 1  # each required line adds one to the denominator
                # its own end comment, or a pure comment line as the nearest non-blank line above
                if ln.trailing or (prev is not None and prev.comment):
                    cov += 1  # covered by its own end comment or the comment line above
                else:  # neither form of explanation is present
                    miss.append(ln.n)  # remembered so `check` can name the line to the map author
            if not ln.blank:  # blank lines do not reset prev, so blank space between comment and code is tolerated
                prev = ln  # becomes the candidate "line above" for the next line
        return cov, req, miss  # totals plus the list of lines still waiting for a comment


# Hash helper shared by every analyser: SHA-256 of a str encoded as UTF-8, returned as 64 hexadecimal characters. Using
# one helper keeps the identity format the same for all languages.
def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()  # hashlib works on bytes, so the text is encoded first


# Split file text into physical lines without their newline characters. text is the whole file as a str (read with
# read_text, so Windows CRLF endings are already plain LF). Returns a list of str; an empty file yields an empty list.
def _lines(text: str) -> list[str]:
    # a final newline yields a last empty string, so `show` lists one more line than wc -l
    return text.split("\n") if text else []


# Shared last pass for every language: converts the flags an analyser set (code, comment, trailing, above, trail,
# why_exempt) into the final required flag and an exemption reason. lines is modified in place and nothing is returned.
# Every analyse_* function calls it just before returning.
def finish(lines: list[Line]) -> None:
    """Common rules once code/comment/trailing are known: what is required, and why a line is exempt."""
    for ln in lines:  # each line is judged on the flags already set for it
        if ln.blank:  # whitespace-only lines never need a comment
            ln.required, ln.why_exempt = False, "blank"  # optional, with the reason `show` prints in brackets
        elif not ln.code:  # no code token: a comment, or text that is not code (docstring body, markup text)
            # Keep any reason the analyser already gave; otherwise call it a comment line or, when there is no comment
            # either, a literal (string or text content).
            ln.required, ln.why_exempt = False, ln.why_exempt or ("comment" if ln.comment else "literal")
        elif ln.why_exempt:  # code exists but the analyser judged it trivial (closers, lone string, frozen)
            ln.required = False  # trivial code carries nothing new to explain
        else:  # any other line with code is something a reader needs explained
            ln.required = True  # the line now counts towards the coverage denominator
        if ln.trailing:  # an existing end comment means a second one cannot be appended
            ln.trail = False  # stops cmap.apply from adding a comment after the existing one
        # Last case: a required line where neither a comment above nor one at the end can be placed and none exists.
        if ln.required and not ln.above and not ln.trail and not ln.trailing:
            # a continuation line that can hold no comment at all (backslash or backtick continued on both sides,
            # or text inside a template string): the comment on the statement's first line explains it
            # exempt it, because demanding an impossible comment would make 100% unreachable
            ln.required, ln.why_exempt = False, "cannot hold a comment (continuation)"


# ------------------------------------------------------------------------------------------------ Python
# Operator tokens that only close or separate. A Python line made of nothing but these has no logic of its own.
PY_CLOSERS = {")", "]", "}", ",", ":", ";"}
STRINGY = {tokenize.STRING}  # token types that make up a string literal; grows in the loop below
# Python 3.12 splits f-strings into START/MIDDLE/END tokens and 3.14 adds t-strings the same way, so register those
# types whenever this interpreter defines them.
for _n in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END", "TSTRING_START", "TSTRING_MIDDLE", "TSTRING_END"):
    if hasattr(tokenize, _n):  # older Pythons lack these names; hasattr keeps the module importable everywhere
        # add the numeric token type so f-string pieces also count as string material
        STRINGY.add(getattr(tokenize, _n))


# Analyse a Python source. path only labels the result; text is the full source. The identity is the SHA-256 of ast.dump
# of the parsed module, which ignores comments and layout but includes docstrings and every statement. Lines are
# classified from the tokenize stream, with special handling for multi-line strings, backslash continuations, lines of
# closers and lines holding only a string. Pure computation: no files or processes.
def analyze_py(path: str, text: str) -> Analysis:
    raw = _lines(text)  # physical lines of the source, newline characters removed
    # Python comments use '#' both above a line and at its end
    lines = [Line(i + 1, t, "py", "py") for i, t in enumerate(raw)]
    a = Analysis(path, "py", lines)  # the result object; its flags are filled in below
    try:  # both the parser and the tokenizer may reject broken code
        tree = ast.parse(text)  # build the syntax tree; a SyntaxError here means the file does not parse
        # ast.dump omits positions and comments, so inserted comment lines keep this hash
        a.identity = _h(ast.dump(tree))
        # every token with (row, col) start and end, COMMENT tokens included
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    # TokenError covers unclosed brackets or strings at end of file
    except (SyntaxError, tokenize.TokenError, IndentationError) as e:
        # keep the failure as readable text for `check` and measurement reports
        a.ok, a.error = False, f"{type(e).__name__}: {e}"
        return a  # without tokens there is nothing else to classify; ok is False
    n = len(lines)  # total line count, used to guard look-ahead to the next line
    spans = []                      # (start_row, start_col, end_row, end_col) of multi-line string constructs
    fstack = []  # start positions of open f-strings/t-strings; a stack because they nest
    per_line: dict[int, list] = {}  # tokens grouped by the row they start on
    # First pass over the tokens: find string constructs that span several rows and file every token under its start
    # row.
    for t in toks:
        # symbolic type name, needed for f-string kinds that may not exist as constants
        name = tokenize.tok_name[t.type]
        if name in ("FSTRING_START", "TSTRING_START"):  # an f-string or t-string opens (3.12+ tokenizes them piecewise)
            fstack.append(t.start)  # remember where it began so its whole extent is known at the close
        elif name in ("FSTRING_END", "TSTRING_END") and fstack:  # the end of the innermost open f-string
            s = fstack.pop()  # pop the matching opening position off the stack
            if not fstack and t.end[0] > s[0]:  # record only outermost f-strings that really end on a later row
                # its rows become a span exactly like a triple-quoted string
                spans.append((s[0], s[1], t.end[0], t.end[1]))
        # an ordinary string token that crosses rows (triple quotes or backslash-newline)
        elif t.type == tokenize.STRING and t.end[0] > t.start[0]:
            spans.append((t.start[0], t.start[1], t.end[0], t.end[1]))  # its inner rows are text, not code
        per_line.setdefault(t.start[0], []).append(t)  # file every token, string or not, under the row where it begins
    inside_start, inside_end = set(), set()  # rows starting inside a string, and rows whose end lies inside one
    for sr, sc, er, ec in spans:  # convert each multi-line span into those two row sets
        for r in range(sr + 1, er + 1):  # rows after the first are entered from inside the string
            inside_start.add(r)  # a comment line inserted there would become string text
        for r in range(sr, er):  # rows before the last carry on into the string past their end
            inside_end.add(r)  # anything appended there would also land inside the string
    # Second pass: classify each physical line from the tokens that start on it.
    for ln in lines:
        # Drop structural tokens (line ends, indentation changes, the encoding marker, the end marker) that are neither
        # visible code nor comments.
        ts = [t for t in per_line.get(ln.n, []) if t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                                                                    # dedent markers and the encoding pseudo-token carry
                                                                    # no text
                                                                    tokenize.DEDENT, tokenize.ENCODING,
                                                                    # end-of-file marker; closes the filter
                                                                    tokenize.ENDMARKER)]
        code = [t for t in ts if t.type != tokenize.COMMENT]  # code tokens are everything on the row except comments
        # comment tokens on the row (at most one, since '#' runs to the line end)
        comm = [t for t in ts if t.type == tokenize.COMMENT]
        if ln.n in inside_start:  # the row starts inside a multi-line string
            ln.above = False  # no comment line may be inserted above it
            if ln.n in inside_end or not code:  # it also continues the string, or has no code after the string closes
                # so the whole row is string content, not code
                ln.code, ln.why_exempt = False, "inside a multi-line string"
        if ln.n in inside_end:  # the row ends inside a string that continues on the next row
            ln.trail = False  # nothing can be appended after its last character
        # Rows outside strings, and the closing row of a string followed by code (such as '""".split())'), take their
        # code flag from the tokens found on them.
        if ln.n not in inside_start or (ln.n not in inside_end and code):
            ln.code = bool(code) or ln.code  # keep an earlier True; otherwise true when any code token is present
        # a pure comment row, unless it is text inside a string that only looks like one
        ln.comment = bool(comm) and not code and ln.n not in inside_start
        ln.trailing = bool(comm) and bool(code)  # code followed by a '#' comment is already explained by it
        if code and ln.n not in inside_start:  # look for rows whose code is trivial and needs no explanation
            # Rows made only of string pieces and closing punctuation, with at least one real string: a docstring line
            # or a wrapped piece of message text.
            if all(t.type in STRINGY or (t.type == tokenize.OP and t.string in PY_CLOSERS) for t in code) and \
                    any(t.type in STRINGY for t in code):  # at least one string piece is required
                ln.why_exempt = "string literal only"  # such a row is data rather than logic, so it is exempt
            # nothing but closing brackets, commas, colons or semicolons on this row
            elif all(t.type == tokenize.OP and t.string in PY_CLOSERS for t in code):
                ln.why_exempt = "closing brackets only"  # it only ends a construct that began on an earlier row
        stripped = ln.text.rstrip()  # remove trailing spaces before looking for a final backslash
        # An explicit backslash continuation outside any string, on a row without its own comment. Note: a pure comment
        # row that happens to end in a backslash also matches, which needlessly blocks a comment above the following
        # row.
        if stripped.endswith("\\") and ln.n not in inside_end and not ln.trailing:
            ln.trail = False  # '# ...' after the backslash would break the continuation
            if ln.n < n:  # the last line has no following line to protect
                lines[ln.n].above = False          # the next line continues this one
    finish(lines)  # apply the shared rules that turn these flags into required or exempt
    return a  # the finished analysis for this Python file


# ------------------------------------------------------------------------------------------------ PowerShell
# PowerShell token kinds that are not code: comments, line ends, the end marker and backtick line continuations.
PS_NONCODE = {"Comment", "NewLine", "EndOfInput", "LineContinuation"}
# kinds that only close or separate; a line of these alone is exempt
PS_CLOSERS = {"RCurly", "RParen", "RBracket", "Comma", "Semi"}
# quoted strings, expandable strings and here-strings (@' '@ and @" "@)
PS_STRINGS = {"StringLiteral", "StringExpandable", "HereStringLiteral", "HereStringExpandable"}


# Run PowerShell's own parser over a batch of .ps1/.psm1/.psd1 files in one PowerShell process. paths is a list of file
# paths that must exist on disk, because Parser.ParseFile reads files. Returns {resolved path: {'errors': [...],
# 'tokens': [[kind, text, start line, start col, end line, end col, flags], ...]}} with 1-based lines and columns. Side
# effects: writes a temporary request file and starts pwsh (or Windows PowerShell) for up to 600 seconds.
def ps_tokens(paths: list[str]) -> dict:
    # prefer PowerShell 7; fall back to Windows PowerShell 5.1 on a bare Windows machine
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:  # neither PowerShell 7 nor Windows PowerShell was found on the PATH
        # fail loudly instead of silently skipping PowerShell files
        raise RuntimeError("PowerShell (pwsh) is needed to analyse .ps1/.psm1/.psd1 files")
    with tempfile.TemporaryDirectory() as td:  # scratch folder removed automatically when the block ends, even on error
        req = Path(td) / "req.json"  # the file ps_tokens.ps1 receives as its only argument
        # a JSON array of absolute paths; resolving makes the reply's keys predictable
        req.write_text(json.dumps([str(Path(p).resolve()) for p in paths]), encoding="utf-8")
        # -NoProfile skips the user's profile scripts (faster, no surprises), -NonInteractive fails instead of
        # prompting, and -File runs the tokenizer script with the request path as its argument.
        r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-File", str(PS_TOKENS), str(req)],
                           # UTF-8 text capture, bad bytes replaced; ten-minute ceiling
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    if r.returncode != 0:  # a non-zero exit code means the helper itself failed
        # keep only the last 1500 characters so the cause is visible but short
        raise RuntimeError("PowerShell tokenizer failed: " + (r.stderr or r.stdout)[-1500:])
    data = json.loads(r.stdout)  # the helper prints a single compact JSON object on stdout
    # Re-key the reply by the same resolved path strings; a path missing from the reply maps to None, which analyze_ps
    # reports as "no tokens".
    return {str(Path(p).resolve()): data.get(str(Path(p).resolve())) for p in paths}


# Analyse a PowerShell source using tokens from ps_tokens. path labels the result, text is the source, and tok is this
# file's entry from the tokenizer reply (or None). The identity hashes the sequence of non-comment tokens (kind and
# text) with each run of line breaks collapsed to one marker, so inserted comment lines leave it unchanged. It also
# enforces PowerShell placement rules: nothing may separate a line ending in '|' or a backtick from its continuation, or
# comment-based help from its function.
def analyze_ps(path: str, text: str, tok: dict) -> Analysis:
    raw = _lines(text)  # split the script into physical lines without newline characters
    # PowerShell uses '#' for comments above a line and at its end
    lines = [Line(i + 1, t, "ps", "ps") for i, t in enumerate(raw)]
    a = Analysis(path, "ps", lines)  # empty PowerShell result record; flags are filled in below
    if tok is None:  # the tokenizer reply had no entry for this file
        a.ok, a.error = False, "no tokens"  # report the file as unparsable because the reply lacks it
        return a  # nothing more can be classified without tokens
    if tok.get("errors"):  # syntax errors: tokens still exist, but the parse check fails
        # join the messages, capped at 500 characters for the report
        a.ok, a.error = False, "; ".join(tok["errors"])[:500]
    # Give each token array readable keys: k kind, t exact text, sl/sc start line and column, el/ec end line and column
    # (all 1-based, as PowerShell reports them). The seventh element, the token flags, is not needed.
    toks = [dict(k=t[0], t=t[1], sl=t[2], sc=t[3], el=t[4], ec=t[5]) for t in tok["tokens"]]
    ident, prev_nl = [], True  # identity pieces, and whether the last kept piece was a line break
    for t in toks:  # build the identity from the tokens in source order
        # comments are exactly what the audit adds, and the end marker holds no code
        if t["k"] == "Comment" or t["k"] == "EndOfInput":
            continue  # leave both out of the identity so comments never change it
        # line breaks end statements in PowerShell, but their count does not matter
        if t["k"] in ("NewLine", "LineContinuation"):
            if not prev_nl:  # only the first break of a run is recorded
                ident.append("NL")  # one marker stands for any number of consecutive breaks
            prev_nl = True  # later breaks in this run are absorbed
            continue  # nothing else needs recording for a line break token
        # kind and exact text, joined by the 0x1F unit separator so they cannot blur together
        ident.append(t["k"] + "\x1f" + t["t"])
        prev_nl = False  # real code was seen, so the next break counts again
    while ident and ident[-1] == "NL":  # trailing line breaks at the end of the file are not code
        ident.pop()  # drop them so a final added newline is harmless
    # hash the pieces joined by 0x1E; a file with syntax errors gets no identity
    a.identity = _h("\x1e".join(ident)) if a.ok else None
    n = len(lines)  # line count, for range checks
    on_line: dict[int, list] = {}  # tokens grouped by the line where they start
    for t in toks:  # first pass: index tokens and handle any token spanning several lines
        if t["k"] in ("NewLine", "EndOfInput"):  # line breaks and the end marker belong to no line's content
            continue  # move on without filing these structural tokens anywhere
        on_line.setdefault(t["sl"], []).append(t)  # file the token under its starting line
        # a block comment (angle-bracket and hash pair), a here-string, or a quoted string containing newlines
        if t["el"] > t["sl"]:
            for r in range(t["sl"] + 1, t["el"] + 1):  # every line after the first is inside the token
                # the Line record, or None if the token claims to end past the last line
                ln = lines[r - 1] if r <= n else None
                if ln is None:  # defensive guard against an out-of-range end line
                    continue  # nothing to mark for a line that does not exist
                ln.above = False  # a comment line inserted here would become part of the token
                if t["k"] == "Comment":  # inside a block comment such as comment-based help
                    ln.comment = True  # these lines are block comment text, not code
                    ln.why_exempt = "comment"  # and need no explanation of their own
                # A middle line of a multi-line string, or its last line when nothing follows the closing quote (ec is
                # 1-based and points just past the token, hence the -1 when slicing).
                elif r < t["el"] or not ln.text[t["ec"] - 1:].strip():
                    # exempt as string content, keeping any earlier reason
                    ln.why_exempt = ln.why_exempt or "inside a multi-line string"
            for r in range(t["sl"], t["el"]):  # every line but the last continues the token past its end
                if r <= n:  # skip lines beyond the end of the file
                    lines[r - 1].trail = False  # appending text there would land inside the string or comment
    for ln in lines:  # second pass: decide what each line contains
        ts = on_line.get(ln.n, [])  # tokens whose first character lies on this line
        # real code: not comments, line breaks, the end marker or backtick continuations
        code = [t for t in ts if t["k"] not in PS_NONCODE]
        comm = [t for t in ts if t["k"] == "Comment"]  # comment tokens that begin on this line
        cont = any(t["k"] == "LineContinuation" for t in ts)  # does the line end with a backtick continuation?
        if ln.why_exempt == "inside a multi-line string":  # lines of string content were settled in the first pass
            ln.code = False  # string content is never code to explain
            continue  # nothing else applies to a line of string content
        # code token present, or flagged earlier and not comment text
        ln.code = bool(code) or (ln.code and not ln.comment)
        if comm and not code:  # a comment token present and no code token at all
            ln.comment = True  # mark it as a pure comment line
        # Code plus a comment that starts to the right of the first code token: an end-of-line explanation. A <# #>
        # block that comes before the code does not count.
        ln.trailing = bool(comm) and bool(code) and min(c["sc"] for c in comm) > min(c["sc"] for c in code)
        if code:  # check whether the code on the line is trivial
            # only closing braces, parentheses, brackets, commas or semicolons
            if all(t["k"] in PS_CLOSERS for t in code):
                ln.why_exempt = "closing brackets only"  # it merely ends something opened earlier
            # Only strings and closers, at least one string, not starting in column 1, and not a here-string: this is a
            # wrapped text argument that continues the statement above.
            elif all(t["k"] in PS_STRINGS or t["k"] in PS_CLOSERS for t in code) and \
                    any(t["k"] in PS_STRINGS for t in code) and code[0]["sc"] > 1 and \
                    not any(t["k"] in ("HereStringLiteral", "HereStringExpandable") for t in code):  # @" blocks count
                # a lone string continuing an argument list has nothing new to explain
                ln.why_exempt = "string literal only"
        if cont:  # the line ends with a backtick, which must be its very last character
            ln.trail = False  # an end comment after the backtick would break the continuation
            if ln.n < n:  # guard for the last line, which has no next line
                lines[ln.n].above = False  # the next line continues this statement, so no comment line may come between
        if code and code[-1]["k"] == "Pipe" and ln.n < n:  # a trailing '|' carries the pipeline on to the next line
            lines[ln.n].above = False              # keep the next pipeline segment directly after the pipe
        # comment-based help must stay directly above `function`
        # Comment-based help (<# .SYNOPSIS ... #>) belongs to a function only when it ends directly above `function`, so
        # no comment line may be inserted between them.
        if code and code[0]["t"].lower() == "function":
            p = ln.n - 2  # 0-based index of the line before this one
            while p >= 0 and lines[p].blank:  # step over blank lines between the help block and `function`
                p -= 1  # move one line further up past the blank line
            # the nearest non-blank line closes a block comment, presumably a help block
            if p >= 0 and lines[p].comment and lines[p].text.strip().endswith("#>"):
                ln.above = False  # keep the help attached to its function
    finish(lines)  # turn the flags into required or exempt
    return a  # the finished PowerShell analysis, ready for coverage and identity checks


# ------------------------------------------------------------------------------------------------ JavaScript
# acorn token labels that only close or separate; a JavaScript line made of these alone is exempt.
JS_CLOSERS = {")", "]", "}", ",", ";"}


# Tokenize many JavaScript sources in one node process with the vendored acorn parser (js_tokens.mjs). sources is a list
# of (id, source text, is_module) tuples; is_module picks ES-module parsing for .mjs files and classic-script parsing
# otherwise. Returns {id: {'ok', 'error', 'tokens', 'comments'}} exactly as the helper replies. Side effects: starts
# node, sends the request on stdin; raises RuntimeError when node is missing or exits with an error.
def js_tokens(sources: list[tuple[str, str, bool]]) -> dict:
    """sources: [(id, text, is_module)] -> node tokenizer results by id."""
    node = shutil.which("node")  # find the node executable on the PATH
    if not node:  # node is not installed, or its folder is missing from PATH
        # JavaScript cannot be checked without node, so stop with a clear message
        raise RuntimeError("node is needed to analyse JavaScript")
    # request records in the shape js_tokens.mjs expects
    req = [{"id": i, "source": s, "module": m} for i, s, m in sources]
    # Send the JSON request on stdin (no temporary file needed) and collect the reply from stdout.
    r = subprocess.run([node, str(JS_TOKENS)], input=json.dumps(req), capture_output=True, text=True,
                       # decode as UTF-8, replace bad bytes, give up after ten minutes
                       encoding="utf-8", errors="replace", timeout=600)
    if r.returncode != 0:  # the helper crashed or could not load acorn
        # keep the last 1500 characters of the error output for the message
        raise RuntimeError("JavaScript tokenizer failed: " + (r.stderr or r.stdout)[-1500:])
    return json.loads(r.stdout)  # the reply: one result object per source id


# Mark JavaScript lines from acorn's tokens and comment ranges, and compute the code identity. lines are the Line
# records to update in place (a whole .js/.mjs file, or an HTML page's lines), res is one reply from js_tokens, and
# only, a set of line numbers, limits the marking to one <script> region of a page. Returns the identity hash built from
# every token's label and value in order; comments never enter it.
def js_lines(lines: list[Line], res: dict, only: set | None = None) -> str | None:
    """Mark JavaScript lines from tokenizer results; returns the identity of the code."""
    n = len(lines)  # line count, for range checks
    on_line: dict[int, list] = {}  # tokens grouped by the line they start on
    # each token is [label, value, start line, end line, start col, end col]; lines 1-based, columns 0-based
    for t in res["tokens"]:
        on_line.setdefault(t[2], []).append(t)  # file the token under its starting line
        if t[3] > t[2]:  # a token spanning lines: in practice a template-literal chunk with newlines
            for r in range(t[2] + 1, t[3] + 1):  # every line after the token's first
                if r <= n and (only is None or r in only):  # stay inside the file and inside the region being marked
                    lines[r - 1].above = False  # a '//' line inserted here would become text in the template literal
                    # a middle line, or the last line when nothing follows the token's end column
                    if r < t[3] or not lines[r - 1].text[t[5]:].strip():
                        # exempt as string content, unless a reason was already set
                        lines[r - 1].why_exempt = lines[r - 1].why_exempt or "inside a multi-line string"
            for r in range(t[2], t[3]):  # lines the token continues past their end
                if r <= n and (only is None or r in only):  # again only lines of this file and region
                    lines[r - 1].trail = False  # an end comment there would become part of the template text
    com_lines: dict[int, list] = {}  # line number to the comment ranges touching that line
    for sl, el, block in res["comments"]:  # each comment from acorn's onComment hook: [start line, end line, is block]
        for r in range(sl, el + 1):  # every line the comment touches, from first to last
            com_lines.setdefault(r, []).append((sl, el))  # record the range under this line
            if r < el and r <= n:  # a /* */ block that carries on past this line
                lines[r - 1].trail = False  # appending '// ...' there would be swallowed by the block comment
            if r > sl and r <= n:  # lines after the first line of a block comment
                lines[r - 1].above = False  # no comment line may be inserted inside the block
    for ln in lines:  # classify each line from its tokens and comments
        if only is not None and ln.n not in only:  # lines outside the current <script> region are handled by other code
            continue  # leave their flags exactly as the markup pass set them
        # tokens starting here, minus acorn's end-of-file marker
        code = [t for t in on_line.get(ln.n, []) if t[0] != "eof"]
        if ln.why_exempt == "inside a multi-line string":  # lines of template text were decided in the first loop
            ln.code = False  # template text is never code to explain
            continue  # nothing more to decide for a template-text line
        ln.code = bool(code)  # any real token makes it a code line
        ln.comment = ln.n in com_lines and not code  # a comment touches the line and there is no code
        # code and a comment on the same line (a // after code, or a /* */ anywhere)
        ln.trailing = ln.n in com_lines and bool(code)
        if code:  # look for trivial code that needs no explanation
            # only closing parentheses, brackets, braces, commas or semicolons
            if all(t[0] in JS_CLOSERS for t in code):
                ln.why_exempt = "closing brackets only"  # it just ends a construct opened above
            # Only string pieces, template backticks, '+' concatenation and closers, with at least one real string: a
            # wrapped message or a chunk of markup text inside a script.
            elif all(t[0] in ("string", "template", "`", "+") or t[0] in JS_CLOSERS for t in code) and \
                    any(t[0] in ("string", "template") for t in code):  # needs an actual string or template piece
                ln.why_exempt = "string literal only"  # pure text, nothing to explain
    # label and value joined by 0x1F, tokens joined by 0x1E; comments are never tokens
    ident = "\x1e".join(f"{t[0]}\x1f{t[1]}" for t in res["tokens"] if t[0] != "eof")
    return _h(ident)  # the fingerprint of this file's or region's code


# Analyse a .js or .mjs file. path labels the result, text is its source and res is its entry from js_tokens, whose ok
# flag is False when acorn rejected the code. Returns an Analysis using '//' comments and the token identity. No side
# effects.
def analyze_js(path: str, text: str, res: dict) -> Analysis:
    # one Line per physical line, '//' comment syntax
    lines = [Line(i + 1, t, "js", "js") for i, t in enumerate(_lines(text))]
    a = Analysis(path, "js", lines)  # empty JavaScript result; flags are set below
    if not res.get("ok"):  # acorn could not parse the source, so the file fails
        a.ok, a.error = False, res.get("error", "parse error")  # keep the parser's message, or a generic one
        return a  # a file that does not parse gets no line classification
    a.identity = js_lines(lines, res)  # mark the lines and keep the token fingerprint
    finish(lines)  # shared rules: required versus exempt
    return a  # the finished JavaScript analysis, with lines and identity set


# ------------------------------------------------------------------------------------------------ CSS
# Character-level CSS scanner, written by hand because the standard library has no CSS tokenizer. text is the CSS to
# scan (a .css file, or an HTML page with everything outside one <style> region blanked), lines are the Line records to
# mark in place and only optionally restricts marking to a set of line numbers. Returns the comment-free code with
# whitespace normalised; its hash is the CSS identity. Three states are tracked: code, inside /* */, inside a string.
def css_scan(text: str, lines: list[Line], only: set | None = None) -> str:
    """Mark CSS lines (comments, strings, code); returns the comment-free normalised code."""
    i, n, row = 0, len(text), 1  # character index, text length, and the current 1-based row
    state = "code"  # scanner state: "code", "comment" or "string"
    quote = ""  # the quote character that opened the current string
    code_out = []  # characters kept for the identity; comment text is left out
    info: dict[int, dict] = {}  # per row: has code, has comment text, and the state at its end

    # Record that row r contains code or comment characters. r is a 1-based row number and key is "code" or "comment".
    # Creates the row's record on first use; returns nothing.
    def mark(r, key):
        # setdefault builds a record with every flag off the first time a row is seen
        info.setdefault(r, {"code": False, "comment": False, "start": None, "end": None})[key] = True

    line_start_state = {1: "code"}  # state at the start of each row; row 1 begins in code
    while i < n:  # walk the text one character at a time
        c = text[i]  # the character currently under the scanning cursor
        if c == "\n":  # end of a physical row of the stylesheet
            # remember how the row ended; outside "code" nothing may be appended
            info.setdefault(row, {"code": False, "comment": False})["end"] = state
            row += 1  # move the row counter on to the next row
            # the new row starts in this state, which decides whether a comment may go above it
            line_start_state[row] = state
            if state == "code":  # in code a line break is just whitespace
                code_out.append(" ")  # keep a separator so tokens on adjacent lines do not merge
            elif state == "string":  # a line break inside a string is kept verbatim
                code_out.append(c)  # the line break belongs to the string's content
            i += 1  # step past the line break
            continue  # the line break is fully handled
        if state == "code":  # ordinary code, outside comments and strings
            if text.startswith("/*", i):  # a slash-star sequence opens a comment here
                state = "comment"  # switch the scanner into comment mode
                mark(row, "comment")  # the row contains comment text
                i += 2  # skip both opening characters of the comment marker
                continue  # nothing from the comment enters the identity
            if c in "\"'":  # a single or double quote opens a string
                state, quote = "string", c  # enter string mode and remember which quote closes it
            if not c.isspace():  # visible characters make the row a code row
                mark(row, "code")  # flag the row as holding code
            code_out.append(c)  # keep the character for the identity text
        elif state == "comment":  # inside a slash-star comment block
            mark(row, "comment")  # the row holds comment text
            if text.startswith("*/", i):  # the star-slash pair closes the comment here
                state = "code"  # back to ordinary code after the comment
                i += 2  # skip the two closing characters of the marker
                continue  # the closing characters are not code either, so nothing is kept
        else:  # inside a quoted string literal value
            mark(row, "code")  # string content is part of the code
            code_out.append(c)  # keep the string character in the identity
            if c == "\\":  # a CSS escape: the following character is taken literally, even a quote
                code_out.append(text[i + 1:i + 2])  # keep the escaped character too
                # Skip both characters. An escaped line break here does not advance the row counter, so rows after it
                # would be off by one (rare in real stylesheets).
                i += 2
                continue  # continue with the character after the escape
            if c == quote:  # the matching quote ends the string
                state = "code"  # back to code after the string
        i += 1  # advance the cursor to the next character
    # record the final row's end state (text without a trailing newline)
    info.setdefault(row, {"code": False, "comment": False})["end"] = state
    for ln in lines:  # translate the per-row facts into Line flags
        if only is not None and ln.n not in only:  # rows outside the requested region are left alone
            continue  # continue with the following row instead
        d = info.get(ln.n, {})  # facts for the row; empty for rows the scanner never marked
        st = line_start_state.get(ln.n, "code")  # state in force when the row began
        ln.code = bool(d.get("code"))  # any visible character outside comments
        ln.comment = bool(d.get("comment")) and not ln.code  # comment text present and no code on the row
        if st != "code":  # the row begins inside a comment or a string
            ln.above = False  # a new /* */ line there would nest inside the open comment or string
            if st == "comment" and not ln.code:  # a middle line of a multi-line comment
                ln.comment = True  # counts as a comment line even without markers on it
        if d.get("end", "code") != "code":  # the row ends inside a comment or a string
            ln.trail = False  # an end comment would land inside it
        if ln.code and d.get("comment"):  # code and comment on one row: does the comment explain the code?
            stripped = re.sub(r"/\*.*?\*/", "", ln.text).strip()  # the row with complete /* */ comments removed
            # code remains and the row ends with '*/': a normal end-of-line comment
            ln.trailing = bool(stripped) and ln.text.rstrip().endswith("*/")
            if not ln.trailing:  # otherwise try the second pattern
                # a row closing a comment opened earlier, unless the row itself starts with '/*'
                ln.trailing = ln.text.strip().startswith("/*") is False and "*/" in ln.text
        # after removing comments, only '}' and ';' are left
        if ln.code and re.fullmatch(r"[\s};]*", re.sub(r"/\*.*?\*/", "", ln.text)):
            ln.why_exempt = "closing brackets only"  # such a row merely closes a rule
    s = "".join(code_out)  # join the kept characters into one string
    s = re.sub(r"\s+", " ", s)  # runs of whitespace (including newline stand-ins) become one space
    # Drop spaces around { } ; : , > so pure re-formatting leaves the identity alone. A side effect: 'a :hover' and
    # 'a:hover' (different selectors) normalise to the same text.
    s = re.sub(r"\s*([{};:,>])\s*", r"\1", s)
    return s.strip()  # the normalised comment-free CSS


# Analyse a stand-alone .css file. path labels the result and text is the stylesheet. Returns an Analysis that uses '/*
# */' comments and whose identity is the hash of css_scan's normalised code. No side effects.
def analyze_css(path: str, text: str) -> Analysis:
    # one Line per row, '/* */' comment syntax
    lines = [Line(i + 1, t, "css", "css") for i, t in enumerate(_lines(text))]
    a = Analysis(path, "css", lines)  # the stylesheet's result record, filled in by the scan
    a.identity = _h(css_scan(text, lines))  # scan the whole sheet, marking lines and hashing the normalised code
    finish(lines)  # decide required versus exempt for every line
    return a  # the finished CSS analysis with its identity hash


# ------------------------------------------------------------------------------------------------ HTML (templates)
# Template placeholders such as {{title}} and section markers {{#name}} / {{/name}}, which no JS/CSS parser accepts.
PLACEHOLDER = re.compile(r"\{\{[#/]?\w+\}\}")


# Make a template parseable by the JavaScript, CSS and HTML tokenizers. text is the template source. Returns the same
# text with every {{name}} replaced by $$name$$; the length is unchanged, so offsets, lines and columns still match.
def neutral(text: str) -> str:
    """Template placeholders replaced by same-length text that parses as JS/CSS/HTML ({{x}} -> $$x$$)."""
    # Replacement callback for one placeholder. m is the regex match; returns the substitute text of the same length.
    def sub(m):
        s = m.group(0)  # the whole placeholder text, braces included
        # the name without braces; '#' and '/' become '_' so it stays a valid identifier
        inner = s[2:-2].replace("#", "_").replace("/", "_")
        # four dollar signs replace four braces; '$' is legal in JS names and harmless in CSS
        return "$$" + inner + "$$"
    return PLACEHOLDER.sub(sub, text)  # apply the callback to every placeholder in the text


# Isolate one region of a page while keeping positions. text is the page and start/end are character offsets of the
# region (end exclusive). Returns a same-length string with everything outside the region turned into spaces, newlines
# kept, so a tokenizer run on it reports lines and columns that match the page.
def blank_outside(text: str, start: int, end: int) -> str:
    """Keep text[start:end] in place; every other character becomes a space (newlines kept)."""
    out = []  # characters of the result, built one at a time
    for i, c in enumerate(text):  # look at every character together with its offset
        # inside the region keep it; outside keep only line breaks
        out.append(c if start <= i < end or c == "\n" else " ")
    return "".join(out)  # the blanked copy joined back into one string


# Line number (1-based) of character offset pos in text. Counts the newlines before pos; used to turn regex offsets of
# <style>/<script> regions into lines.
def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1  # newlines before the offset, plus one


# html.parser subclass that records what the audit needs from markup: an event list for the identity, where each tag and
# comment starts and ends, and which lines hold text. analyze_html feeds it a copy of the page whose style and script
# bodies have been blanked out.
class _Html(html.parser.HTMLParser):
    # Set up the parser and the empty collections. Takes no arguments besides the instance; nothing is returned.
    def __init__(self):
        # convert_charrefs=False hands text over at once instead of buffering it to decode &amp; and friends. Entity and
        # character references then arrive through handle_entityref and handle_charref below, which record them.
        super().__init__(convert_charrefs=False)
        self.events = []  # ordered start tags, end tags, text and declarations for the identity
        self.tags = []        # (start_line, end_line, kind, tag name): kind is "start" or "end"
        self.comments = []    # (start_line, end_line)
        self.data_lines = set()  # line numbers that hold non-blank text between tags

    # First and last line of the construct being handled. text is its source; the start comes from the parser's current
    # position and the end adds the newlines the text contains. Returns (start_line, end_line).
    def _span(self, text):
        sl, _ = self.getpos()  # getpos() gives (line, column) of the start of the current construct
        return sl, sl + text.count("\n")  # lines spanned by the construct, first and last

    # Called for each opening tag such as <div class="a">. tag is the lower-cased name and attrs a list of (name, value)
    # pairs. Records the tag's lines and an identity event.
    def handle_starttag(self, tag, attrs):
        txt = self.get_starttag_text() or ""  # raw source of the tag, which may span several lines; "" as a fallback
        sl, el = self._span(txt)  # the tag's first and last line numbers
        self.tags.append((sl, el, "start", tag))  # lets continuation lines of a long tag refuse a comment above them
        self.events.append(("s", tag, tuple(attrs)))  # tag name and attributes (as a hashable tuple) enter the identity

    # Called for self-closing tags such as <br/>; tag and attrs as for an opening tag. Handled exactly like one.
    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)  # delegate to the opening-tag handler

    # Called for closing tags such as </div>; tag is the lower-cased name. Records the line and an identity event.
    def handle_endtag(self, tag):
        sl, _ = self.getpos()  # closing tags are assumed to sit on one line
        self.tags.append((sl, sl, "end", tag))  # a closing tag starts and ends on the same line
        self.events.append(("e", tag))  # closing tags are part of the structure the identity guards

    # Called for <!-- ... --> comments; data is the text between the markers. Their lines matter for placement, but they
    # never enter the identity, since comments are what the audit adds.
    def handle_comment(self, data):
        # rebuild the full comment source so its newline count gives the end line
        sl, el = self._span("<!--" + data + "-->")
        self.comments.append((sl, el))  # remember the comment's line span

    # Called for text between tags; data is the raw text including surrounding whitespace.
    def handle_data(self, data):
        if data.strip():  # whitespace-only text is formatting, not content
            sl, el = self._span(data)  # lines from where the text starts to where it ends
            # collapse inner whitespace so re-indenting text does not change the identity
            self.events.append(("d", re.sub(r"\s+", " ", data.strip())))
            # Mark every line the text spans. Its trailing newline and indentation reach the row of the next tag, so a
            # closing tag that follows text on its own line is marked as holding text too.
            for r in range(sl, el + 1):
                self.data_lines.add(r)  # this line holds page text

    # Called for declarations such as <!DOCTYPE html>; decl is the text inside '<!' and '>'.
    def handle_decl(self, decl):
        self.events.append(("decl", decl))  # the doctype is part of the page structure

    # Called for a named reference in text such as &amp; or &hellip;; name is the part between '&' and ';'. Recorded so
    # that swapping one entity for another changes the identity.
    def handle_entityref(self, name):
        self.events.append(("ent", name))  # the entity's name as an identity event, in document order

    # Called for a numeric reference such as &#8212; or &#x2014;; name is the part after '&#' (with the x for hex).
    def handle_charref(self, name):
        self.events.append(("chr", name))  # the number as written, so a changed code point changes the identity


# Analyse an HTML page or template. path labels the result, text is the source, and js_results optionally holds
# tokenizer replies already computed for its scripts, keyed "<path>#<index>". Markup lines use <!-- --> comments,
# <style> bodies /* */ and <script> bodies //. The identity combines the markup events, each style block's CSS identity,
# each script's token identity and a hash of each JSON data block. Starts node only if a script's reply is missing.
def analyze_html(path: str, text: str, js_results: dict | None = None) -> Analysis:
    # one Line per row; markup comment syntax by default
    lines = [Line(i + 1, t, "html", "html") for i, t in enumerate(_lines(text))]
    a = Analysis(path, "html", lines)  # the page's result record, filled in step by step
    neu = neutral(text)  # placeholders made parseable, with every offset unchanged
    regions = []          # (kind, content_start, content_end)
    # each <style ...> or <script ...> opening tag, any case; group 2 holds its attributes
    for m in re.finditer(r"<(style|script)\b([^>]*)>", neu, re.I):
        tag, attrs = m.group(1).lower(), m.group(2)  # lower-cased tag name and the raw attribute text
        # the matching closing tag after it, spaces allowed before '>'
        close = re.compile(r"</%s\s*>" % tag, re.I).search(neu, m.end())
        if not close:  # an element that never closes before the page ends
            # the rest of the page would be ambiguous, so report it as unparsable
            a.ok, a.error = False, f"unclosed <{tag}>"
            return a  # stop: no reliable line classification is possible
        # A style element holds CSS; a script typed application/json or application/ld+json holds data; any other script
        # is JavaScript.
        kind = "css" if tag == "style" else ("data" if re.search(r'type\s*=\s*"application/(ld\+)?json"', attrs)
                                             else "js")  # everything else in a script tag is code
        # content runs from right after the opening tag to right before the closing one
        regions.append((kind, m.end(), close.start()))
    masked = list(neu)  # mutable character list of the page
    for kind, s, e in regions:  # blank every region's content in the masked copy
        for i in range(s, e):  # each character offset inside the region
            if masked[i] != "\n":  # line breaks stay so line numbers do not move
                masked[i] = " "  # the HTML parser then sees empty elements on the right lines
    masked = "".join(masked)  # join the masked characters back into one string
    p = _Html()  # a fresh markup recorder for this page
    p.feed(masked)  # parse the masked page, recording tags, text and comments
    p.close()  # flush any text still buffered at the end
    ident_parts = [json.dumps(p.events)]  # first identity part: the serialised event list
    in_region: dict[int, str] = {}  # line number to region kind, for lines lying fully inside a style/script body
    for kind, s, e in regions:  # work out which lines each region covers
        sl, el = line_of(neu, s), line_of(neu, e)  # lines of the content's first and last characters
        # The text between the line start and the content start includes the opening tag itself, so the region normally
        # begins on the line after the tag.
        first_full = sl if neu[neu.rfind("\n", 0, s) + 1:s].strip() == "" else sl + 1
        # Likewise the text from the content's end to the end of its line includes the closing tag, so that line
        # normally stays markup.
        last_full = el if neu[e:neu.find("\n", e) if neu.find("\n", e) >= 0 else len(neu)].strip() == "" else el - 1
        for r in range(first_full, last_full + 1):  # every full line of the region's content
            in_region[r] = kind  # remember that the line belongs to this region kind
        # On the opening-tag line of a multi-line region an end comment would sit inside the element, so it must use CSS
        # or JS syntax; a JSON data block keeps HTML syntax (its lines are exempt anyway).
        lines[sl - 1].ctx_end = {"css": "css", "js": "js"}.get(kind, "html") if sl != el else lines[sl - 1].ctx_end
        if kind == "data":  # a JSON data block inside a script element
            for r in range(sl, el + 1):  # every line of it, tag lines included
                lines[r - 1].why_exempt = "data"  # data is not code that needs explaining
                lines[r - 1].code = False  # and these lines are excluded from code counting
            ident_parts.append(_h(neu[s:e]))  # hash the JSON text verbatim so any change to the data shows up
    # markup lines
    # Long tags: rows after a tag's first line are attribute continuations, and rows before its last line cannot end
    # with a comment.
    for sl, el, kind, tag in p.tags:
        for r in range(sl + 1, el + 1):  # rows after the tag's first line
            lines[r - 1].above = False  # a comment line there would split the tag's attributes
            # exempt as part of a tag already explained on its first line
            lines[r - 1].why_exempt = lines[r - 1].why_exempt or "inside a tag"
        for r in range(sl, el):  # rows the tag continues past
            lines[r - 1].trail = False  # an appended comment would land inside the tag
    for sl, el in p.comments:  # each HTML comment's first and last row; a one-row comment leaves the flags unchanged
        for r in range(sl, el + 1):  # each row of the comment, first to last
            ln = lines[r - 1]  # the row's Line record for flag updates
            if r > sl:  # rows after the comment's first
                ln.above = False  # no new comment line in the middle of an existing comment
            if r < el:  # rows before the comment's last
                ln.trail = False  # nothing may be appended inside the comment
    tag_lines: dict[int, list] = {}  # line number to the kinds ("start"/"end") of the tags beginning on it
    for sl, el, kind, tag in p.tags:  # index every tag by the line where it begins
        tag_lines.setdefault(sl, []).append(kind)  # group the tag kinds under that starting line
    com_lines = {r for sl, el in p.comments for r in range(sl, el + 1)}  # every line touched by an HTML comment
    pre_open = 0  # depth of open <pre>/<textarea> elements, where whitespace is visible content
    for ln in lines:  # classify the markup lines outside style and script bodies
        if ln.n in in_region:  # region lines are handled by the CSS and JS passes below
            continue  # move on without touching the region's lines
        kinds = tag_lines.get(ln.n, [])  # kinds of the tags that begin on this line
        has_data = ln.n in p.data_lines  # does the line hold page text?
        ln.code = bool(kinds) or has_data or ln.code  # a tag or text makes it a code line
        stripped = re.sub(r"<!--.*?-->", "", ln.text).strip()  # the line with any complete comments removed
        ln.comment = ln.n in com_lines and not stripped  # a lone comment on the line, without markup
        # markup that ends with a comment is already explained
        ln.trailing = ln.n in com_lines and bool(stripped) and ln.text.rstrip().endswith("-->")
        if kinds and all(k == "end" for k in kinds) and not has_data:  # only closing tags on the line and no text
            ln.why_exempt = ln.why_exempt or "closing tags only"  # nothing new to explain on such a line
        elif not kinds and has_data:  # text without tags is page content, not code
            ln.why_exempt = ln.why_exempt or "text only"  # exempt as page text rather than markup
        if re.search(r"<(pre|textarea)\b", ln.text, re.I):  # a <pre> or <textarea> opens on this line
            pre_open += 1  # one nesting level deeper into preformatted content
        if pre_open:  # inside preformatted content an inserted comment would change what the page shows
            ln.above, ln.trail = False, False  # neither above nor at the end
            # exempt because of the preformatting rule just applied
            ln.why_exempt = ln.why_exempt or "inside pre/textarea"
        if re.search(r"</(pre|textarea)\s*>", ln.text, re.I):  # the preformatted element closes on this line
            pre_open = max(0, pre_open - 1)  # one level shallower, never below zero
    # CSS and JS regions
    # Second pass over the regions in source order; idx matches the numbering used by html_js_sources.
    for idx, (kind, s, e) in enumerate(regions):
        # lines lying fully inside this region
        rows = {r for r, k in in_region.items() if k == kind and line_of(neu, s) <= r <= line_of(neu, e)}
        for r in rows:  # each such line gets the region's comment syntax
            # comments use the region's syntax; JSON data falls back to HTML
            lines[r - 1].ctx_start = lines[r - 1].ctx_end = kind if kind != "data" else "html"
        if kind == "css":  # a style block holding CSS rules
            # scan just this region (rest blanked so rows match) and add its CSS identity
            ident_parts.append(_h(css_scan(blank_outside(neu, s, e), lines, rows)))
        elif kind == "js":  # an executable script holding JavaScript code
            key = f"{path}#{idx}"  # key shared with html_js_sources so a batched reply can be found
            res = (js_results or {}).get(key)  # use the reply computed in advance, if the caller passed one
            if res is None:  # no reply was prepared for this script
                # tokenize it on its own now (starts node)
                res = js_tokens([(key, blank_outside(neu, s, e), False)])[key]
            if not res.get("ok"):  # acorn rejected the script's syntax
                # name the failing script by its index and pass on the message
                a.ok, a.error = False, f"script {idx}: {res.get('error')}"
                return a  # stop: identity cannot be computed
            ident_parts.append(js_lines(lines, res, rows))  # mark the script's lines and add its token identity
    a.identity = _h("\x1e".join(ident_parts))  # one hash over all parts, separated by 0x1E
    # region kinds with first and last lines, for diagnostics
    a.extra["regions"] = [(k, line_of(neu, s), line_of(neu, e)) for k, s, e in regions]
    finish(lines)  # required versus exempt for every line
    return a  # the finished page analysis with combined identity


# List the JavaScript bodies of a page so analyze_many can tokenize every script of every page in one node run. path and
# text describe the page. Returns [("<path>#<index>", body with the rest of the page blanked, False)]; False means
# classic-script parsing. The index counts every <style>/<script> match, exactly as analyze_html numbers them.
def html_js_sources(path: str, text: str) -> list[tuple[str, str, bool]]:
    neu = neutral(text)  # placeholders made parseable, same length
    out = []  # collected script sources for the batch
    # same regex and numbering as analyze_html
    for idx, m in enumerate(re.finditer(r"<(style|script)\b([^>]*)>", neu, re.I)):
        tag, attrs = m.group(1).lower(), m.group(2)  # lower-cased tag name and raw attribute text
        close = re.compile(r"</%s\s*>" % tag, re.I).search(neu, m.end())  # the closing tag match, or None if absent
        # only executable scripts are tokenized; styles and JSON data are not JavaScript
        if tag == "script" and close and not re.search(r'type\s*=\s*"application/(ld\+)?json"', attrs):
            # the body in place, everything else blanked, parsed as a classic script
            out.append((f"{path}#{idx}", blank_outside(neu, m.end(), close.start()), False))
    return out  # the request list handed to js_tokens later


# ------------------------------------------------------------------------------------------------ XML
# Analyse an XML file, including PowerShell .ps1xml format data. path labels the result and text is the document. The
# identity is the hash of the C14N 2.0 canonical form with comments dropped and text whitespace stripped; line flags
# come from a small scanner that tracks three states: text, inside a tag, inside a comment. No side effects.
def analyze_xml(path: str, text: str) -> Analysis:
    # one Line per row, <!-- --> comment syntax
    lines = [Line(i + 1, t, "xml", "xml") for i, t in enumerate(_lines(text))]
    a = Analysis(path, "xml", lines)  # the document's result record, filled below
    try:  # canonicalisation fails on malformed XML
        # Remove a byte-order mark (the invisible U+FEFF character in the quotes) and the <?xml ...?> declaration, so
        # only the document itself is canonicalised.
        body = re.sub(r"^\s*<\?xml[^>]*\?>", "", text.lstrip("﻿"))
        # canonical XML without comments: attribute order, quoting and spacing no longer matter
        a.identity = _h(ET.canonicalize(body, strip_text=True, with_comments=False))
    except Exception as e:  # noqa: BLE001
        a.ok, a.error = False, f"XML: {e}"  # keep the parser's complaint for the report
        return a  # no line classification for a broken document
    i, n, row, state = 0, len(text), 1, "text"  # character index, length, current 1-based row, scanner state
    info: dict[int, dict] = {}  # per row: tag kinds begun, comment present, text present, state at the end
    start_state = {1: "text"}  # state at the start of each row; row 1 starts in text
    while i < n:  # walk the document one character at a time
        c = text[i]  # the XML character being examined now
        # this row's record, created on first use
        d = info.setdefault(row, {"tag": [], "comment": False, "data": False})
        if c == "\n":  # end of a physical row of the document
            d["end"] = state  # remember the state in which the row ended
            row += 1  # advance the row counter by one line
            start_state[row] = state  # the new row starts in the current state
            i += 1  # step past the line break
            continue  # the line break is fully handled now
        if state == "text":  # between tags, in element text
            if text.startswith("<!--", i):  # a comment marker opens right here
                state = "comment"  # switch the scanner into comment state
                d["comment"] = True  # the row holds comment text
                i += 4  # skip the four characters of the comment opener
                continue  # comment markers are not content or code
            if c == "<":  # any other '<' opens a tag (also <?...?> and <!DOCTYPE>, treated alike)
                state = "tag"  # now inside a tag until the next '>'
                # note whether it is a closing tag ('</') or an opening one
                d["tag"].append("end" if text.startswith("</", i) else "start")
            elif not c.isspace():  # a visible character outside tags is element text
                d["data"] = True  # the row holds text content
        elif state == "comment":  # inside a comment block of the document
            d["comment"] = True  # every row the comment touches is marked
            if text.startswith("-->", i):  # the comment closing marker appears here
                state = "text"  # back to element text after the comment
                i += 3  # skip the three characters of the closing marker
                continue  # closing markers handled; continue scanning
        else:  # inside a tag, between its angle brackets
            if c == ">":  # the tag ends at the next '>' (a '>' inside an attribute value would end it early)
                state = "text"  # back to text after the tag
        i += 1  # advance the cursor to the following character
    # record the last row's end state when the text has no final newline
    info.setdefault(row, {"tag": [], "comment": False, "data": False})["end"] = state
    # Row of the <?xml ...?> declaration: the first non-blank row when it starts with one (only blank rows may come
    # before it, or the document would not have parsed above), else 0. That row and the blank rows before it accept
    # no comment above them; a declaration that is not on row 1 must not block row 1 alone and leave its own row open.
    first = next((ln for ln in lines if ln.text.strip("\ufeff \t\r")), None)  # the first row with any content
    decl_row = first.n if first and first.text.lstrip("\ufeff \t").startswith("<?xml") else 0
    for ln in lines:  # turn the row facts into Line flags
        # the row's facts, or an empty record for a row never visited
        d = info.get(ln.n, {"tag": [], "comment": False, "data": False})
        st = start_state.get(ln.n, "text")  # scanner state at the start of the row
        # a tag begins here, text sits here, or the row continues a tag from above
        ln.code = bool(d["tag"]) or d["data"] or st == "tag"
        ln.comment = d["comment"] and not ln.code  # comment text present with no code on the row
        # markup ending in a comment is already explained
        ln.trailing = d["comment"] and ln.code and ln.text.rstrip().endswith("-->")
        if st != "text":  # the row begins inside a tag or comment
            ln.above = False  # a comment line may not be inserted above it
            if st == "tag":  # the row continues a tag's attributes
                ln.why_exempt = "inside a tag"  # explained by the comment on the tag's first row
            if st == "comment" and not ln.code:  # a middle row of a multi-line comment
                ln.comment = True  # counts as a comment line of the block
        if d.get("end", "text") != "text":  # the row ends inside a tag or comment
            ln.trail = False  # no comment may be appended after its last character
        # only closing tags present and no element text
        if d["tag"] and all(k == "end" for k in d["tag"]) and not d["data"]:
            ln.why_exempt = ln.why_exempt or "closing tags only"  # it just closes elements opened above
        if ln.n <= decl_row:  # the XML declaration's row, or a blank row before it
            ln.above = False  # a comment there would precede the declaration, which must stay first
    finish(lines)  # required versus exempt for each row
    return a  # the finished XML analysis with canonical identity


# ------------------------------------------------------------------------------------------------ TOML, requirements
# Analyse a TOML file such as pyproject.toml. path labels the result and text is the document. The identity is the hash
# of the parsed data dumped as key-sorted JSON, so comments and layout never count. Lines are classified by a small
# scanner that understands quoted strings, multi-line strings and '#' comments. No side effects.
def analyze_toml(path: str, text: str) -> Analysis:
    import tomllib  # standard library from Python 3.11; imported here so other languages work on older Pythons
    lines = [Line(i + 1, t, "toml", "toml") for i, t in enumerate(_lines(text))]  # one Line per row, '#' comment syntax
    a = Analysis(path, "toml", lines)  # the TOML file's result record
    try:  # tomllib rejects invalid TOML
        # Parse and dump the data in a stable key order. TOML dates and times are not JSON types, so default=str writes
        # them in their ISO text form (2026-10-28, 07:32:00) instead of failing on them.
        a.identity = _h(json.dumps(tomllib.loads(text), sort_keys=True, default=str))
    except Exception as e:  # noqa: BLE001
        a.ok, a.error = False, f"TOML: {e}"  # keep the TOML parser's reason for the report
        return a  # no line classification for an unparsable file
    in_ml = None  # the closing delimiter (""" or ''') while inside a multi-line string, else None
    for ln in lines:  # classify each row in order, tracking multi-line strings
        t = ln.text  # the row's text without its newline
        if in_ml:  # this row continues a multi-line string
            # string content cannot take a comment above and needs none
            ln.above, ln.why_exempt = False, "inside a multi-line string"
            if in_ml in t:  # the closing delimiter appears on this row
                in_ml = None  # the multi-line string ends on this row
            else:  # still inside the string at the row's end
                ln.trail = False  # nothing may be appended inside the string
            continue  # rows inside the string are fully decided (the closing row is not scanned further)
        code, j, q = [], 0, None  # characters outside comments, the scan position, the open one-line quote if any
        while j < len(t):  # scan the row character by character
            ch = t[j]  # the current character of the row being scanned
            if q:  # inside a quoted single-line string value
                # this test ends in "and False", so it can never hold: leftover code with no effect
                if t.startswith(q * 3, j) and len(q) == 1 and False:
                    pass  # does nothing; the statement only fills the block
                if ch == "\\" and q == '"':  # basic (double-quoted) strings have backslash escapes; literal ones do not
                    j += 2  # skip the escape pair so an escaped quote does not end the string
                    continue  # the escape pair is consumed, nothing more to check
                if ch == q:  # the same quote character closes the string
                    q = None  # no longer inside a quoted string
                code.append(ch)  # string characters count as code
            elif ch == "#":  # a '#' outside strings starts a comment
                ln.trailing = bool("".join(code).strip())  # code before it makes this an end-of-line comment
                ln.comment = not ln.trailing  # otherwise the whole row is a comment
                break  # the rest of the row is comment text
            else:  # an ordinary code character outside strings
                for mq in ('"""', "'''"):  # check for an opening triple quote
                    # a triple quote that occurs only once on the row opens a multi-line string
                    if t.startswith(mq, j) and t.count(mq) == 1:
                        in_ml = mq  # remember which delimiter will close the string
                # a single quote character opens a one-line string, unless it began a triple quote
                if ch in "\"'" and not in_ml:
                    q = ch  # remember the opening quote character
                code.append(ch)  # keep the character as code text
            j += 1  # advance to the following character position
        if in_ml:  # the row opened a multi-line string that runs past its end
            ln.trail = False  # an end comment would become string text
        ln.code = bool("".join(code).strip())  # code when something other than whitespace lies outside comments
        # only ']', '}' and ',' left: the end of an array or inline table
        if ln.code and re.fullmatch(r"[\s\],}]*", "".join(code)):
            ln.why_exempt = "closing brackets only"  # it merely closes an array or table
    finish(lines)  # required versus exempt for every row
    return a  # the finished TOML analysis with data identity


# Analyse a pip requirements file. path labels the result and text is the file. Every non-comment line is a requirement
# or option; the identity is the hash of those specs, one per line, with comments and surrounding spaces removed.
def analyze_req(path: str, text: str) -> Analysis:
    lines = [Line(i + 1, t, "req", "req") for i, t in enumerate(_lines(text))]  # one Line per row, '#' comment syntax
    a = Analysis(path, "req", lines)  # the requirements file's result record
    specs = []  # requirement specs in file order
    for ln in lines:  # classify each row of the requirements file
        s = ln.text  # the row's text, including any comment
        # start of an inline space-hash comment, 0 for a full comment row, -1 when absent
        cut = s.find(" #") if not s.lstrip().startswith("#") else 0
        body = s[:cut] if cut >= 0 else s  # the requirement text before any inline comment
        if s.lstrip().startswith("#"):  # the row is entirely a comment line
            ln.comment = True  # it can explain the requirement line below
            continue  # nothing more to examine on this comment row
        ln.code = bool(body.strip())  # a requirement spec is present on the row
        ln.trailing = cut > 0  # an inline comment already explains the spec
        if body.strip():  # collect non-empty specs for the identity
            specs.append(body.strip())  # stripped so indentation changes do not count
    a.identity = _h("\n".join(specs))  # fingerprint of the requirement specs in order
    finish(lines)  # required versus exempt for the rows
    return a  # the finished requirements analysis for this file


# ------------------------------------------------------------------------------------------------ entry
# Analyse many files at once, batching the external tokenizers: one PowerShell run for every .ps1 file and one node run
# for every JavaScript source, including scripts inside HTML pages. items is a list of (path, text) pairs and frozen
# maps a path to line numbers that must stay untouched (template lines other code depends on). Returns {path: Analysis}.
# Side effects: temporary files and child processes; raises ValueError for a file type with no analyser.
def analyze_many(items: list[tuple[str, str]], frozen: dict | None = None) -> dict[str, Analysis]:
    """items: [(path, text)] -> {path: Analysis}. Batches the PowerShell and JavaScript tokenizers."""
    frozen = frozen or {}  # no frozen lines unless the caller gives some
    out: dict[str, Analysis] = {}  # finished analyses collected by file path
    ps_items = [(p, t) for p, t in items if lang_of(p) == "ps"]  # PowerShell files, tokenized together
    # .js and .mjs files, sent to the Node tokenizer together with the HTML scripts
    js_items = [(p, t) for p, t in items if lang_of(p) == "js"]
    # HTML pages, whose inline scripts join the JavaScript batch
    html_items = [(p, t) for p, t in items if lang_of(p) == "html"]
    ps_res = {}  # tokenizer replies for PowerShell files, by original path
    if ps_items:  # only start PowerShell when the batch contains script files
        # Parser.ParseFile reads from disk, so in-memory texts are written to temporary files
        with tempfile.TemporaryDirectory() as td:
            files = []  # paths of the temporary script copies
            for k, (p, t) in enumerate(ps_items):  # one copy per PowerShell text
                f = Path(td) / f"f{k}{Path(p).suffix}"  # numbered names avoid clashes; the original extension is kept
                # newline="\n" writes the text exactly, with no conversion to Windows line endings
                f.write_text(t, encoding="utf-8", newline="\n")
                files.append(str(f))  # collect the copy's path for the batch request
            res = ps_tokens(files)  # one PowerShell process serves the whole batch, because its start-up is slow
            for (p, t), f in zip(ps_items, files):  # pair each original path with its temporary copy
                ps_res[p] = res.get(str(Path(f).resolve()))  # map the copy's reply back to the original path
    js_res = {}  # tokenizer replies for JavaScript, by source id
    # .mjs files parse as ES modules, .js files as classic scripts
    srcs = [(p, t, Path(p).suffix == ".mjs") for p, t in js_items]
    for p, t in html_items:  # add each page's inline scripts
        srcs += html_js_sources(p, t)  # ids are "<path>#<index>", matching analyze_html's lookup
    if srcs:  # only start node if there is JavaScript to read
        js_res = js_tokens(srcs)  # one node process for every JavaScript source
    for p, t in items:  # analyse each file in input order with its language's analyser
        lang = lang_of(p)  # language key from the file name
        if lang == "py":  # Python needs no helper process
            a = analyze_py(p, t)  # ast and tokenize run inside this process
        elif lang == "ps":  # PowerShell scripts, modules and data files
            a = analyze_ps(p, t, ps_res.get(p))  # uses the batched reply; None becomes a "no tokens" failure
        elif lang == "js":  # stand-alone JavaScript or ES module file
            # a missing reply becomes a parse failure instead of a crash
            a = analyze_js(p, t, js_res.get(p, {"ok": False, "error": "no result"}))
        elif lang == "css":  # stylesheet files with the .css extension
            # no external tokenizer here: css_scan marks comment, string and code rows in plain Python
            a = analyze_css(p, t)
        elif lang == "html":  # HTML page or template with embedded regions
            a = analyze_html(p, t, js_res)  # gets every JavaScript reply and picks out its own keys
        elif lang == "xml":  # XML documents, including PowerShell format files
            a = analyze_xml(p, t)  # canonical XML identity with comments dropped
        elif lang == "toml":  # TOML configuration such as pyproject.toml
            a = analyze_toml(p, t)  # identity computed from the parsed data
        elif lang == "req":  # pip requirements files listing packages
            a = analyze_req(p, t)  # identity computed from the list of specs
        else:  # an unknown language key has no matching analyser
            # discover only offers known types, so this signals a programming error
            raise ValueError(f"no analyser for {p}")
        for r in frozen.get(p, []):  # frozen line numbers for this path, if any
            if 1 <= r <= len(a.lines):  # ignore numbers past the end of the file
                ln = a.lines[r - 1]  # the frozen line's record in the analysis
                ln.above = ln.trail = ln.required = False  # nothing above, nothing appended, nothing required
                ln.why_exempt = "frozen (template structure)"  # reason shown by `code show`
        out[p] = a  # store the analysis under its path
    return out  # every file's analysis keyed by its path


# Convenience wrapper for one file. path and text as for analyze_many, frozen likewise; returns that file's Analysis.
# May start node or PowerShell, exactly as the batch version does.
def analyze(path: str, text: str, frozen: dict | None = None) -> Analysis:
    return analyze_many([(path, text)], frozen)[path]  # a batch of one file, unpacked again
