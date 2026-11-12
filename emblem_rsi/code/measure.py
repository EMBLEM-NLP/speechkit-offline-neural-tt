# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""The code-comment audit, run through the emblem-rsi loop (seal, measure, promote or veto).

    seal_run(run_dir, repo, rubric, tests, frozen)   snapshot every code file as the baseline, seal the rubric
    measure(run_dir, note, tests)                   measure the repository's files now -> measure-NNN.json
    check(run_dir, rels)                            apply the maps in memory and report, writing nothing
    apply(run_dir, rels)                            write baseline + maps to the repository (snapshot first)
    revert(run_dir)                                 put back the files the last apply replaced

Metrics (the rubric in emblem_rsi/rubrics/code-comments.json sets the thresholds):
  identity_ratio        files whose code is unchanged from the baseline (comments removed, by the language's own
                        tokenizer or syntax tree)                                                       gate, guard
  parse_ratio           files that still parse                                                          gate, guard
  tests_pass            share of the test commands that pass                                            gate, guard
  forbidden_count       added comments with forbidden text (template markers and project-specific text in
                        templates, private strings given at run time)                                   gate
  coverage              required code lines covered by a comment on the line or directly above          guard
  files_complete_ratio  files at 100% coverage
  detail_words          mean words per added comment
  echo_ratio            added comments that only repeat the code's own words
  duplicate_ratio       added comments repeated 3 or more times in one file
  placeholder_count     added comments that are placeholders (TODO, ...)
  long_lines            lines the comments pushed past 120 characters (code that was already longer is not counted)
