# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""book.md -> display blocks + word tokens + narration segments (work/docmodel.json, work/request.json).

Each display token keeps the exact book text (`disp`); `spoken` is what the voice is given for it:
quotes stripped, dashes turned into a comma pause, scripture references expanded ("Psalm 23:1" ->
"Psalm 23 verse 1"), long ALL-CAPS words read as words, and reviewed pronunciation overrides applied
(work/pronunciations.json: {"overrides": {word: respelling}, "tokens": {token id: respelling}}; a
respelling may be Piper raw phonemes "[[...]]"). `spoken_ref` is the spoken form before overrides: what a
listener should hear, used by the pronunciation audit.

Narration segments are runs of tokens: one per block, except that consecutive verse lines are joined
until a sentence ends.

Reading rules for reports (project.json "spoken", all off by default):
    say             per-word rules, applied first and in order: ["regex", "spoken"] or {"match": regex, "say":
                    template, "prev": regex for the word before, "next": regex for the word after, "next2": regex for
                    the word after that, "spell_digits": true to read every digit of the result on its own}. The
                    regex runs on the displayed word as
                    printed (quotes and brackets included); a template may use \\1 for groups; "" makes the word
                    silent. When any rule changed a word, the result is final (no other spoken-form rule applies).
    silent          exact phrases shown but not spoken, e.g. "(Bible Hub)"; punctuation right after a phrase moves to
                    the word before it, so the sentence still ends where it should
    silent_columns  table columns, named by their header text, shown but not spoken (header cell included)
    read_links      LINK blocks are spoken only when true; code blocks are never spoken
    code_names      names from source code in the prose (snake_case, dotted.names, paths, --options, camelCase,
                    Verb-Noun) are read the way a programmer says them (text/codenames.py); say rules still win
    dates           "dmy" or "mdy": 03/10/2024 and 2026-03-14 are read as dates ("October 3rd, 2024")
    ordinal_days    "October 30" and "Nov 10 or 11" are read "October 30th" and "November 10th or 11th"
    years           1900-2099 are read the American way: "20 26" (twenty twenty-six), "19 60", "two thousand 2"
    bracket_pause   a short pause before and after a remark in brackets
    table_reading   "cells" (the header row read, then each cell as a sentence) or "rows": the header row is shown but
                    not read, and each row is one sentence with its column names: "CNPJ: 5 7, ..." in a two-column
                    table, "Stripe. Role: Payment processor. Evidence: Terms section 6." in a wider one
