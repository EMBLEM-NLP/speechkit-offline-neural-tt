# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Word-level repairs and the review list.

    spacing      "intense.The" -> "intense. The", "inside,even" -> "inside, even", "said.”Then" -> "said.” Then"
    glued words  "ofthe" -> "of the", "Grahambefore" -> "Graham before" when the split is unambiguous
    text_fixes   the project's own whole-word (or phrase) replacements, applied last
    suspects     words a person should check: glued words not split, OCR-type misreads, unknown words
"""
from __future__ import annotations  # hints such as tuple[str, list] stay unevaluated strings at runtime

import re  # all repairs here are regular-expression rewrites of block text
from collections import Counter  # word frequencies inside one document (recurring names are left alone)

# shared word helpers: function-word list, token splitter, frequency and dictionary lookups
from .common import FUNCTION_WORDS, core, have_wordfreq, is_numberish, known, zipf

# One token is any run of non-space characters; the repairs work token by token.
TOKEN = re.compile(r"\S+")  # compiled once because it runs over every block of the book


# ----------------------------------------------------------------- spacing

# Decide whether a token is a web or e-mail address, which must never have spaces inserted into it.
# tok is one whitespace-free token (str); returns a truthy value (True or a regex match) when it looks like a URL or
# address.
def _urlish(tok: str) -> bool:
    # an @, a scheme, a leading www. or a common top-level domain such as .com
    return "@" in tok or "://" in tok or tok.lower().startswith("www.") or re.search(r"\.(com|org|net|io|gov|edu)\b", tok)


# Repair missing and extra spaces around punctuation in one block of text, as OCR and PDF text layers often produce
# them.
# text is a block's text (str); returns the repaired text with single spaces and no leading or trailing space.
# Pure function; extract_blocks() runs it on every text block before the quote and glued-word repairs.
def fix_spacing(text: str) -> str:
    # Fix one token in place; called by TOKEN.sub for every non-space run in the text.
    # m is the regex match of the token; the returned string replaces it, so spaces may be inserted inside it.
    def tok(m):
        t = m.group(0)  # the token's current text
        # addresses and numbers such as 3.5, 1,000 or 23:1 keep their punctuation unspaced
        if _urlish(t) or re.search(r"\d[.,:]\d", t):
            return t  # left exactly as it was, because inserting spaces would break the address or the number
        # "intense.The" becomes "intense. The": a sentence end glued to a capitalised word (2+ lower-case letters before
        # it)
        t = re.sub(r"([a-z]{2,}[a-z’'”)\]]?)([.!?])([A-Z][a-z]+)", r"\1\2 \3", t)
        # "inside,even" becomes "inside, even": a comma, semicolon or colon glued between two words
        t = re.sub(r"([a-z]{2,})([,;:])([A-Za-z]{2,})", r"\1\2 \3", t)
        # 'said.”Then' becomes 'said.” Then': a closing quote glued to the next word
        t = re.sub(r"([.,;!?])”([A-Za-z]{2,})", r"\1” \2", t)
        # 'said“Hello' becomes 'said “Hello': an opening quote glued to the word before it
        t = re.sub(r"([a-z]{2,})“([A-Za-z])", r"\1 “\2", t)
        return t  # the repaired token, possibly now containing spaces
    text = TOKEN.sub(tok, text)  # apply the token repair to every token of the text
    text = re.sub(r"(\w) +([,;:!?](\s|$))", r"\1\2", text)      # "word ," -> "word,"
    text = re.sub(r"(\w) \.(\s|$)", r"\1.\2", text)  # "word ." at a word end becomes "word."
    text = re.sub(r"\s{2,}", " ", text)  # collapse any double spaces left by the repairs
    return text.strip()  # trim the ends of the block


# ----------------------------------------------------------------- quotes

# Close quotation marks that OCR got wrong, working through one block's text character by character.
# text is the block text (str); returns (repaired text, list of (from, to) pairs describing each change).
# Only curly marks are tracked: open_d counts unclosed “ and open_s unclosed ‘ in the text so far.
# True when a closing ” follows position i before any new opening “, i.e. the dialogue quote that is open at i is
# closed by a real mark later in the text.
def _closed_later(text: str, i: int) -> bool:
    close, opening = text.find("”", i), text.find("“", i)  # the next closing and opening double quotes, or -1
    return close != -1 and (opening == -1 or close < opening)


def balance_quotes(text: str) -> tuple[str, list]:
    """OCR often reads a closing ” as ’ and doubles marks ("rest.”’"). Close an open “ with ” where a
    stray ’ ends a word, and drop a ’ glued after ” when no ‘ is open."""
    out, changes = [], []  # output characters and the change list
    open_d = open_s = 0  # how many double and single quotes are currently open
    i = 0  # current position in the text
    n = len(text)  # length of the text
    while i < n:  # walk the text one character at a time
        c = text[i]  # the character being looked at
        if c == "“":  # an opening double quote
            open_d += 1  # one more dialogue quote is open
        elif c == "”":  # a closing double quote
            open_d = max(0, open_d - 1)  # close one open quote, never going below zero
            # ”’ with no single quote open: OCR doubled the closing mark
            if i + 1 < n and text[i + 1] == "’" and not open_s:
                out.append(c)  # keep only the ”
                changes.append(("”’", "”"))  # record the repair for the report
                i += 2  # advance past both the ” and the stray ’, so the ’ is dropped from the output
                continue  # continue after the dropped mark
        elif c == "‘":  # an opening single quote
            open_s += 1  # one more single quote is open
        elif c == "’":  # a right single quote: an apostrophe, a closing single quote, or a misread ”
            prev = text[i - 1] if i else " "  # the character before it, a space at the very start
            nxt = text[i + 1] if i + 1 < n else " "  # the character after it, a space at the very end
            word_end = not nxt.isalnum()  # nothing alphanumeric follows, so the mark ends a word
            if open_s and word_end:  # a single quote is open and this mark ends a word
                open_s -= 1  # so it simply closes that single quote
            # Otherwise, at a word end inside an open double quote, after a letter, digit or sentence punctuation but
            # not after "s" (to spare plural possessives such as parents’), the ’ is taken to be a misread closing ”,
            # unless the open quote is still closed by a real ” later on (before the next “): then this mark is the
            # apostrophe of an elided word such as goin’, and is kept.
            elif word_end and open_d and prev != "s" and (prev.isalnum() or prev in ".,!?;:") \
                    and (nxt in " )]" or nxt in ".,;:!?" or i + 1 == n) \
                    and not _closed_later(text, i + 1):  # then a space, bracket, punctuation or end
                out.append("”")  # write the corrected closing mark
                changes.append(("’", "”"))  # record ’ -> ” for the report
                open_d -= 1  # that dialogue quote is now closed
                i += 1  # step past the replaced mark
                continue  # skip the default append below
        out.append(c)  # every other character is copied unchanged
        i += 1  # move on to the following character of the text
    return "".join(out), changes  # the repaired text plus the list of changes


# Apply balance_quotes() to the OCR-derived blocks and log every change.
# blocks is the block list, log the autofix list that ends up in report.json "autofixes".
# Returns a new block list; blocks from the PDF text layer are passed through, because their quotes are trusted.
def fix_quotes(blocks: list, log: list) -> list:
    out = []  # the repaired copy of the block list, built in the original order
    for b in blocks:  # visit every block, OCR and text-layer alike, keeping their order
        if b.get("ocr") and "text" in b:  # only blocks containing OCR text and having a text field (not table rows)
            t, ch = balance_quotes(b["text"])  # repaired text and the list of changes
            for a, z in ch:  # one log entry per change
                # page number lets the user find the spot in the PDF
                log.append({"page": b.get("page"), "from": a, "to": z, "why": "unbalanced quote marks (OCR)"})
            b = dict(b, text=t)  # a copy of the block with the repaired text, leaving the input untouched
        out.append(b)  # unchanged and repaired blocks alike are kept in order
    return out  # the block list with quotes balanced


# ----------------------------------------------------------------- glued words

# Yield every way a glued token could be split into two dictionary words.
# w is the bare word (str); yields (split index, left part, right part, score) with both parts lower-cased.
# The score is the lower of the two parts' Zipf frequencies, so a split is only as good as its rarer word.
def _split_candidates(w: str):
    # lower-case with a straight apostrophe, the form the dictionaries use; the length is unchanged
    lw = w.lower().replace("’", "'")
    for i in range(1, len(lw)):  # every position strictly inside the word
        a, b = lw[:i], lw[i:]  # left and right candidates
        if len(a) == 1 and a not in ("a", "i"):  # a one-letter left part is only allowed for the words "a" and "I"
            continue  # other single letters are almost always OCR noise
        if len(b) == 1 and b not in ("a", "i"):  # the same rule for a one-letter right part
            continue  # skip this split point
        if b.startswith("'"):  # a right part starting with an apostrophe is a contraction, as in don't, not two words
            continue  # skip this split point, since a piece like don + 't is one contracted word
        if not (known(a) and known(b)):  # both parts must be real words
            continue  # otherwise this is not a split
        yield i, a, b, min(zipf(a), zipf(b))  # a plausible split with its weaker-word score


# Pick the most plausible two-word split of a glued token.
# w is the bare word (str). The docstring says (i, score), but the function returns a 4-tuple (i, score, left, right),
# or None.
def best_split(w: str):
    """-> (i, score) of the most plausible two-word split of a glued token, or None."""
    best = None  # best candidate so far
    for i, a, b, sc in _split_candidates(w):  # look at all dictionary-valid splits
        if best is None or sc > best[1]:  # keep the one whose weaker word is most frequent
            best = (i, sc, a, b)  # remember index, score and both parts
    return best  # None when no split gives two dictionary words


# Glued-word judge that uses evidence from the whole document, so recurring names and real words are left alone.
# Built from a block list; decide() is then asked about single words. split_glued() and suspects() each build one.
class Glue:
    """Decides glued-word splits with document-wide evidence (names that recur are left alone)."""

    # Count how often every bare word occurs in the document.
    # blocks is the block list; only "text" blocks are counted, table cells are not.
    def __init__(self, blocks):
        self.lenient = False  # becomes True when the document has shown it glues words often (set by split_glued)
        self.counts = Counter()  # exact-case counts of bare words
        self.standalone = Counter()  # lower-case counts; filled here but not read anywhere
        for b in blocks:  # every block of the book
            for t in TOKEN.findall(b.get("text", "")):  # each whitespace token of its text
                w = core(t)[1]  # the bare word without surrounding punctuation
                if w:  # a token of pure punctuation has an empty bare word and is not counted
                    self.counts[w] += 1  # case-sensitive tally, used to recognise recurring names
                    # the same word counted ignoring case; nothing reads this tally at present
                    self.standalone[w.lower()] += 1

    # Decide what to do with one word that may be two words run together.
    # w is the bare word (str); src says where it came from: "ocr", "layer" (PDF text layer) or "text" (typed input).
    # Returns ("split", "two words") when the split is safe, ("suspect", "two words") when a person should check it, or
    # None.
    # The thresholds use Zipf frequencies: about 3 is a fairly common word, below 1 is very rare, 0 is unknown.
    def decide(self, w: str, src: str = "ocr"):
        """-> ('split', new) | ('suspect', suggestion) | None.

        src is where the token came from: "ocr" (Tesseract glues words often), "layer" (a PDF text layer:
        split only with strong support) or "text" (typed input: never split, only flag)."""
        # too short, or containing non-letters (except a simple contraction such as "don’t")
        if len(w) < 3 or not w.isalpha() and not re.fullmatch(r"[A-Za-z]+[’'][a-z]+", w):
            return None  # never split such tokens
        if w.isupper() or known(w):  # an acronym in capitals or a word the dictionary already knows
            return None  # an all-caps acronym or a word CMUdict knows is never treated as glued
        bs = best_split(w)  # most plausible split of this token
        if bs is None:  # no split into two known words
            return None  # it cannot be cut into two known words, so there is nothing to propose
        i, sc, a, b = bs  # split index, weaker-word score, left part, right part
        new = w[:i] + " " + w[i:]  # the proposed spelling with a space at the split point
        zw = zipf(w)  # frequency of the glued form itself; 0 means no corpus has seen it
        # one half is a closed-class word such as "of", "the", "to"
        func = a in FUNCTION_WORDS or b.strip("'") in FUNCTION_WORDS
        # both halves are closed-class words, as in "ofthe": never a real word
        both = a in FUNCTION_WORDS and b.strip("'") in FUNCTION_WORDS
        # a split worth showing: two function words, a common pair forming an unseen word, or a function word glued to a
        # very common one
        plausible = both or (sc >= 3.0 and zw < 1.0) or (func and sc >= 4.0 and zw < 1.6)
        # typed text is never changed; mixed capitals such as "TheLord" are only flagged too
        if src == "text" or (w[0].isupper() and not w[1:].islower()):
            return ("suspect", new) if plausible else None  # flagged for review when plausible, otherwise ignored
        if w[0].isupper():  # a capitalised word with a lower-case rest
            # capitalised: a name unless its first part stands alone elsewhere or is a function word
            first = w[:i]  # the left part as written, with its capital
            # the same capitalised token occurs more than once: a name, unless a function word is involved
            if self.counts[w] > 1 and not func:
                return None  # leave the name alone
            # neither a function word nor a left part that appears on its own elsewhere
            if not (func or self.counts.get(first, 0) >= 1):
                return ("suspect", new) if sc >= 3.0 else None  # only flagged, and only when the pair is common
        if self.counts[w] >= 2 and not both:  # a lower-case token that recurs in the document
            return ("suspect", new) if plausible else None      # recurs: probably a real word or a name
        if both:  # two function words glued together
            return ("split", new)                  # "ofthe", "bya", "thatwas": never a word
        if len(w) < 4 or not have_wordfreq():      # no frequency list: only flag, never guess
            # without a frequency list the evidence is too weak to change text
            return ("suspect", new) if plausible else None
        if src == "layer":  # a word from the PDF text layer, which glues words much less often than OCR
            # a text-layer word is split only with strong support: a closed-class word glued to a common
            # word, or (in a document that demonstrably glues words) an inflected word glued to another
            # function word glued to another, the glued form very rare, and the pair reasonably common
            if func and zw < 1.0 and sc >= 2.5:
                return ("split", new)  # safe enough to split
            # in a document that glues words: unseen form and one half is an inflected word ("needsground")
            if self.lenient and zw == 0 and sc >= 3.0 and (_inflectional(a) or _inflectional(b)):
                return ("split", new)  # split on the strength of the document's habit
            # in a document that glues words: function word plus a very common word
            if self.lenient and func and zw < 1.6 and sc >= 4.0:
                return ("split", new)              # "thisweek", "indebt" in a document that glues words
            return ("suspect", new) if plausible else None  # anything else from the text layer is at most flagged
        if zw == 0 and sc >= (2.5 if func else 3.0):  # OCR token never seen as a whole and both halves common enough
            return ("split", new)  # an OCR glue that is safe to undo
        # lenient document: a function word glued to a very common word
        if self.lenient and func and zw < 1.6 and sc >= 4.0:
            return ("split", new)                  # the document glues words often: "backto", "indebt"
        return ("suspect", new) if plausible else None  # otherwise flag when plausible


# Tell decide() where a token came from.
# b is the block dict, w the bare word, mode "pdf" or "text"; returns "text", "ocr" or "layer".
def _token_src(b: dict, w: str, mode: str) -> str:
    if mode == "text":  # the book came from a typed .txt or .md file rather than from a PDF
        return "text"  # every token of it is "text"
    # layout.py records the bare words read by OCR in the block's "ocr_words" set
    return "ocr" if w in (b.get("ocr_words") or ()) else "layer"


# Split glued words in the block texts and log every split for the report.
# blocks is the block list; log the autofix list; mode "pdf" uses the source of each token, "text" changes nothing.
# Returns a new block list; suspicious tokens that are not split are reported later by suspects().
def split_glued(blocks: list, log: list, mode: str = "pdf") -> list:
    """Split glued words. mode "pdf": OCR tokens by frequency evidence, text-layer tokens only with strong
    support; mode "text": never (typed input), suspects only."""
    g = Glue(blocks)  # a judge loaded with how often each word occurs across the whole book
    if mode == "text":  # typed input is never altered here
        return blocks  # typed input is trusted, so the blocks come back untouched (suspects() may still flag words)
    # a document with several certain glued pairs (a closed-class word stuck to a common word) gets the
    # benefit of the doubt on inflected pairs ("needsground") too
    sure = 0  # number of certain glued pairs in the document
    for b in blocks:  # first pass over every block, counting the glued pairs that are certain
        for t in b.get("text", "").split():  # every token of its text
            w = core(t)[1]  # strip punctuation from both ends, so a token like ofthe, becomes ofthe
            # only tokens beginning with a function word (one of its prefixes is in FUNCTION_WORDS)
            if w and FUNCTION_WORDS & {w[:k].lower() for k in range(1, len(w))}:
                d = g.decide(w, _token_src(b, w, mode))  # ask the strict judge (lenient is still False)
                sure += bool(d and d[0] == "split")  # count the confident splits
    g.lenient = sure >= 5  # five or more certain pairs: the document glues words, so allow more splits
    out = []  # the repaired block list
    for b in blocks:  # second pass: repair each block with the strictness now chosen
        if "text" not in b:  # table rows have no text field
            out.append(b)  # blocks without a text field, such as table rows, are kept unchanged
            continue  # nothing to split in this block, so go on to the following one
        toks = b["text"].split(" ")  # single-space split, so joining with " " restores the text exactly
        for k, t in enumerate(toks):  # k is the token index
            pre, w, post = core(t)  # leading punctuation, bare word, trailing punctuation
            if not w:  # the token is only punctuation, such as a dash or an ellipsis
                continue  # nothing to split, so the token stays as it is
            d = g.decide(w, _token_src(b, w, mode))  # the decision, now possibly lenient
            if d and d[0] == "split":  # the judge is confident; a mere "suspect" verdict leaves the text alone
                toks[k] = pre + d[1] + post  # rebuild the token with its punctuation around the two words
                # every split is reported, so the user can undo it with text_fixes
                log.append({"page": b.get("page"), "from": t, "to": toks[k], "why": "glued words"})
        out.append(dict(b, text=" ".join(toks)))  # a copy of the block with the repaired text
    return out  # the block list with glued words split


# ----------------------------------------------------------------- project fixes

# Apply the project's own replacements from project.json "text_fixes" to the draft blocks.
# blocks is the block list, fixes a {from: to} dict of words or phrases, log the list stored as report.json
# "text_fixes".
# Returns a new block list; each fix is logged with the number of times it matched, so unused fixes are easy to spot.
def apply_text_fixes(blocks: list, fixes: dict, log: list) -> list:
    """Whole-word (or whole-phrase) replacements from project.json "text_fixes", applied last."""
    if not fixes:  # project.json has no text_fixes, or an empty object
        return blocks  # the blocks are returned as they are
    # One pattern per fix that matches only whole words: no letter, digit or apostrophe may touch either end.
    # the key is escaped, so dots or brackets in it are literal
    pats = [(re.compile(r"(?<![\w’'])" + re.escape(k) + r"(?![\w’'])"), v, k)
            # longest keys first, so a phrase wins over a word inside it; empty keys are skipped
            for k, v in sorted(fixes.items(), key=lambda kv: -len(kv[0])) if k]
    used = Counter()  # how many replacements each key made
    out = []  # the new block list
    for b in blocks:  # visit every block of the draft in order
        b = dict(b)  # a shallow copy, so the caller's blocks are not modified
        for key in ("text",):  # a one-element loop over the field name
            if key in b and b.get("kind") != "code":  # blocks that have text; a command block is never rewritten
                for p, v, k in pats:  # apply every fix in turn
                    # a function replacement inserts the value literally (a backslash in it is not a group reference)
                    b[key], n = p.subn(lambda _m, v=v: v, b[key])
                    used[k] += n  # add this block's matches to the total
        if "cells" in b:  # a table row: the fixes apply to every cell text as well
            cells = []  # the row's cell texts after the replacements
            for c in b["cells"]:  # each cell's text, left to right
                for p, v, k in pats:  # apply every fix to the cell
                    # replace every whole-word match in the cell; n is how many replacements were made
                    c, n = p.subn(lambda _m, v=v: v, c)
                    used[k] += n  # add to the total
                cells.append(c)  # store the cell with all fixes applied
            b["cells"] = cells  # replace the cell list
        out.append(b)  # add the fixed copy, keeping the block order
    for k in fixes:  # every configured fix appears in the log
        # with its replacement and match count (0 means it never matched)
        log.append({"fix": k, "to": fixes[k], "count": used[k]})
    return out  # the block list with the project's fixes applied


# ----------------------------------------------------------------- suspects

# Word shapes Tesseract typically produces instead of I-words or "m": "lam" for "I am", "Tm" for "I’m", "rn" for "m".
# any occurrence is listed for review
OCR_SHAPES = {"lam", "Iam", "Ihave", "Tm", "Pm", "Tf", "Ts", "Tt", "Jn", "Pll", "Td", "Tve", "Il", "TI", "rn"}


# Find the page a word in a block came from, for the review list.
# b is the block; tok_index_text is the word (str). Uses the block's "parts" list of (line text, page) from layout.py.
# Returns the page number of the first part containing the word, or the block's first page.
def _page_of(b, tok_index_text):
    parts = b.get("parts")  # pieces of the block with their page numbers
    if not parts:  # text input has no parts
        return b.get("page")  # fall back to the block page, None for text input
    for t, pg in parts:  # each source line in order
        if tok_index_text in t:  # substring test on the line as extracted, before any repairs
            return pg  # page of the first line that contains it
    return b.get("page")  # word not found, perhaps because a repair changed it


# Is w an inflected form (plural, past tense, -ing) of a dictionary word?
# Used to allow text-layer splits such as "needsground", because compounds rarely contain an inflected form.
def _inflectional(w: str) -> bool:
    """needs, showed, qualified, happening: an inflected form of a known word (compounds rarely contain one)."""
    lw = w.lower()  # lower-case for dictionary lookups
    # suffix to remove and letters to put back, e.g. "ies" -> "y" turns "carries" into "carry"
    for suf, add in (("s", ""), ("es", ""), ("ies", "y"), ("ied", "y"), ("ed", ""), ("ed", "e"), ("ing", ""),
                     ("ing", "e")):  # the ing forms, plain and with a restored "e" (making -> make)
        # the stem must keep at least three letters and be a known word
        if lw.endswith(suf) and len(lw) - len(suf) >= 3 and known(lw[: -len(suf)] + add):
            return True  # it is an inflected form
    return False  # no suffix leaves a known stem, so the word is not an inflected form


# A broader version of _inflectional() that also accepts derived forms (-ly, -er, -est, -ness, -ment).
# suspects() uses it so that unusual but well-formed words are not listed as unknown.
def _inflected(w: str) -> bool:
    lw = w.lower()  # lower-case once, because the dictionary lookups are lower-case
    # plural, past and participle endings, including bare "d" (loved -> love)
    for suf, add in (("s", ""), ("es", ""), ("ied", "y"), ("ies", "y"), ("ed", ""), ("ed", "e"), ("d", ""),
                     ("ing", ""), ("ing", "e"),  # -ing removed as is (walking) or with an e put back (writing -> write)
                     # adverb, comparative, superlative and noun-forming endings
                     ("ly", ""), ("er", ""), ("est", ""), ("ness", ""), ("ment", "")):
        # stem of three or more letters that the dictionary knows
        if lw.endswith(suf) and len(lw) - len(suf) >= 3 and known(lw[: -len(suf)] + add):
            return True  # treat as a valid word
    return False  # not explained by any suffix


# Build the review list: words in the draft a person should check before narration.
# blocks is the final block list; limit caps the list (300 by default) so the report stays readable.
# Returns a list of dicts {word, why, count, pages, context, suggest?}, ordered by reason, then by frequency.
# extract.run() stores it as report.json "suspects", and the check stage prints the first few.
def suspects(blocks: list, limit: int = 300) -> list:
    """Words a person should look at, most frequent first within each reason."""
    names = Counter()  # capitalised words seen in the middle of a sentence (likely names)
    initial = Counter()  # capitalised words seen only at a sentence start
    for b in blocks:  # every block, prose or table
        # rough sentence split after . ! ? or a closing quote, followed by space
        for sent in re.split(r"(?<=[.!?”])\s+", b.get("text", "") or " ".join(b.get("cells", []))):
            toks = sent.split()  # words of the sentence
            for i, t in enumerate(toks):  # i is the word position in the sentence
                w = core(t)[1]  # the word without surrounding punctuation or quotation marks
                if w[:1].isupper() and w[1:].islower():  # Title-case word such as "Graham"
                    (initial if i == 0 else names)[w] += 1  # sentence-initial ones go to initial, the rest to names
    glue = Glue(blocks)  # a judge with this document's word counts
    found = {}  # (word, reason) -> entry

    # Record one finding: the first sighting creates the entry with context, later ones raise the count and add pages.
    # word is the token, why the reason text, b the block, text the block text for context, suggest an optional fix.
    def add(word, why, b, text, suggest=None):
        key = (word, why)  # one entry per word and reason
        e = found.get(key)  # existing entry, if any
        if e is None:  # first time this word is flagged for this reason
            i = text.find(word)  # position of its first occurrence in the block
            ctx = text[max(0, i - 40): i + len(word) + 40]  # about 40 characters of text on each side
            if i > 40:  # text was cut on the left
                ctx = "…" + ctx  # mark it with an ellipsis
            if i + len(word) + 40 < len(text):  # text was cut on the right
                ctx += "…"  # an ellipsis shows the excerpt continues past its right edge
            # new entry; pages collects where it occurs
            e = found[key] = {"word": word, "why": why, "count": 0, "pages": [], "context": ctx}
            if suggest:  # a proposed replacement is available
                e["suggest"] = suggest  # stored with the entry
        e["count"] += 1  # count this sighting, the first one included
        pg = _page_of(b, word)  # page of this occurrence
        if pg and pg not in e["pages"] and len(e["pages"]) < 12:  # list up to 12 different pages
            e["pages"].append(pg)  # remember this page so the reviewer can find the word in the PDF

    for b in blocks:  # walk every block, prose and table rows alike, checking each token
        text = b.get("text") or " | ".join(b.get("cells", []))  # context text; table cells joined with " | "
        toks = (b.get("text") or " ".join(b.get("cells", []))).split()  # the tokens; table cells joined with spaces
        for i, t in enumerate(toks):  # i is the token position
            pre, w, post = core(t)  # leading punctuation, bare word, trailing punctuation
            if not w:  # a token with no letters or digits, such as a dash or a stray bar
                # a lone "|" is often a misread capital I ("l" never gets here because it counts as a word)
                if t in ("|", "l"):
                    add(t, "possible OCR misread of “I”", b, text, "I")  # flagged with the suggestion "I"
                continue  # no further checks for punctuation
            nxt = toks[i + 1] if i + 1 < len(toks) else ""  # the following token, "" at the end
            # "T" or "l" at the start or before a lower-case word is probably "I"
            if w in ("T", "l") and (i == 0 or nxt[:1].islower()):
                add(w, "possible OCR misread of “I”", b, text, "I")  # flag with the suggestion "I"
                continue  # this token is done
            if w in OCR_SHAPES:  # a known OCR misread shape
                add(w, "typical OCR misread", b, text)  # flagged without a suggestion
                continue  # move on to the following word
            if re.search(r"\d", w):  # the word has at least one digit, as in 1914, 21st or a misread c1ear
                # Letters mixed with digits, unless it is an ordinal or unit (21st, 5pm, 3x), or a hyphenated code (A-1,
                # 3-D).
                if re.search(r"[A-Za-z]", w) and not re.fullmatch(r"\d+(st|nd|rd|th|s|am|pm|k|m|x)|[A-Za-z]+-\d+|\d+-[A-Za-z]+", w,
                                                                # re.I makes the exception list case-insensitive.
                                                                re.I):
                    add(w, "letters and digits mixed (OCR?)", b, text)  # such as "c1ear" where OCR read l as 1
                continue  # numbers are not checked any further
            # numbers, roman numerals and ALL-CAPS words are not dictionary words but are fine
            if is_numberish(w) or w.isupper():
                continue  # accept the word without any dictionary or glued-word check
            # a capital inside a word, except Mc, Mac and O’ names
            if re.search(r"[a-z][A-Z]", w) and not re.match(r"^(Mc|Mac|O’|O')", w):
                add(w, "odd capitals (glued or misread?)", b, text,  # flagged as glued or misread
                    re.sub(r"([a-z])([A-Z])", r"\1 \2", w))  # suggestion: a space before each inner capital
                continue  # no further checks for this word
            if known(w):  # CMUdict knows it, allowing for possessives, plurals and hyphenated parts
                continue  # a known word needs no review
            d = glue.decide(w)  # could it be two words run together?
            if d:  # the judge sees a possible split, either certain or worth a look
                add(w, "glued words?", b, text, d[1])  # listed with the proposed split as the suggestion
                continue  # already listed, so stop here
            # A capitalised word used as a name at least once mid-sentence, or twice in all, is not flagged,
            # because names are usually missing from dictionaries.
            if w[:1].isupper() and (names[w] >= 1 or initial[w] + names[w] >= 2):
                continue                                   # a name
            # common enough in the frequency list, every hyphen or apostrophe part common, or an inflected form
            if zipf(w) >= 1.5 or all(zipf(p) >= 3 for p in re.split(r"[-’']", w) if p) or _inflected(w):
                continue  # accepted: probably a real word that is only missing from the dictionary
            add(w, "not in the dictionary", b, text)  # anything left is unknown
    # reasons in the order a reviewer should work through them
    order = {"possible OCR misread of “I”": 0, "typical OCR misread": 1, "glued words?": 2,
             # then mixed letters and digits, odd capitals, and finally unknown words
             "letters and digits mixed (OCR?)": 3, "odd capitals (glued or misread?)": 4, "not in the dictionary": 5}
    # by reason, then most frequent first, then alphabetical
    out = sorted(found.values(), key=lambda e: (order.get(e["why"], 9), -e["count"], e["word"].lower()))
    return out[:limit]  # trimmed to the limit