"""
# Defer evaluation of type hints so annotations like `Path | None` work on every supported Python 3 release.
from __future__ import annotations

import collections  # Counter tallies comment texts and lines as multisets
import hashlib  # SHA-256 fingerprints for files, pages and the probe
import json  # manifest, measurements and snapshot lists are stored as JSON
import os  # reads the private forbidden strings from the environment
import re  # regular expressions for words, placeholders and whitespace
import shutil  # copies files into snapshots and removes reverted snapshots
import subprocess  # runs each test command as a separate child process
import time  # wall-clock timing of each test command in seconds
from pathlib import Path  # filesystem paths for the run directory and repository

from .. import loop  # the generic emblem-rsi loop: sealing, rubric scoring and decisions
from . import cmap, langs  # comment maps and the per-language line analysis

MANIFEST = "manifest.json"  # file in the run directory that lists every sealed file
# the rubric shipped beside this package, used unless another is named
DEFAULT_RUBRIC = Path(__file__).resolve().parents[1] / "rubrics" / "code-comments.json"
# Path fragments never audited: the vendored acorn parser, Python bytecode caches, node packages and git metadata.
EXCLUDE = ("emblem_rsi/js/vendor/", "/__pycache__/", "/node_modules/", ".git/")
# Text that must never appear in any added comment, in any file; matched without regard to case. Private strings (an
# owner's name or e-mail domain, say) are given at run time in EMBLEM_CODE_FORBIDDEN, separated by '|', so they never
# have to be written into the code itself; empty entries are dropped.
FORBIDDEN_ALWAYS = tuple(s for s in os.environ.get("EMBLEM_CODE_FORBIDDEN", "").split("|") if s.strip())
# Project-specific text (a title, tagline, slug, colours, font) that must not leak into the comments of the shared
# templates, which ship inside every book's output; matched case-sensitively. Like the private strings above it is given
# at run time, in EMBLEM_CODE_FORBIDDEN_TEMPLATE separated by '|', so no particular book is named in the code.
FORBIDDEN_TEMPLATE = tuple(s for s in os.environ.get("EMBLEM_CODE_FORBIDDEN_TEMPLATE", "").split("|") if s.strip())
# Common English words ignored when judging whether a comment merely repeats the code's own words (see is_echo).
STOP = set("""a an the of to and or is are be been it its this that these those for in on at with as by from into
than then so if else when while not no yes can may must will would should could do does did done has have had
which what who where how why all any each every both either one two use uses used using here there also just only
very more most less such same other""".split())  # split() turns the block into single separate words
WIDTH = 120  # maximum length of a new or changed line, in characters


# List every code file under the repository. repo is the repository root. Returns sorted posix-style paths relative to
# it, skipping excluded folders and non-Python files inside fixtures folders (test data). Reads directory listings only.
def discover(repo: Path) -> list[str]:
    """Every code file under the repository (posix paths relative to it), minus vendored and fixture data."""
    out = []  # relative paths of the files to audit
    for p in sorted(repo.rglob("*")):  # every path below the root, sorted for a stable order
        if not p.is_file():  # folders and other non-files are not audited
            continue  # skip to the next path in the listing
        rel = p.relative_to(repo).as_posix()  # forward-slash relative path, the same on Windows and Linux
        # the leading slash lets fragments like '/__pycache__/' match top-level folders too
        if any(x in "/" + rel for x in EXCLUDE):
            continue  # excluded paths are never audited or sealed
        # fixture data such as sample pages is not code to explain; fixture Python files are. The leading slash
        # makes a top-level fixtures/ folder match as well as a nested one.
        if "/fixtures/" in "/" + rel and p.suffix != ".py":
            continue  # skip the non-Python fixture file entirely
        if langs.lang_of(p):  # only file types that have a language analyser
            out.append(rel)  # remember the file as part of the audit list
    return out  # the sorted list of auditable files


# Whether a file belongs to a template folder, whose contents ship inside every book's output. rel is the
# repository-relative path; returns True when a 'template' folder appears anywhere in it. Template comments get extra
# forbidden-text rules.
def is_template(rel: str) -> bool:
    return "/template/" in "/" + rel  # a leading slash lets a top-level template folder match too


# SHA-256 of raw bytes as 64 hexadecimal characters. b is the content to hash; used for file hashes, the page hash and
# the probe hash.
def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()  # lower-case hex digest of the given bytes


# Read the run's manifest.json written by seal_run. run_dir is the run directory. Returns the manifest dict (repository,
# tests, frozen lines, per-file identities); raises loop.LoopError when the run was never sealed.
def load_manifest(run_dir: Path) -> dict:
    p = run_dir / MANIFEST  # where seal_run wrote the manifest file
    if not p.exists():  # a missing manifest means the run was never sealed
        # tell the user which command to run first
        raise loop.LoopError(f"no {MANIFEST} in {run_dir}: run `emblem-rsi code seal` first")
    return json.loads(p.read_text(encoding="utf-8"))  # the manifest as a dictionary, decoded from UTF-8 JSON


# Start a run: seal the rubric and snapshot every code file as the baseline. run_dir is the new run directory, repo the
# repository root, rubric an optional rubric path (default code-comments.json), tests a list of test commands (each a
# list of arguments), frozen {path: [line numbers]} that must stay untouched, files an optional explicit file list and
# log a print-like function. Writes rubric.sealed.json, baseline/ copies, an empty maps/ folder and manifest.json, and
# returns the manifest. Raises loop.LoopError when already sealed or when a file does not parse.
def seal_run(run_dir: Path, repo: Path, rubric: Path | None = None, tests: list | None = None,
             # files limits the audit to a chosen list; log receives progress lines
             frozen: dict | None = None, files: list | None = None, log=print) -> dict:
    # absolute repository path, so later commands work from any folder
    run_dir, repo = Path(run_dir), Path(repo).resolve()
    rels = files or discover(repo)  # the explicit file list, or every discovered code file
    # current text of each file, decoded as UTF-8 with universal newlines
    texts = {r: (repo / r).read_text(encoding="utf-8") for r in rels}
    # analyse all files in one batch (one pwsh and one node run)
    an = langs.analyze_many(list(texts.items()), frozen or {})
    bad = [r for r, a in an.items() if not a.ok]  # files the language's own parser rejected
    if bad:  # a baseline must parse, or no later identity could be trusted
        # name each unparsable file with its parser message
        raise loop.LoopError("baseline files that do not parse: " + ", ".join(f"{r} ({an[r].error})" for r in bad))
    # Seal the rubric only now that every baseline file parses, so a refused seal leaves the folder unsealed and the
    # command can simply be run again. loop.seal refuses to overwrite an existing seal: a run is sealed exactly once.
    sealed = loop.seal(Path(rubric) if rubric else DEFAULT_RUBRIC, run_dir)
    base = run_dir / "baseline"  # baseline copies live inside the run directory
    entries = []  # one manifest record per file
    for r in rels:  # copy each audited file into the baseline folder
        dst = base / r  # baseline path mirroring the repository layout
        dst.parent.mkdir(parents=True, exist_ok=True)  # create the intermediate folders as needed
        dst.write_bytes((repo / r).read_bytes())  # byte-for-byte copy, line endings and byte-order mark included
        cov, req, _ = an[r].coverage()  # how many lines need a comment and how many already have one
        # Record the language, the file's SHA-256, its code identity, the required and covered line counts and its
        # length in lines.
        entries.append({"path": r, "lang": an[r].lang, "sha256": _sha((repo / r).read_bytes()),
                        # identity, coverage counts and line total for this file
                        "identity": an[r].identity, "required": req, "covered": cov, "lines": len(an[r].lines)})
    (run_dir / "maps").mkdir(exist_ok=True)  # empty maps folder where authors write their comment maps
    # The manifest: schema tag, absolute repository path, seal time, rubric hash, test commands, frozen lines and
    # per-file records.
    man = {"schema": "emblem-rsi.code-manifest/1", "repo": str(repo), "sealed_at": sealed["sealed_at"],
           # test commands, frozen lines and the per-file records
           "rubric_sha256": sealed["sha256"], "tests": tests or [], "frozen": frozen or {}, "files": entries}
    # indented JSON with a final newline, written as UTF-8
    (run_dir / MANIFEST).write_text(json.dumps(man, indent=1) + "\n", encoding="utf-8")
    # progress message with the file and line totals
    log(f"sealed {len(rels)} files, {sum(e['required'] for e in entries)} required lines "
        # how many required lines were already explained at the seal
        f"({sum(e['covered'] for e in entries)} already explained)")
    return man  # the manifest, also handy for tests and other callers


# ------------------------------------------------------------------------------------------- comments in a file
# Split a file's comments into units for the quality metrics: consecutive comment lines (blank lines allowed between)
# form one unit, and each trailing comment is a unit of its own. an is the file's Analysis. Returns dicts with text,
# lines and target, where target is the code line the unit explains: the next non-blank line for a block, or the line
# itself for a trailing comment (that line still contains the comment text).
def comment_units(an: langs.Analysis) -> list[dict]:
    """Every comment in the file as a unit: consecutive comment lines form one unit; a trailing comment is one.
    Each unit records the code line it explains (the next code line for a block, its own line when trailing)."""
    units, cur = [], None  # finished units, and the block currently being collected
    lines = an.lines  # the analysed Line records of the whole file
    for i, ln in enumerate(lines):  # walk the lines with their 0-based index
        if ln.comment:  # a pure comment line extends or starts a block
            t = strip_comment(ln.text.strip(), ln.ctx_start)  # the comment text without markers or surrounding spaces
            if cur is None:  # no open block: this line starts a new one
                # text so far, lines it spans, and a target still unknown
                cur = {"text": t, "lines": [ln.n], "target": None}
            else:  # a block is already open and collecting lines
                cur["text"] += " " + t  # append this line's text to the open block
                cur["lines"].append(ln.n)  # record that the block also spans this line
            continue  # nothing else to do for a pure comment line
        if cur is not None:  # a non-comment line arrives while a block is open
            if not ln.blank:  # a non-blank line ends the block and is what it explains
                cur["target"] = ln.text  # the code line the block sits directly on top of
                units.append(cur)  # the block is complete and becomes a unit
                cur = None  # no block is open any more until the next comment
            elif i + 1 < len(lines) and not lines[i + 1].comment:  # a blank line inside or after a block keeps it open
                pass  # no effect: this branch is leftover and changes nothing
        if ln.trailing:  # code followed by its own comment on the same line
            # A trailing comment forms its own unit. Its target is the code before the comment, so the echo test
            # compares the comment with the code it explains and not with the comment's own words.
            units.append({"text": trailing_text(ln.text, ln.ctx_end), "lines": [ln.n],
                          "target": code_part(ln.text, ln.ctx_end)})  # the line with its trailing comment cut off
    if cur is not None:  # a block still open at the end of the file
        units.append(cur)  # keep it as a unit with no target line
    for u in units:  # normalise each unit's text before comparison
        u["text"] = re.sub(r"\s+", " ", u["text"]).strip()  # collapse runs of whitespace left by joined lines
    return units  # all comment units of the file in order


# Remove comment markers from a full comment line. s is the stripped line text and ctx the line's comment syntax.
# Returns the bare comment text.
def strip_comment(s: str, ctx: str) -> str:
    if ctx in ("css",) or s.startswith("/*"):  # CSS context, or any line that starts with a slash-star opener
        # drop opener and closer, then spaces and stray asterisks
        return s.removeprefix("/*").removesuffix("*/").strip(" *")
    if ctx in ("html", "xml") or s.startswith("<!--"):  # markup context, or a line that opens with an angle-bang marker
        # drop the markup comment markers and surrounding spaces
        return s.removeprefix("<!--").removesuffix("-->").strip()
    if ctx == "js" or s.startswith("//"):  # JavaScript context, or a line starting with two slashes
        return s.lstrip("/").strip()  # drop the leading slashes and any spaces after them
    if s.startswith("<#") or s.endswith("#>"):  # a line of a PowerShell block comment that opens or closes it
        return s.removeprefix("<#").removesuffix("#>").strip()  # drop the block comment markers and spaces
    return s.lstrip("#").strip()  # hash-style comments: drop leading hashes and spaces


# Extract the text of a trailing comment from a code line. s is the full line and ctx the comment syntax at its end.
# Returns the comment text, or an empty string when no marker is found. The last marker on the line wins, so a comment
# that itself contains the marker (a URL, a ' #' inside the text) loses its first part.
def trailing_text(s: str, ctx: str) -> str:
    s = s.rstrip()  # ignore trailing whitespace after the comment
    if ctx == "css":  # stylesheet line ending in a slash-star comment
        i = s.rfind("/*")  # position of the last comment opener on the line
        # text after the opener without the closer, or nothing
        return s[i + 2:].removesuffix("*/").strip() if i >= 0 else ""
    if ctx in ("html", "xml"):  # markup line ending in an angle-bang comment
        i = s.rfind("<!--")  # position of the last markup comment opener
        # text after the four-character opener without the closer
        return s[i + 4:].removesuffix("-->").strip() if i >= 0 else ""
    if ctx == "js":  # JavaScript line ending in a double-slash comment
        i = s.rfind("//")  # position of the last double slash on the line
        return s[i + 2:].strip() if i >= 0 else ""  # text after the slashes, trimmed of spaces
    i = s.rfind(" #")  # hash style: the last space-hash pair marks the comment start
    if i >= 0:  # a space-hash pair was found
        return s[i + 2:].strip()  # the text after it
    # with no space before '#', fall back to the first hash; with no hash at all there is no comment, so the
    # result is empty like the other branches (returning the whole line would treat code as comment text)
    return s.split("#", 1)[1].strip() if "#" in s else ""


# The code part of a line that ends in a trailing comment: the counterpart of trailing_text, cut at the same marker.
# s is the full line and ctx the comment syntax at its end. Returns the line without the comment and without the
# spaces before it; used as the echo target and to tell whether a comment made a line too long.
def code_part(s: str, ctx: str) -> str:
    s = s.rstrip()  # trailing whitespace after the comment plays no part
    # the comment opener for this context; hash-style languages (Python, PowerShell, TOML) have none in the table
    marker = {"css": "/*", "html": "<!--", "xml": "<!--", "js": "//"}.get(ctx)
    if marker:  # slash-star, angle-bang or double-slash comments
        i = s.rfind(marker)  # the same last opener trailing_text cuts at
        return s[:i].rstrip() if i >= 0 else s  # everything before it, or the whole line when there is none
    i = s.rfind(" #")  # hash style: the last space-hash pair, as in trailing_text
    # everything before it; with no space before '#', cut at the first hash instead
    return s[:i].rstrip() if i >= 0 else s.split("#", 1)[0].rstrip()


# Natural-language words of a comment, lower-cased: letter runs of at least two characters, apostrophes allowed. s is
# the text; returns a list used by is_echo.
def words(s: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z']+", s.lower())  # letter runs with apostrophes, case folded for comparison


# Every word a line of code mentions, lower-cased, including the parts of snake_case and CamelCase identifiers
# (HereStringLiteral gives here, string and literal). s is the code line; returns a set used by is_echo.
def code_words(s: str) -> set:
    out = set()  # collected words of the code line
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", s):  # each identifier-like token in the code
        out.add(tok.lower())  # the whole identifier in lower case
        # split on underscores, capitals and digit runs into parts
        for part in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", tok.replace("_", " ")):
            out.add(part.lower())  # add every part in lower case as well
    return out  # all words the code line could be said to contain


# Whether a comment unit only repeats the code: True when it has no content words, or three or fewer that all occur in
# its target line. unit is a dict from comment_units. Feeds the echo_ratio metric.
def is_echo(unit: dict) -> bool:
    # content words: not stop words and at least three letters long
    content = [w for w in words(unit["text"]) if w not in STOP and len(w) > 2]
    if not content:  # a comment made only of stop words says nothing
        return True  # counted as an echo of the code it follows
    # A short comment whose content words all appear in the code it explains adds nothing new. For a trailing comment
    # the target line contains the comment itself, so every short trailing comment counts as an echo.
    return len(content) <= 3 and set(content) <= code_words(unit.get("target") or "")


# Whether a comment is filler rather than an explanation. text is the comment; returns True for to-do style tags, a bare
# ellipsis, the lone word for comment, a not-applicable marker, and the stress-test and dummy Latin filler texts.
def is_placeholder(text: str) -> bool:
    t = text.strip().lower()  # trimmed and lower-cased for the whole-comment comparisons
    # To-do tags as whole upper-case words (so prose naming the --todo option is not flagged), or a comment that
    # consists only of an ellipsis, the bare word for comment, or a not-applicable marker.
    return bool(re.search(r"\b(TODO|FIXME|XXX|TBD)\b", text)) or t in ("...", "comment", "n/a", "na") or \
        "stress comment" in t or "lorem ipsum" in t  # texts left by stress tests and dummy Latin filler


# Forbidden strings found in a comment. text is the comment and rel the file's path (template rules apply only to
# template files). Returns the list of forbidden items found; an empty list means clean.
def forbidden_hits(text: str, rel: str) -> list[str]:
    # always-forbidden strings, compared without regard to case
    hits = [f for f in FORBIDDEN_ALWAYS if f.lower() in text.lower()]
    if is_template(rel):  # files inside a template folder get extra rules
        hits += [f for f in FORBIDDEN_TEMPLATE if f in text]  # project-specific strings, compared case-sensitively
        if cmap.TEMPLATE_MARKERS.search(text):  # placeholder or upper-case marker syntax in the text
            hits.append("template marker")  # reported as a generic template marker hit
    return hits  # every forbidden item found in this comment


# Count the lines that the comments made longer than WIDTH. base and cur are the baseline and current analyses of one
# file. A line counts when it is new or changed and over the width, unless it is a code line whose code alone was
# already over the width (a long line that only accepts a comment at its end is not the comment's doing).
def made_long(base: langs.Analysis, cur: langs.Analysis) -> int:
    seen = collections.Counter(ln.text for ln in base.lines)  # multiset of the baseline's line texts
    n = 0  # lines the comments pushed past the width
    for ln in cur.lines:  # every line of the commented file, in order
        if seen.get(ln.text, 0) > 0:  # an unchanged baseline line: consume one copy of it
            seen[ln.text] -= 1  # unchanged lines never count, however long they are
        elif len(ln.text) > WIDTH:  # a new or changed line beyond the width
            # the code before the trailing comment was already over the width on its own
            if ln.trailing and len(code_part(ln.text, ln.ctx_end)) > WIDTH:
                continue  # so the comment did not make it long
            n += 1  # an inserted comment line, or code that its trailing comment pushed over
    return n  # the count for the long_lines metric


# ------------------------------------------------------------------------------------------- measurement
# Analyse the current repository files and their baselines. run_dir is the run directory, man the manifest and rels an
# optional subset of paths. Returns (current texts, current analyses, baseline analyses), each keyed by path; a file
# missing from the repository is treated as empty.
def _analyse_repo(run_dir: Path, man: dict, rels: list[str] | None = None) -> tuple[dict, dict, dict]:
    repo = Path(man["repo"])  # repository root recorded in the manifest at seal time
    rels = rels or [e["path"] for e in man["files"]]  # the given subset, or every sealed file
    # current text, or an empty string when the file was deleted
    cur = {r: (repo / r).read_text(encoding="utf-8") if (repo / r).exists() else "" for r in rels}
    # the sealed baseline copies from the run directory
    base = {r: (run_dir / "baseline" / r).read_text(encoding="utf-8") for r in rels}
    # analysis of the current versions, frozen lines applied
    a_cur = langs.analyze_many(list(cur.items()), man.get("frozen") or {})
    # analysis of the baseline versions, for comparing comments
    a_base = langs.analyze_many(list(base.items()), man.get("frozen") or {})
    return cur, a_cur, a_base  # texts plus both analysis maps, keyed by path


# Quality metrics for the comments added since the baseline. rels are the paths, cur their current texts, a_cur and
# a_base their current and baseline analyses. A unit counts as added unless an identical text existed in the baseline.
# Returns detail_words, echo_ratio, duplicate_ratio, placeholder_count, forbidden_count, long_lines, the number of added
# comments and per-file details.
def quality(rels, cur, a_cur, a_base) -> dict:
    added_units, per_file = [], {}  # all added comment units, and per-file statistics
    for r in rels:  # compare each file's comments with its baseline
        # multiset of baseline comment texts, matched one for one
        base_units = collections.Counter(u["text"] for u in comment_units(a_base[r]))
        units = comment_units(a_cur[r])  # comment units of the current file
        added = []  # units that did not exist in the baseline version
        for u in units:  # check each current unit against the baseline multiset
            if base_units.get(u["text"], 0) > 0:  # an identical comment was already in the baseline
                base_units[u["text"]] -= 1  # use up one matching baseline copy of that text
            else:  # no baseline copy left: this unit is new
                added.append(u)  # record the new comment unit
        cnt = collections.Counter(u["text"] for u in added)  # how often each added text occurs in this file
        dups = sum(1 for u in added if cnt[u["text"]] >= 3)  # added units whose text appears three or more times
        # Per-file figures: units added, duplicates, echoes, up to five placeholder texts (80 characters each) and up to
        # five forbidden hits.
        per_file[r] = {"added": len(added), "duplicates": dups,
                       "echo": sum(1 for u in added if is_echo(u)),  # units that merely repeat the code's own words
                       # first five placeholder texts, trimmed to 80 characters
                       "placeholders": [u["text"][:80] for u in added if is_placeholder(u["text"])][:5],
                       # first five forbidden strings found in added units
                       "forbidden": [h for u in added for h in forbidden_hits(u["text"], r)][:5]}
        added_units += added  # pool the file's added units for the overall averages
    n_added = len(added_units)  # total number of added comment units
    long_lines = sum(made_long(a_base[r], a_cur[r]) for r in rels)  # lines a comment pushed past the width
    return {  # metrics and details for the measurement file
        # mean words per added unit, or None when nothing was added
        "detail_words": round(sum(len(u["text"].split()) for u in added_units) / n_added, 2) if n_added else None,
        # share of added units that echo the code
        "echo_ratio": round(sum(f["echo"] for f in per_file.values()) / n_added, 4) if n_added else 0.0,
        # share of added units repeated three or more times
        "duplicate_ratio": round(sum(f["duplicates"] for f in per_file.values()) / n_added, 4) if n_added else 0.0,
        # placeholder texts across all files, at most five counted per file
        "placeholder_count": sum(len(f["placeholders"]) for f in per_file.values()),
        # forbidden hits across all files, likewise capped at five per file
        "forbidden_count": sum(len(f["forbidden"]) for f in per_file.values()),
        # long new lines, total added units and per-file details
        "long_lines": long_lines, "added_comments": n_added, "per_file": per_file,
    }


# Run the manifest's test commands from the repository root. man is the manifest and log a print-like function. Returns
# (share of commands that passed, rounded to 4 places, list of result records with the command, outcome, seconds and the
# last six output lines). Starts each command as a child process with a fresh bytecode cache and no timeout.
def run_tests(man: dict, log=print) -> tuple[float, list]:
    if not man.get("tests"):  # a run sealed without any test commands
        return 1.0, []  # counts as fully passing, with no results to show
    import os  # imported here because only test runs need environment access
    import tempfile  # temporary folders for the bytecode caches
    res = []  # result records, one per test command
    for cmd in man["tests"]:  # each command is a list of arguments, run in order
        t0 = time.time()  # start time for the duration measurement
        with tempfile.TemporaryDirectory() as cache:  # a fresh, empty bytecode cache folder for this command
            # a fresh bytecode cache: a file changed within the same second at the same size must not run stale code
            env = {**os.environ, "PYTHONPYCACHEPREFIX": cache, "PYTHONDONTWRITEBYTECODE": "1"}
            # Run from the repository root, capturing output as UTF-8 text with bad bytes replaced. There is no timeout,
            # so a hanging suite would block the measurement.
            r = subprocess.run(cmd, cwd=man["repo"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=env)  # the environment with the fresh cache settings
        ok = r.returncode == 0  # exit code zero means every test in the command passed
        # last six lines of combined output, for the report
        tail = (r.stdout + "\n" + r.stderr).strip().splitlines()[-6:]
        # command (first 200 characters), outcome, seconds and output tail
        res.append({"cmd": " ".join(cmd)[:200], "ok": ok, "seconds": round(time.time() - t0, 1), "tail": tail})
        # progress line with outcome, command and duration
        log(f"tests: {'ok  ' if ok else 'FAIL'} {' '.join(cmd)[:90]} ({res[-1]['seconds']} s)")
    # share of passing commands, and every result record
    return round(sum(1 for x in res if x["ok"]) / len(res), 4), res


# Measure the repository against the baseline and write measure-NNN.json. run_dir is the run directory, note a free-text
# label, tests whether to run the test commands and log a print-like function. Computes per-file identity, parse result,
# coverage and comment quality, the rubric metrics, a page hash over all files and a probe hash of the measuring code.
# Returns the path of the new measurement file, which loop.decide then promotes or vetoes.
def measure(run_dir: Path, note: str = "", tests: bool = True, log=print) -> Path:
    run_dir = Path(run_dir)  # accept a str or a Path for the run directory
    sealed = loop.load_sealed(run_dir)  # the sealed rubric; refuses an edited or missing seal
    man = load_manifest(run_dir)  # the manifest with baseline identities and test commands
    rels = [e["path"] for e in man["files"]]  # every sealed file path, in manifest order
    by_path = {e["path"]: e for e in man["files"]}  # manifest records looked up by path
    cur, a_cur, a_base = _analyse_repo(run_dir, man)  # current texts and analyses, with baseline analyses
    files, req_t, cov_t = {}, 0, 0  # per-file results and running totals of required and covered lines
    for r in rels:  # measure each sealed file in turn
        a = a_cur[r]  # this file's current analysis record
        cov, req, miss = a.coverage()  # explained, required and uncovered line numbers
        same = a.ok and a.identity == by_path[r]["identity"]  # parses and has the same code identity as at the seal
        # Per-file record: language, parse result and message, identity check, line counts, the first 50 uncovered lines
        # and the file length.
        files[r] = {"lang": a.lang, "ok": a.ok, "error": a.error, "identity_ok": same, "required": req,
                    # required, covered, first fifty uncovered line numbers, length
                    "covered": cov, "uncovered": miss[:50], "lines": len(a.lines)}
        req_t += req  # add to the repository-wide required count
        cov_t += cov  # add to the repository-wide covered count
    q = quality(rels, cur, a_cur, a_base)  # comment quality across all sealed files
    for r, f in q.pop("per_file").items():  # move the per-file quality details into the file records
        files[r]["comments_added"] = f["added"]  # how many comment units this file gained
        # only the non-empty quality findings, as issues
        files[r]["issues"] = {k: v for k, v in f.items() if k != "added" and v}
    # run the tests unless disabled (None marks them as not measured)
    tp, tres = run_tests(man, log) if tests else (None, [])
    metrics = {  # the values the sealed rubric scores
        # share of files whose code is unchanged (a gate)
        "identity_ratio": round(sum(1 for f in files.values() if f["identity_ok"]) / len(files), 4),
        # share of files that still parse (a gate)
        "parse_ratio": round(sum(1 for f in files.values() if f["ok"]) / len(files), 4),
        "tests_pass": tp,  # share of test commands passing, or None when skipped
        # explained share of all required lines; 1.0 when none are required
        "coverage": round(cov_t / req_t, 4) if req_t else 1.0,
        # share of files with every required line explained
        "files_complete_ratio": round(sum(1 for f in files.values() if f["covered"] == f["required"]) / len(files), 4),
        # the comment quality metrics copied from quality()
        **{k: q[k] for k in ("detail_words", "echo_ratio", "duplicate_ratio", "placeholder_count",
                             "forbidden_count", "long_lines")},
    }
    # Page hash: one SHA-256 over every file's path and content hash, so any change anywhere gives a new value
    # (loop.decide vetoes a measurement identical to the last promoted one).
    page = _sha("\n".join(f"{r}:{_sha(cur[r].encode())}" for r in rels).encode())
    # Probe hash: a fingerprint of the measuring code itself (langs.py, cmap.py and measure.py), recorded with each
    # measurement.
    probe = _sha(b"".join(Path(__file__).with_name(n).read_bytes() for n in ("langs.py", "cmap.py", "measure.py")))
    n = len(list(run_dir.glob("measure-*.json"))) + 1  # next measurement number, from the count of existing files
    out = run_dir / f"measure-{n:03d}.json"  # zero-padded name such as measure-007.json
    # The measurement record in the loop's schema, with the rubric hash, time, page and probe hashes, the note, metrics,
    # totals, test results and per-file details.
    m = {"schema": loop.MEASURE_SCHEMA, "rubric_sha256": sealed["sha256"], "measured_at": loop.now(),
         # no audio signature in this loop; probe hash and note
         "page_sha256": page, "signature_sha256": None, "probe_sha256": probe, "note": note, "metrics": metrics,
         # totals across files: count, required, covered, added comments
         "totals": {"files": len(files), "required": req_t, "covered": cov_t, "added_comments": q["added_comments"]},
         "tests": tres, "files": files}  # test outcomes and the per-file records
    out.write_text(json.dumps(m, indent=1) + "\n", encoding="utf-8")  # indented UTF-8 JSON with a trailing newline
    # one-line summary printed to the console
    log(f"measured {len(files)} files: coverage {metrics['coverage']:.2%} ({cov_t}/{req_t}), identity "
        # identity share, parse share and test outcome
        f"{metrics['identity_ratio']:.0%}, parse {metrics['parse_ratio']:.0%}, tests {tp}")
    return out  # path of the new measurement file, for decide


# ------------------------------------------------------------------------------------------- maps: check, apply
# Apply the maps to the baselines in memory. run_dir is the run directory, man the manifest and rels the paths. Returns
# {path: {text, errors, origin, entries, base_analysis}}: the would-be file text, map and placement errors, the baseline
# line behind each output line, the entry count and the baseline analysis. Writes nothing.
def build(run_dir: Path, man: dict, rels: list[str]) -> dict:
    """Baseline + maps for each file, in memory: {rel: {text, errors, origin}}."""
    # baseline text of each requested file
    base = {r: (run_dir / "baseline" / r).read_text(encoding="utf-8") for r in rels}
    # baseline analyses carry the placement rules for each line
    a_base = langs.analyze_many(list(base.items()), man.get("frozen") or {})
    out = {}  # result records keyed by repository path
    for r in rels:  # process each requested file in turn
        # all map entries for this file and any syntax errors
        entries, errs = cmap.read_entries(cmap.map_files(run_dir / "maps", r))
        # insert the comments; template files get the stricter text rules
        text, origin, aerrs = cmap.apply(base[r], a_base[r], entries, template=is_template(r))
        # the would-be text, all errors and line origins
        out[r] = {"text": text, "errors": errs + aerrs, "origin": origin, "entries": len(entries),
                  "base_analysis": a_base[r]}  # entry count and the baseline analysis, for check()
    return out  # every file's in-memory build result


# Report what applying the maps would give, without writing anything. run_dir is the run directory and rels the files to
# check. Returns per file: entries, errors, parse result, identity check, required and covered counts, uncovered
# baseline lines with their text, forbidden hits, placeholder texts and the number of lines the comments pushed past
# 120 characters (lines whose code was already longer are not counted, as in the long_lines metric). `code check`
# prints this.
def check(run_dir: Path, rels: list[str]) -> dict:
    """What applying the maps would give, file by file: errors, uncovered baseline lines, identity, parse."""
    run_dir = Path(run_dir)  # normalise the run directory argument to a Path
    man = load_manifest(run_dir)  # sealed manifest; fails if the run was never sealed
    known = {e["path"]: e for e in man["files"]}  # manifest records keyed by repository path
    for r in rels:  # every requested file must be part of the sealed run
        if r not in known:  # a path outside the manifest has no baseline
            raise loop.LoopError(f"{r} is not in the sealed manifest")  # refuse with the offending path in the message
    built = build(run_dir, man, rels)  # baselines plus maps, assembled in memory
    # analyse the would-be texts exactly as a measurement would
    an = langs.analyze_many([(r, b["text"]) for r, b in built.items()], man.get("frozen") or {})
    res = {}  # per-file check results, keyed by path
    for r in rels:  # report each requested file in order
        a, b = an[r], built[r]  # the would-be analysis and the build record for this file
        cov, req, miss = a.coverage()  # coverage figures computed from the would-be text
        base_lines = b["base_analysis"].lines  # baseline lines, used to quote uncovered code
        unc = []  # uncovered lines in baseline numbering, with their text
        for out_line in miss:  # translate each uncovered output line back to the baseline
            bl = b["origin"][out_line - 1]  # baseline line number of that output line
            if bl:  # inserted comment lines have origin zero and are skipped
                # baseline number and the first 100 characters of its code
                unc.append((bl, base_lines[bl - 1].text.strip()[:100]))
        units = comment_units(a)  # all comment units of the would-be text, old and new
        # entry count, errors, parse result and parser message
        res[r] = {"entries": b["entries"], "errors": b["errors"], "ok": a.ok, "error": a.error,
                  # same code identity as sealed, required and covered counts
                  "identity_ok": a.ok and a.identity == known[r]["identity"], "required": req, "covered": cov,
                  # uncovered lines and forbidden hits across all comments
                  "uncovered": unc, "forbidden": [h for u in units for h in forbidden_hits(u["text"], r)],
                  # placeholder comments, first sixty characters each
                  "placeholders": [u["text"][:60] for u in units if is_placeholder(u["text"])],
                  # lines the comments pushed past the width (lines that were already long are not counted)
                  "long_lines": made_long(b["base_analysis"], a)}
    return res  # per-file results for the command-line printer


# Write baseline plus maps into the repository. run_dir is the run directory, rels the files (None means every file that
# has a map) and log a print-like function. The current files are first copied to snapshots/NNN with a list of them, so
# revert can undo the step; nothing is written if any file has map errors, fails to parse or would change its code.
# Returns the snapshot folder, or None when there are no files to apply (nothing is written then).
def apply(run_dir: Path, rels: list[str] | None = None, log=print) -> Path | None:
    """Write baseline + maps to the repository for these files (default: every file that has a map), after copying
    the current versions to RUN/snapshots/NNN so `revert` can restore them. Refuses files whose result has map errors
    or changed code."""
    run_dir = Path(run_dir)  # the run directory as a Path object
    man = load_manifest(run_dir)  # the manifest names the repository root
    repo = Path(man["repo"])  # repository folder where the commented files are written
    if rels is None:  # no explicit list: take every file that has a map
        # sealed files with at least one map file
        rels = [e["path"] for e in man["files"] if cmap.map_files(run_dir / "maps", e["path"])]
    res = check(run_dir, rels)  # dry run first: errors, parsing and identity
    # files with map errors, a parse failure or changed code
    bad = {r: v for r, v in res.items() if v["errors"] or not v["ok"] or not v["identity_ok"]}
    if bad:  # any bad file stops the whole apply
        raise loop.LoopError("not applied; fix these first:\n" + "\n".join(  # list up to three problems per file
            # the file path followed by its errors, its parser message or "code changed"
            f"  {r}: " + "; ".join((v["errors"] or [v["error"] or "code changed"])[:3]) for r, v in bad.items()))
    if not rels:  # nothing to write (no file has a map, or an empty list was given)
        log("no files to apply; nothing written and no snapshot taken")  # say so instead of an empty snapshot
        return None  # no snapshot, so a later revert still undoes the previous real apply
    built = build(run_dir, man, rels)  # build again to obtain the texts to write
    snaps = run_dir / "snapshots"  # folder holding numbered snapshots of replaced files
    # next snapshot number, counting the existing numbered folders
    k = len([p for p in snaps.glob("[0-9]*")]) + 1 if snaps.exists() else 1
    snap = snaps / f"{k:03d}"  # zero-padded snapshot folder such as 003
    for r in rels:  # snapshot the current copy and then write each file
        dst = snap / r  # snapshot copy path mirroring the repository layout
        dst.parent.mkdir(parents=True, exist_ok=True)  # create the intermediate snapshot folders
        shutil.copy2(repo / r, dst)  # keep the current version, timestamps included
        text = built[r]["text"]  # the commented text produced by the maps
        # Write UTF-8 with newline="" so the LF endings of the built text are written exactly. Note: read_text
        # normalised the baseline to LF, so a file that used Windows CRLF endings comes back with LF endings.
        with open(repo / r, "w", encoding="utf-8", newline="") as f:
            f.write(text)  # replace the repository file with the commented version
    (snap / "files.json").write_text(json.dumps(rels), encoding="utf-8")  # list of replaced files, read back by revert
    # progress line with the file count and snapshot name
    log(f"applied maps to {len(rels)} files (snapshot {snap.name})")
    return snap  # the snapshot folder that revert would restore


# Undo the last apply. run_dir is the run directory and log a print-like function. Copies every file of the newest
# snapshot back into the repository and deletes that snapshot, so repeated reverts walk further back. Returns the
# snapshot's name; raises loop.LoopError when there is nothing to revert. Only files that apply replaced are restored;
# other hand edits stay as they are.
def revert(run_dir: Path, log=print) -> str:
    run_dir = Path(run_dir)  # work with a Path whatever the caller passed
    man = load_manifest(run_dir)  # the manifest gives the repository root
    # numbered snapshots in order, or none at all
    snaps = sorted((run_dir / "snapshots").glob("[0-9]*")) if (run_dir / "snapshots").exists() else []
    if not snaps:  # no apply has been made, or all were reverted
        raise loop.LoopError("nothing to revert")  # nothing to restore, so stop with an error
    snap = snaps[-1]  # the newest snapshot (zero-padded names sort numerically)
    # the files that apply replaced, from files.json
    rels = json.loads((snap / "files.json").read_text(encoding="utf-8"))
    for r in rels:  # restore each replaced file in turn
        shutil.copy2(snap / r, Path(man["repo"]) / r)  # copy the saved version back over the repository file
    shutil.rmtree(snap)  # delete the snapshot so the next revert goes one further back
    log(f"reverted {len(rels)} files from snapshot {snap.name}")  # progress line with the count and snapshot name
    return snap.name  # the restored snapshot's folder name, for messages