"""
from __future__ import annotations  # lets the tuple[dict, dict] style hints work without being evaluated at import

import hashlib  # SHA-256 of book.md, which tells a current prevoice fixes file from a stale one
import json  # reads pronunciations.json and writes docmodel.json and request.json
import re  # pattern matching for punctuation, scripture references and override lookups

from . import codenames, markup  # the book.md parser and validator (speechkit_narrate/text/markup.py)

# Characters peeled off both ends of a display word before it is spoken: curly and straight double quotes,
# curly single quotes and round brackets. The voice should not try to pronounce them.
QUOTES = '“”‘’"()'  # stripped with str.strip, so only the ends of a token are affected here
# Lower-case names of Bible books. A "23:1" token is only read as "23 verse 1" when the word right before it is
# one of these (scripture_refs "auto"), so clock times such as "10:30" stay as they are.
BIBLE_BOOKS = {  # a set gives constant-time membership tests for every number token
    "genesis", "exodus", "leviticus", "numbers", "deuteronomy", "joshua", "judges", "ruth", "samuel", "kings",
    "chronicles", "ezra", "nehemiah", "esther", "job", "psalm", "psalms", "proverbs", "ecclesiastes", "song",
    "isaiah", "jeremiah", "lamentations", "ezekiel", "daniel", "hosea", "joel", "amos", "obadiah", "jonah",
    "micah", "nahum", "habakkuk", "zephaniah", "haggai", "zechariah", "malachi", "matthew", "mark", "luke",
    "john", "acts", "romans", "corinthians", "galatians", "ephesians", "philippians", "colossians",
    "thessalonians", "timothy", "titus", "philemon", "hebrews", "james", "peter", "jude", "revelation",
}
# Books that exist as First/Second (and for John, Third) volumes, written in title case as they appear in text.
# "1 John" is read "First John" only when the next word is one of these
NUMBERED_BOOKS = ("Samuel", "Kings", "Chronicles", "Corinthians", "Thessalonians", "Timothy", "Peter", "John")
# the digit token that may precede a numbered book, mapped to the spoken ordinal
ORDINAL = {"1": "First", "2": "Second", "3": "Third"}
# Month names in order, for reading numeric dates; MONTHS maps full and short names (lower case) to 1..12.
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December"]
MONTHS = {m.lower(): i + 1 for i, m in enumerate(MONTH_NAMES)}  # "january" -> 1 ... "december" -> 12
MONTHS.update({m.lower()[:3]: i + 1 for i, m in enumerate(MONTH_NAMES)})  # "jan" -> 1 ... "dec" -> 12
MONTHS["sept"] = 9  # the four-letter short form of September
# A display word that is only a pause mark: never spoken; a comma pause goes on the word before it instead.
PAUSE_MARKS = ("—", "–", "/", "…")
# A free-standing hyphen is silent too, but without the pause (books set "word - word" loosely; a comma there would
# change the narration of editions already made).
SILENT_MARKS = PAUSE_MARKS + ("-",)
# A section number at the start of a heading ("2.1", "8.10", "11."): the voice pauses after it.
HEADING_NUMBER = re.compile(r"\d+(\.\d+)*\.?")


# A day of the month with its English ordinal suffix: 1 -> "1st", 2 -> "2nd", 11 -> "11th", 23 -> "23rd".
def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:  # 10th to 20th (11th, 12th, 13th included) always take "th"
        return f"{n}th"  # e.g. "12th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"  # 1st, 22nd, 23rd, 30th ...


# A year as an American reader says it, written so the voice reads it that way: 2026 -> "20 26" (twenty twenty-six),
# 2002 -> "two thousand 2", 1960 -> "19 60", 1906 -> "19 oh 6", 1900 -> "19 hundred". Other numbers come back as is.
def _year(y: int) -> str:
    if 2000 <= y <= 2009:  # the first decade of 2000 is read "two thousand (and) N"
        return "two thousand" + (f" {y - 2000}" if y > 2000 else "")  # 2000 -> "two thousand"
    if 1900 <= y <= 2099:  # every other year from 1900 to 2099 is read as two pairs
        hi, lo = divmod(y, 100)  # 2026 -> (20, 26)
        return f"{hi} hundred" if lo == 0 else (f"{hi} oh {lo}" if lo < 10 else f"{hi} {lo}")  # pairs
    return str(y)  # outside the range: unchanged


# Compile project.json "spoken.say" into rule dicts with compiled patterns.
# rules is the list from project.json: ["regex", "spoken"] pairs or {"match", "say", "prev", "next", "next2",
# "spell_digits"}. Returns [{"match", "say", "prev", "next", "next2", "spell"}]; the conditions are compiled patterns
# or None.
def compile_say(rules) -> list:
    out = []  # compiled rules in the order given
    for r in rules or []:  # an empty or missing list gives no rules
        if isinstance(r, (list, tuple)):  # the short form ["regex", "spoken"]
            r = {"match": r[0], "say": r[1]}  # same meaning as the long form
        out.append({"match": re.compile(r["match"]), "say": r.get("say", ""),  # what to find and what to say
                    "prev": re.compile(r["prev"]) if r.get("prev") else None,  # condition on the word before
                    "next": re.compile(r["next"]) if r.get("next") else None,  # condition on the word after
                    # condition on the word after that ("PDF 3, 35" is a list of pages, "PDF 3, right" is one page)
                    "next2": re.compile(r["next2"]) if r.get("next2") else None,
                    "spell": bool(r.get("spell_digits"))})  # read each digit of the result on its own
    return out  # ready for spoken_form(..., rules=)


# Apply the compiled say rules to one displayed word. Returns (spoken, changed): changed is True when any rule
# matched; the result then has quotes and brackets removed and single spaces ("" means silent). next2 is the word two
# places after this one, or None.
def _apply_say(tok: str, prev_disp, next_disp, rules: list, next2=None) -> tuple[str, bool]:
    s, changed = tok, False  # the word as printed, and whether any rule touched it
    for r in rules:  # in order, each rule working on the result of the ones before
        if r["prev"] and not r["prev"].search(prev_disp or ""):  # the word before does not fit the rule
            continue  # rule skipped
        if r["next"] and not r["next"].search(next_disp or ""):  # the word after does not fit the rule
            continue  # rule skipped
        if r["next2"] and not r["next2"].search(next2 or ""):  # nor does the word after that
            continue  # rule skipped
        if r["spell"]:  # the template's result is read digit by digit: "57" -> "5 7"
            new = r["match"].sub(lambda m, r=r: re.sub(r"(?<=\d)(?=\d)", " ", m.expand(r["say"])), s)  # space digits
        else:  # an ordinary replacement template
            new = r["match"].sub(r["say"], s)  # \1-style groups are filled in
        if new != s:  # the rule matched and changed the word
            s, changed = new, True  # keep the change
    if not changed:  # no rule applied
        return tok, False  # the caller continues with the ordinary spoken-form rules
    s = re.sub(r'[“”‘"()\[\]]', "", s)  # quotes and brackets are never spoken
    s = re.sub(r"(?<![A-Za-zÀ-ÿ0-9])’|’(?![A-Za-zÀ-ÿ])", "", s)  # a ’ stays between a letter/digit and a letter
    s = re.sub(r",(?=[.,;:!?])", "", s)  # a rule's closing comma before printed punctuation: "4 6,)," -> "4 6,"
    return re.sub(r"\s+", " ", s.replace("’", "'")).strip(), True  # ASCII apostrophes, single spaces


# Read a numeric date as words: "03/10/2024" with order "dmy" -> "October 3rd, 2024"; ISO "2026-03-14" ->
# "March 14th, 2026". Returns None when s is not a valid date in that form. The year goes through _year when
# years is True; trailing punctuation after the date is kept.
def _numeric_date(s: str, order: str, years: bool):
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})([.,;:!?]*)", s)  # ISO: year-month-day
    if m:  # an ISO date
        y, mo, d, tail = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)  # its parts
    else:  # a day/month/year or month/day/year date
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})([.,;:!?]*)", s)  # two numbers and a year, slash-separated
        if not m or order not in ("dmy", "mdy"):  # not a date, or numeric dates are not to be read
            return None  # not handled
        a, b, y, tail = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)  # its parts
        d, mo = (a, b) if order == "dmy" else (b, a)  # which number is the day depends on the book's convention
    if not (1 <= mo <= 12 and 1 <= d <= 31):  # not a real month or day
        return None  # left alone
    return f"{MONTH_NAMES[mo - 1]} {_ordinal(d)}, {_year(y) if years else y}{tail}"  # "October 3rd, 20 24"


# Positions of the words in one cell that are days of a month: a number from 1 to 31 right after a month name
# ("October 30", "Nov 10"), or after "or", "and", "to" or a dash that follows such a day ("Nov 10 or 11").
# words is the cell's display words; returns a set of indexes into words.
def _day_positions(words: list) -> set:
    days = set()  # the indexes found so far
    for i, w in enumerate(words):  # each word with its index
        if not re.fullmatch(r"\(?\d{1,2}[.,;:!?)]*", w) or not 1 <= int(re.sub(r"\D", "", w)) <= 31:  # not 1..31
            continue  # only small numbers can be days
        prev = re.sub(r"[^A-Za-z]", "", words[i - 1]) if i else ""  # the word before, letters only
        if i and words[i - 1][:1].isupper() and prev.lower() in MONTHS:  # a capitalised month name ("May" not "may")
            days.add(i)  # "October 30"
        elif i >= 2 and prev.lower() in ("or", "and", "to") and i - 2 in days:  # "10 or 11", "26 and 27"
            days.add(i)  # the second day of a pair
        elif i >= 2 and words[i - 1] in ("-", "–", "—") and i - 2 in days:  # "10 – 11" with a free-standing dash
            days.add(i)
    return days  # positions to read as ordinals


# How one cell of a table row is read when the row is read as a sentence (spoken.table_reading "rows").
# k is the cell's place among the row's spoken columns, n how many spoken columns the table has, heads the spoken
# header text of those columns, second the printed text of the row's second spoken cell, num_col True when the first
# column only numbers the rows ("#"). Returns (end mark, spoken lead-in): two columns read "CNPJ: 5 7 ...", wider
# tables "Stripe. Role: Payment processor. Evidence: Terms section 6.", numbered ones "Finding 1: No operator ...".
def _row_cell(k: int, n: int, heads: list, second: str, num_col: bool) -> tuple[str, str]:
    bare_number = n == 2 and bool(re.fullmatch(r"[\d\s.,–-]+", second.strip()))  # "24": needs its column's name
    paired = (n == 2 and not bare_number) or num_col  # the label leads straight into the second cell
    if k == 0:  # the row's label
        lead = f"{heads[1]} " if num_col and n > 1 and heads[1] else ""  # "Finding" before the row number
        return (":" if paired else "."), lead  # "CNPJ:" or "Stripe."
    if k == 1 and paired:  # the value the label introduced
        return ".", ""  # no column name needed
    return ".", (f"{heads[k]}: " if k < len(heads) and heads[k] else "")  # "Evidence: Terms section 6."


# Load the reviewed pronunciation fixes for this book from work/pronunciations.json.
# path is a pathlib.Path (or str) to that file; a missing file simply means "no overrides yet".
# Returns (words, tokens): words maps a lower-case word to its respelling, tokens maps an int token id to a respelling.
# Read-only; build() calls it before tokenising so the overrides land in the spoken forms.
def load_overrides(path) -> tuple[dict, dict]:
    try:  # the file is optional, so its absence is handled below
        # UTF-8 explicitly, because Windows would otherwise default to a legacy code page
        with open(path, encoding="utf-8") as f:
            ov = json.load(f)  # parse the JSON object with "overrides" and "tokens" sections
    except FileNotFoundError:  # no pronunciation review has happened yet for this project
        return {}, {}  # two empty maps make apply_override a no-op
    # word keys are lower-cased so lookups ignore capitalisation in the text
    return ({k.lower(): v for k, v in ov.get("overrides", {}).items()},
            # JSON object keys are always strings, so token ids are turned back into ints to match token["i"]
            {int(k): v for k, v in ov.get("tokens", {}).items()})


# Where the prevoice stage (speechkit_narrate/audit/prevoice.py) writes its per-token fixes, relative to the project folder.
PREVOICE_FIXES = "work/prevoice/fixes.json"


# Load the prevoice stage's per-token fixes, the lowest-priority layer of pronunciation fixes. project is the Project.
# Returns {token id: (word, respelling)}, or {} when project.json prevoice.enabled is off (the default), when there is
# no fixes file, or when the file was made from another version of work/book.md (its book_sha256 differs), because
# token ids may have moved; the prevoice stage then runs again and rebuilds the script. Read-only.
def load_prevoice(project) -> dict:
    if not (project.cfg.get("prevoice") or {}).get("enabled"):  # opt-in: off means the script never reads the file
        return {}  # so a book that sets nothing is built exactly as before
    try:  # the file is made by the prevoice stage, so it may not exist yet
        data = json.loads((project.root / PREVOICE_FIXES).read_text(encoding="utf-8"))  # UTF-8 for the IPA
    except (FileNotFoundError, json.JSONDecodeError):  # never made, or cut short
        return {}  # no prevoice fixes
    if data.get("book_sha256") != hashlib.sha256(project.p("book").read_bytes()).hexdigest():  # another book.md
        return {}  # stale: the stage recomputes them after this script build
    # token id -> (the word the fix is for, what the voice should say instead)
    return {int(k): (v["word"], v["say"]) for k, v in (data.get("tokens") or {}).items()}


# Replace one spoken word with its reviewed respelling, if a fix exists for it.
# s is the spoken form (str), token_id the global token index (int), words and tokens the two maps from
# load_overrides(); low is load_prevoice()'s map, used only when neither of those fixes this word.
# Returns the respelled word with the original trailing punctuation re-attached, or s unchanged when no fix applies.
# A per-token fix wins over a per-word fix, so one occurrence of a name can be corrected without touching the rest.
def apply_override(s: str, token_id, words: dict, tokens: dict, low: dict | None = None) -> str:
    # group 1 is the bare word (letters, apostrophes, hyphens); group 2 the trailing punctuation
    m = re.fullmatch(r"([A-Za-zÀ-ÿ'’-]+)([.,;:!?]*)", s)
    if not m:  # numbers, "23 verse 1" and anything else that is not a plain word
        return s  # returned exactly as spoken: pronunciation fixes only ever apply to plain words
    if token_id in tokens:  # a fix aimed at this exact occurrence
        rep = tokens[token_id]  # takes priority over the word-wide rule
    elif m.group(1).lower() in words:  # otherwise look the word up case-insensitively
        rep = words[m.group(1).lower()]  # a fix that applies to every occurrence of this word
    # lowest priority: a prevoice fix for this token, used only while the token still holds the word it was made for
    elif low and token_id in low and low[token_id][0] == m.group(1).lower().replace("’", "'"):
        rep = low[token_id][1]  # hand and loop fixes above always win over it
    else:  # no fix for this word at all
        return s  # leave the spoken text untouched
    if m.group(1)[:1].isupper() and not rep.startswith("[["):   # [[...]] = Piper raw phonemes, used as is
        # keep a capital at the start (a sentence opening or a name) in the respelling as well
        rep = rep[:1].upper() + rep[1:]
    return rep + m.group(2)  # trailing punctuation is restored so the voice keeps its pauses and intonation


# Letters-only skeleton of a word, used to compare words while ignoring punctuation and digits.
# w is a str or None (None stands for "no neighbouring word"); returns a str of ASCII letters, possibly empty.
def _bare(w):
    return re.sub(r"[^A-Za-z]", "", w or "")  # None becomes "" so callers can pass a missing neighbour safely


# Work out what the voice should say for one display word of the book.
# tok is the display token (str); prev_disp and next_disp are the neighbouring display tokens in the same cell, or None.
# opts is project.json "spoken" (scripture_refs, lowercase_allcaps_min). Returns the spoken str, where "" means silent.
# Pure function; tokenize() calls it once per word and then adds pause and table punctuation around the result.
# rules are compile_say() results (compiled from opts["say"] when None); day is True when tokenize() found this word to
# be a day of a month ("30" in "October 30").
def spoken_form(tok: str, prev_disp, next_disp, opts: dict, rules=None, day: bool = False, next2=None) -> str:
    """Spoken string for one display token ('' = silent)."""
    if tok in SILENT_MARKS:  # a free-standing dash, hyphen, slash or ellipsis
        return ""  # is not spoken; tokenize() turns most of them into a comma pause on the previous word
    if rules is None:  # a direct caller (tests): compile the project's rules here
        rules = compile_say(opts.get("say"))  # an empty list when there are none
    said, changed = _apply_say(tok, prev_disp, next_disp, rules, next2)  # the project's own reading rules come first
    if changed:  # a rule decided how this word is read
        return said  # final: no other rule applies to it
    if opts.get("code_names"):  # names from source code read the way a programmer says them (text/codenames.py)
        code = codenames.spoken(tok)
        if code is not None:  # the word names code: its reading, with the surrounding quotes and punctuation kept
            return code
    s = tok.strip(QUOTES)  # peel quotes and brackets off both ends of the word
    bare = re.sub(r'[“”‘’"()\[\]]', "", s)  # no brackets or quotes at all: "(Apr)," -> "Apr,", "2026)." -> "2026."
    core, tail = re.fullmatch(r"(.*?)([.,;:!?]*)", bare).groups()  # the word and its trailing punctuation
    # a numeric date read as words, when the project says how its numeric dates are written; None otherwise
    nd = _numeric_date(bare, opts.get("dates", ""), bool(opts.get("years"))) if opts.get("dates") else None
    if nd:  # 03/10/2024 or 2026-03-14, read as a date
        return nd  # e.g. "October 3rd, 20 24"
    if day and core.isdigit():  # a day of a month ("October 30"): read as an ordinal
        return _ordinal(int(core)) + tail  # "30th,"
    if opts.get("years") and re.fullmatch(r"(19|20)\d\d", core):  # a year from 1900 to 2099
        return _year(int(core)) + tail  # "20 26."
    # a capitalised month name of up to four letters ("Nov", "Sept.", "(Apr),"), when dates are read naturally
    if opts.get("ordinal_days") and core[:1].isupper() and core.lower().rstrip(".") in MONTHS \
            and len(core.rstrip(".")) <= 4:  # a short month name ("Nov", "Sept.") when dates are read naturally
        return MONTH_NAMES[MONTHS[core.lower().rstrip(".")] - 1] + tail  # read in full: "November"
    # punctuation stuck behind a closing quote, e.g. life”). -> life.  An apostrophe between letters stays
    # (I’ll, we’ll, don’t): dropping it made the voice say "ill", "well", "dont".
    s = re.sub(r'[“”‘"()]', "", s)  # then drop any double quotes or brackets left inside, e.g. life”). becomes life.
    # a ’ is removed unless a letter or digit comes before it and a letter after it, so "don’t" and "chapter 15’s"
    # keep their apostrophe ("15s" was read "fifteens")
    s = re.sub(r"(?<![A-Za-zÀ-ÿ0-9])’|’(?![A-Za-zÀ-ÿ])", "", s)
    # a chapter:verse pair such as "23:1", with optional trailing punctuation
    m = re.fullmatch(r"(\d+):(\d+)([.,;:!?]*)", s)
    sr = opts.get("scripture_refs", "auto")  # "auto" (expand after a Bible book name), True (always) or False (never)
    # expand when forced on, or in auto mode when the previous word is a Bible book
    if m and (sr is True or (sr == "auto" and _bare(prev_disp).lower() in BIBLE_BOOKS)):
        # "23 verse 1" is what a reader says aloud; the punctuation is kept for pacing
        return f"{m.group(1)} verse {m.group(2)}{m.group(3)}"
    # a lone "1", "2" or "3" right before a numbered book name such as "John"
    if s in ORDINAL and next_disp and _bare(next_disp) in NUMBERED_BOOKS:
        return ORDINAL[s]  # becomes "First", "Second" or "Third", as the title is read aloud
    # minimum letter count for treating an ALL-CAPS word as a normal word; 0 or null disables it
    n = int(opts.get("lowercase_allcaps_min", 5) or 0)
    letters = _bare(s)  # letters only, so "LORD," is judged on its four letters
    # a long all-capitals word (emphasis or a title) would otherwise be spelled letter by letter
    if n and len(letters) >= n and letters.isupper():
        s = s.lower()  # lower-casing makes the voice read it as an ordinary word
    elif s.startswith("NOT") and _bare(s) == "NOT":      # emphasis "NOT" is a word, not N-O-T
        s = s.lower()  # the shorter emphatic "NOT" is also read as a word rather than N-O-T
    return s.replace("’", "'")  # the voice's phonemiser expects ASCII apostrophes, so the curly one is swapped


# Split every block into display word tokens and attach the token ids to the blocks.
# blocks are markup.parse() dicts (mutated in place), opts is project.json "spoken", and words_ov/tokens_ov come from
# load_overrides(); low (optional) is load_prevoice()'s lowest-priority layer.
# Returns the flat list of token dicts {i, disp, spoken, spoken_ref, block, cell}; each block gains "cell_tokens" and
# "tokens".
# The token ids are the shared index that the narration timings, the pronunciation audit and the export refer to.
def tokenize(blocks: list, opts: dict, words_ov: dict, tokens_ov: dict, low: dict | None = None) -> list:
    """Attach token lists to blocks; returns the flat token list."""
    flat = []  # one entry per word in the whole book, in reading order
    rules = compile_say(opts.get("say"))  # the project's reading rules, compiled once for the whole book
    phrases = [p for p in (opts.get("silent") or []) if p.strip()]  # phrases shown but not spoken
    silent_cols = {c.strip().lower() for c in (opts.get("silent_columns") or [])}  # column headers, lower case
    mute = set()  # cell positions of the current table whose column is silent
    by_rows = opts.get("table_reading") == "rows"  # rows read as sentences, the header row shown but not read
    live, heads, num_col = [], [], False  # current table: spoken columns, their spoken header text, a "#" column
    # a cross-reference ending a block right after a finished sentence: shown, not spoken
    trail = re.compile(r"(?<=[.!?”)])\s+(" + opts["silent_trailing"] + r")\s*$") if opts.get("silent_trailing") \
        else None  # off unless the project sets a pattern
    for bi, b in enumerate(blocks):  # bi is the block index, stored on every token
        if b["kind"] == "code":  # a command block is shown line by line and never spoken: it has no word tokens
            b["cell_tokens"], b["tokens"] = [], []  # renderers show its text as it is
            continue  # next block
        # a table row is tokenised cell by cell; any other block is treated as a single cell
        cells = b["cells"] if b["kind"] in ("th", "tr") else [b["text"]]
        if b["kind"] == "th":  # a new table: which of its columns are silent
            mute = {ci for ci, c in enumerate(cells) if c.strip().lower() in silent_cols}  # by header text
            live = [ci for ci in range(len(cells)) if ci not in mute]  # the columns that are spoken
            heads = ["" for _ in live]  # their header text as spoken, filled in as the header cells are read
            num_col = bool(live) and cells[live[0]].strip() in ("#", "No.", "No", "Nº", "Number")  # numbering only
        b["cell_tokens"] = []  # list of token-id lists, one per cell
        for ci, cell in enumerate(cells):  # ci is the cell index within the row (always 0 for prose blocks)
            spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", cell)]  # each display word's place in the cell
            words = [cell[a:z] for a, z in spans]  # whitespace split: each display word keeps its exact punctuation
            days = _day_positions(words) if opts.get("ordinal_days") else set()  # "October 30": 30 is a day
            quiet = set()  # positions of words inside a silent phrase
            carry = {}  # position of the last spoken word before a silent phrase -> punctuation to give it
            for p in phrases:  # every silent phrase, wherever it occurs in this cell
                for m in re.finditer(re.escape(p), cell):  # each occurrence
                    inside = [k for k, (a, z) in enumerate(spans)  # words whose text (minus trailing punctuation)
                              if a >= m.start() and a + len(words[k].rstrip(".,;:!?")) <= m.end()]  # lies in it
                    quiet.update(inside)  # are not spoken
                    if inside:  # punctuation printed right after the phrase still ends the clause or sentence
                        tail = re.search(r"[.,;:!?]+$", words[inside[-1]])  # e.g. the "." of "(Bible Hub)."
                        if tail and inside[0] > 0:  # there is a word before the phrase to carry it
                            carry[inside[0] - 1] = tail.group(0)  # given to that word below
            m = trail.search(cell) if trail and b["kind"] not in ("th", "tr") else None  # "... PDF. Section 8"
            if m:  # the block ends with a cross-reference after a finished sentence
                quiet.update(k for k, (a, z) in enumerate(spans) if a >= m.start(1))  # its words are not spoken
            ids = []  # ids of the tokens made from this cell
            for wi, w in enumerate(words):  # wi is the word position inside the cell
                # neighbours are taken from this cell only, so a reference cannot match across table cells
                sp = spoken_form(w, words[wi - 1] if wi else None, words[wi + 1] if wi + 1 < len(words) else None,
                                 opts, rules=rules, day=wi in days,  # the spoken.* options from project.json
                                 next2=words[wi + 2] if wi + 2 < len(words) else None)  # two words ahead
                # inside a silent phrase, in a silent table column, or a source title that is not read
                if (b["kind"] == "cover" and not opts.get("read_cover", True)) \
                        or wi in quiet or (b["kind"] in ("th", "tr") and ci in mute) \
                        or (b["kind"] == "link" and not opts.get("read_links")):  # shown but not spoken
                    sp = ""  # silent: the word is displayed and highlighted with its neighbours, never voiced
                # i is the global token id, equal to the token's position in flat
                t = {"i": len(flat), "disp": w, "spoken": sp, "block": bi, "cell": ci}
                flat.append(t)  # appended before its id is recorded, so the id and the list position agree
                ids.append(t["i"])  # remember the id for this cell
            for k, punct in carry.items():  # the word before each silent phrase takes the phrase's end punctuation
                t = flat[ids[k]]  # that word's token
                if t["spoken"] and not re.search(r"[.,;:!?]$", t["spoken"]):  # spoken, with no punctuation yet
                    t["spoken"] += punct[-1]  # "...a gift (Bible Hub)." -> "...a gift."
            if opts.get("bracket_pause"):  # a remark in brackets is set off by short pauses, as a reader would
                for k, i in enumerate(ids):  # each word of the cell
                    t, nxt = flat[i], (flat[ids[k + 1]] if k + 1 < len(ids) else None)  # this word and the next one
                    if k and t["disp"].startswith("(") and t["spoken"]:  # a spoken remark opens here
                        j = ids[k - 1]  # the word before the bracket
                        if flat[j]["spoken"] and not re.search(r"[.,;:!?]$", flat[j]["spoken"]):  # no pause there yet
                            flat[j]["spoken"] += ","  # "Brasil (unrelated)" -> "Brasil, unrelated"
                    # a spoken remark that closes here, with more spoken words after it in the same cell
                    if t["disp"].endswith(")") and t["spoken"] and nxt and nxt["spoken"] \
                            and not re.search(r"[.,;:!?]$", t["spoken"]):  # a remark closes mid-sentence
                        t["spoken"] += ","  # "(the scan may be older) shows" -> "older, shows"
            # heading number -> comma pause after it ("8.10 Editorial quality" -> "8 point 10, Editorial quality")
            # a heading that starts with a number, which is spoken and has no punctuation after it yet
            if b["kind"] in ("h1", "h2", "h3") and len(ids) > 1 and HEADING_NUMBER.fullmatch(words[0]) \
                    and flat[ids[0]]["spoken"] and not re.search(r"[.,;:!?]$", flat[ids[0]]["spoken"]):  # bare
                flat[ids[0]]["spoken"] += ","  # the voice pauses between the number and the title
            # dash -> comma pause on the previous spoken word
            for k, i in enumerate(ids):
                if flat[i]["disp"] in PAUSE_MARKS and k:  # a free-standing pause mark that is not the first word
                    j = ids[k - 1]  # id of the word just before the dash
                    # only if that word is spoken and does not already end in punctuation
                    if flat[j]["spoken"] and not re.search(r"[.,;:!?]$", flat[j]["spoken"]):
                        flat[j]["spoken"] += ","  # the comma makes the voice pause where the dash is printed
            lead = ""  # words spoken before this cell (a column name), added after the pronunciation fixes
            if by_rows and b["kind"] in ("th", "tr") and ids:  # tables read row by row, as sentences
                said = [i for i in ids if flat[i]["spoken"]]  # the cell's spoken words
                if b["kind"] == "th":  # the header row: remembered for the rows, shown but not read
                    if ci in live:  # a spoken column
                        heads[live.index(ci)] = " ".join(flat[i]["spoken"] for i in said).rstrip(".,;:")  # "Basis"
                    for i in ids:  # every header word
                        flat[i]["spoken"] = ""  # silent
                elif said and ci in live:  # a spoken cell of a row
                    second = cells[live[1]] if len(live) > 1 and live[1] < len(cells) else ""  # its second cell
                    mark, lead = _row_cell(live.index(ci), len(live), heads, second, num_col)  # how it is read
                    j = said[-1]  # the cell's last spoken word
                    if not re.search(r"[!?]$", flat[j]["spoken"]):  # unless it ends a question or exclamation
                        flat[j]["spoken"] = flat[j]["spoken"].rstrip(",;:.") + mark  # "CNPJ:" / "High."
            # table cells, and source titles when they are read, are read as separate sentences
            elif (b["kind"] in ("th", "tr") or (b["kind"] == "link" and opts.get("read_links"))) and ids:
                # ids of the words that are actually spoken in this cell
                spoken_ids = [i for i in ids if flat[i]["spoken"]]
                if spoken_ids:  # a cell made only of silent tokens (such as the "—" placeholder) needs nothing
                    j = spoken_ids[-1]  # the last spoken word of the cell
                    if not re.search(r"[.!?]$", flat[j]["spoken"]):  # unless it already ends a sentence
                        # any trailing comma, semicolon or colon becomes a full stop, so each cell is read as its own
                        # sentence
                        flat[j]["spoken"] = flat[j]["spoken"].rstrip(",;:") + "."
            # overrides last, so dash/table punctuation is kept
            for i in ids:
                # keep the unoverridden form for the pronunciation audit to compare against
                flat[i]["spoken_ref"] = flat[i]["spoken"]
                if flat[i]["spoken"]:  # silent tokens are skipped
                    # the reviewed respelling replaces the spoken text the voice will receive
                    flat[i]["spoken"] = apply_override(flat[i]["spoken"], i, words_ov, tokens_ov, low)
            if lead:  # a column name goes before the cell's first spoken word ("Evidence: Terms")
                first = next(i for i in ids if flat[i]["spoken"])  # that word's token
                flat[first]["spoken"] = lead + flat[first]["spoken"]  # what the voice receives
                flat[first]["spoken_ref"] = lead + flat[first]["spoken_ref"]  # and what a listener should hear
            b["cell_tokens"].append(ids)  # store this cell's ids on the block
        # the block's tokens in reading order, cells concatenated
        b["tokens"] = [i for ids in b["cell_tokens"] for i in ids]
    return flat  # the list every later stage indexes by token id


# Group the spoken tokens into narration segments, the units the voice renders one audio clip for.
# blocks are the tokenised blocks and flat the token list from tokenize(); both are read, not changed.
# Returns a list of dicts {id, kind, tokens, closed, first_block, last_block, spokenText, text}.
# One segment per block, except that consecutive verse lines are merged until one ends a sentence.
def segments(blocks: list, flat: list) -> list:
    """Group tokens into narration segments (verse lines merged until a sentence ends)."""
    segs, cur = [], None  # finished segments, and the segment currently being filled
    for bi, b in enumerate(blocks):  # visit blocks in order, remembering each index for first_block/last_block
        # silent tokens such as dashes are left out of the narration
        toks = [i for i in b["tokens"] if flat[i]["spoken"]]
        if not toks:  # a block with nothing to say (for example only dashes)
            continue  # produces no segment and leaves the current one open
        # a verse line continuing an unfinished verse sentence
        if b["kind"] == "verse" and cur and cur["kind"] == "verse" and not cur["closed"]:
            cur["tokens"] += toks  # joins the same segment so the voice reads it with one intonation
        else:  # any other block starts a fresh segment
            # closed tells the next verse line whether it may still join this segment
            cur = {"kind": b["kind"], "tokens": list(toks), "closed": False, "first_block": bi}
            segs.append(cur)  # registered now; later fields are filled in on the same dict
        cur["last_block"] = bi  # the segment so far ends in this block
        # prose is always one segment; a verse segment closes on a final . ! or ?
        if b["kind"] != "verse" or re.search(r"[.!?]$", flat[toks[-1]]["spoken"]):
            cur["closed"] = True  # the next block will start a new segment
    for n, s in enumerate(segs):  # n gives each segment its running number
        s["id"] = f"s{n:04d}"  # stable ids s0000, s0001 ... in reading order, used as audio file names and request ids
        # the exact text the voice receives for this segment
        s["spokenText"] = " ".join(flat[i]["spoken"] for i in s["tokens"])
        # the printed words of the same tokens, shown next to the audio in reports
        s["text"] = " ".join(flat[i]["disp"] for i in s["tokens"])
    return segs  # the segments in narration order


# The script of a project in memory, without writing anything: blocks, tokens and narration segments.
# project is a speechkit_narrate.project.Project; the book is read from work/book.md and overrides from work/pronunciations.json.
# prevoice=False leaves the prevoice stage's fixes out (that stage reads the script as hand and loop fixes make it).
# Returns (blocks, tokens, segments, counts); counts holds "overrides" and, when prevoice is enabled, "prevoice" (how
# many of its fixes were applied). Raises ValueError if book.md has structural problems.
def model(project, prevoice: bool = True) -> tuple[list, list, list, dict]:
    text = project.p("book").read_text(encoding="utf-8")  # the reviewed book text that everything is generated from
    # all validate() problems except the "no chapter titles" warning, which does not stop narration
    probs = [p for p in markup.validate(text, project.title) if "no '# ' chapter" not in p]
    if probs:  # any remaining problem would break a later stage
        raise ValueError("book.md problems: " + "; ".join(probs))  # stop early with every problem listed in one message
    blocks = markup.parse(text, project.title)  # parse again into blocks, cover first
    # reviewed respellings, both word-wide and per token
    words_ov, tokens_ov = load_overrides(project.p("pronunciations"))
    low = load_prevoice(project) if prevoice else {}  # the prevoice layer ({} unless enabled, current and wanted)
    # tokens and spoken forms, using the project's spoken.* settings
    flat = tokenize(blocks, project.cfg["spoken"], words_ov, tokens_ov, low)
    segs = segments(blocks, flat)  # narration units built from the spoken tokens
    counts = {"overrides": len(words_ov) + len(tokens_ov)}  # how many pronunciation fixes were loaded
    if (project.cfg.get("prevoice") or {}).get("enabled"):  # reported only when the stage is on
        # prevoice fixes that reached the script (hand and loop fixes take the others)
        counts["prevoice"] = sum(1 for i, (_, say) in low.items()
                                 if i < len(flat) and flat[i]["spoken"].startswith(say))  # its fix is what is spoken
    return blocks, flat, segs, counts  # everything build() writes


# Build the script stage outputs for a project: the docmodel and the voice request.
# project is a speechkit_narrate.project.Project; the book is read from work/book.md and overrides from work/pronunciations.json.
# Writes work/request.json (segments for the narrator) and work/docmodel.json (blocks, tokens, segments for every
# renderer).
# Returns a dict of counts for the stage log; raises ValueError if book.md has structural problems.
def build(project) -> dict:
    """Write docmodel.json and request.json for a project; returns counts."""
    blocks, flat, segs, counts = model(project)  # the script, with every layer of pronunciation fixes
    # the request handed to the voice engine; id is the project slug
    req = {"schemaVersion": "1.0.0", "id": project.slug,
           # per segment: display text and the full spoken sentence
           "segments": [{"id": s["id"], "text": s["text"], "spokenText": s["spokenText"],
                         # the spoken word list and the matching global token ids, used to map word timings back to the
                         # page
                         "tokens": [flat[i]["spoken"] for i in s["tokens"]], "tokenIndex": s["tokens"]} for s in segs]}
    project.ensure("request")  # creates the work/ folder; both output files live there
    # indent=1 keeps the request readable and diffable; ensure_ascii=False keeps curly quotes as characters
    project.p("request").write_text(json.dumps(req, indent=1, ensure_ascii=False), encoding="utf-8")
    # the full model that video, web, print and audit read; no indent because it is large
    project.p("docmodel").write_text(json.dumps({"blocks": blocks, "tokens": flat, "segments": segs},
                                                # UTF-8 so non-ASCII text survives on every platform
                                                ensure_ascii=False), encoding="utf-8")
    return {"blocks": len(blocks), "tokens": len(flat), "segments": len(segs),  # summary numbers for the stage log
            "spoken_words": sum(len(s["tokens"]) for s in segs),  # words that will actually be spoken
            **counts}  # how many pronunciation fixes were loaded (and, with prevoice on, how many of its own)
