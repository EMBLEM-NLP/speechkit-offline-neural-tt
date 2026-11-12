# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Dictionary probe: the voice's espeak phonemes per unique word vs. the CMU pronouncing dictionary.

The narration phonemized every sentence piece with espeak-ng voice "en" (the pinned voice config),
so that is what is measured here; "en-us" is kept for reference only. Both espeak and CMU (ARPAbet,
converted to IPA) are reduced to a coarse common alphabet that ignores rhoticity, length, stress and
the usual British/American vowel splits before comparing; primary stress is compared separately.

    python3 -I emblem_rsi/pron/lexicon.py [--karaoke <project root>] [--outdir runs/pron-probe/lexicon]
"""
from __future__ import annotations  # lets annotations like str | None be written on any supported Python

import argparse  # command-line options of the standalone probe
import json  # JSON in and out: the helper protocol and lexicon_measure.json
import os  # reads EMBLEM_PIPER_PYTHON from the environment
import re  # regular expressions for ARPAbet symbols, numbers and display forms
import subprocess  # runs the espeak helper in the speechkit runtime venv
import sys  # sys.path tweak for script mode and the exit code
import unicodedata  # Unicode normalization: accent stripping and decomposing IPA
from collections import Counter, defaultdict  # Counter tallies occurrences; defaultdict groups rows without key checks
from difflib import SequenceMatcher  # character alignment used to split a sentence's phonemes among its words
from functools import lru_cache  # caches the loaded CMU dictionary
from pathlib import Path  # Path objects for the runtime interpreter, helper script and output files, joined with /

# Started as a plain script there is no parent package, so the repository root is put on sys.path and the
# inventory module is imported by its full name; inside the package a relative import is used.
if __package__ in (None, ""):
    # parents[2] of emblem_rsi/pron/lexicon.py is the repository root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from emblem_rsi.pron import inventory  # noqa: E402
else:  # normal case: imported as emblem_rsi.pron.lexicon
    from . import inventory  # inventory builds the table of every spoken word occurrence

SCHEMA = "emblem-rsi/pron-lexicon/1"  # format tag stored in lexicon_measure.json
REPO = Path(__file__).resolve().parents[2]  # repository root, used for the default output folder
# default output folder for the standalone run (runs/pron-probe/lexicon)
OUTDIR = REPO / "runs" / "pron-probe" / "lexicon"
HELPER = Path(__file__).with_name("_espeak_helper.py")  # the phonemizer helper script that sits next to this file
# Interpreter of the speechkit runtime venv, the only Python that has piper's espeak bridge. Read from the
# same variable as paths.RUNTIME_PY; None when it is unset (espeak() then needs an explicit runtime).
RUNTIME_PY = os.environ.get("EMBLEM_PIPER_PYTHON") or None  # see paths.py
NARRATION_VOICE = "en"  # espeak voice the narration actually used (from the pinned voice config)
REFERENCE_VOICE = "en-us"  # American espeak voice, phonemized only as a reference column
# Coarse distance at or above which a dictionary word is flagged: roughly a third of its sounds differ.
FLAG_DISTANCE = 0.34

# --------------------------------------------------------------------------- espeak (runtime venv)

# Phonemes already computed in this process, keyed by (voice, text), so repeated calls cost nothing.
_ESPEAK_CACHE: dict[tuple[str, str], str] = {}
ESPEAK_CALLS = 0  # how many helper subprocesses were started; reported in the totals


# Phonemizes many strings at once through _espeak_helper.py in the runtime venv. texts is any iterable of
# strings; voices is a tuple of espeak voice names (default only the narration voice "en"). Returns
# {voice: [IPA string per input text, same order]}. Side effects: at most one subprocess per call, for the
# strings not cached yet, and the module-level cache and call counter are updated. runtime is the runtime venv's
# python; None means RUNTIME_PY (EMBLEM_PIPER_PYTHON as read when this module was imported).
def espeak(texts, voices=(NARRATION_VOICE,), runtime=None) -> dict[str, list[str]]:
    """Phonemize texts with Piper's espeak bridge in the runtime venv: one subprocess for all uncached strings."""
    global ESPEAK_CALLS  # the call counter is a module global
    texts = list(texts)  # materialize the iterable because it is walked more than once
    # unique texts missing for at least one requested voice, sorted for a stable helper input
    todo = sorted({t for t in texts for v in voices if (v, t) not in _ESPEAK_CACHE})
    if todo:  # only start the helper when something is missing
        # runtime python (the caller's, else the module default) in isolated mode (-I) running the helper
        if not (runtime or RUNTIME_PY):  # no interpreter to run the helper with
            raise RuntimeError("the speechkit runtime Python is not configured: set EMBLEM_PIPER_PYTHON")
        args = [str(runtime or RUNTIME_PY), "-I", str(HELPER)]
        for v in voices:  # add one --voice option per requested voice so a single helper run answers all of them
            args += ["--voice", v]  # as "--voice NAME" pairs, the helper's only accepted arguments
        # Send the list as JSON on stdin and capture stdout as text. 600 s allows a whole book in one call;
        # errors="replace" keeps a stray undecodable byte from crashing the run.
        p = subprocess.run(args, input=json.dumps(todo), capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
        if p.returncode:  # a non-zero exit code means the helper failed
            # raise with the tail of its error output
            raise RuntimeError(f"espeak helper failed ({p.returncode}): {p.stderr.strip()[-500:]}")
        res = json.loads(p.stdout)  # the helper's JSON answer
        if len(voices) == 1:  # one voice comes back as a bare list
            res = {voices[0]: res}  # normalize it to the {voice: list} shape
        for v in voices:  # for every voice requested
            for t, ph in zip(todo, res[v]):  # pair each submitted text with its phonemes
                _ESPEAK_CACHE[(v, t)] = ph  # store in the process-wide cache
        # one more helper process started; the total appears as espeak_calls in the measurement totals
        ESPEAK_CALLS += 1
    # answer every input (duplicates included) from the cache, per voice
    return {v: [_ESPEAK_CACHE[(v, t)] for t in texts] for v in voices}


# Reports whether the espeak helper can run here: the runtime interpreter and the helper file must exist.
# measure.lexicon_metrics() uses it to skip the lexicon probe quietly on machines without the runtime.
def espeak_available() -> bool:
    # True only when the interpreter is configured and both files are present
    return bool(RUNTIME_PY) and Path(RUNTIME_PY).exists() and HELPER.exists()


# --------------------------------------------------------------------------- CMUdict and ARPAbet -> IPA

# ARPAbet vowel names to IPA (stress digits removed first); AH and ER are refined by stress in arpabet_to_ipa.
ARPA_VOWEL = {
    "AA": "ɑ", "AE": "æ", "AH": "ʌ", "AO": "ɔ", "AW": "aʊ", "AY": "aɪ", "EH": "ɛ", "ER": "ɝ",
    "EY": "eɪ", "IH": "ɪ", "IY": "i", "OW": "oʊ", "OY": "ɔɪ", "UH": "ʊ", "UW": "u",
}
# ARPAbet consonants to IPA; ɡ is the IPA script g and ɹ the English approximant r, as espeak writes them.
ARPA_CONS = {
    "B": "b", "CH": "tʃ", "D": "d", "DH": "ð", "F": "f", "G": "ɡ", "HH": "h", "JH": "dʒ", "K": "k",
    "L": "l", "M": "m", "N": "n", "NG": "ŋ", "P": "p", "R": "ɹ", "S": "s", "SH": "ʃ", "T": "t",
    "TH": "θ", "V": "v", "W": "w", "Y": "j", "Z": "z", "ZH": "ʒ",
}


# Converts one CMU pronunciation to an IPA string in espeak's notation so the two can be compared.
# arpa is a list of ARPAbet symbols or one space-separated string. Returns the IPA string with ˈ or ˌ
# placed before stressed vowels. Raises ValueError for a symbol that is not ARPAbet: one of another shape
# (lowercase, a stress digit other than 0-2) or an unknown name.
def arpabet_to_ipa(arpa) -> str:
    """ARPAbet (list or space-separated string) to IPA, stress mark placed before the vowel (espeak style).

    AH0 -> ə, AH1/2 -> ʌ, ER0 -> ɚ, ER1/2 -> ɝ; an ER followed by a vowel is written with a linking ɹ.
    """
    phones = arpa.split() if isinstance(arpa, str) else list(arpa)  # accept either a string or a list of symbols
    out = []  # IPA pieces in order
    prev_vowel = False  # whether the previous symbol was a vowel
    for k, p in enumerate(phones):  # walk the symbols with their index
        # split e.g. "AH0" into the name "AH" and the stress digit "0" (the digit is optional)
        m = re.fullmatch(r"([A-Z]+)([012]?)", p)  # None for a symbol of any other shape, e.g. "ah0" or "AH3"
        if m is None:  # not capital letters with an optional 0, 1 or 2
            raise ValueError(f"malformed ARPAbet symbol {p!r}")  # the same error type as an unknown name below
        base, stress = m.groups()
        if base in ARPA_VOWEL:  # a vowel symbol: it may carry a stress digit, and the stress mark must go before it
            mark = {"1": "ˈ", "2": "ˌ"}.get(stress, "")  # 1 is primary stress (ˈ), 2 secondary (ˌ), 0 none
            if prev_vowel and not mark:  # two vowels in a row and the second unstressed
                mark = "."  # syllable break: AO + IH is two vowels (drawing), not the diphthong ɔɪ
            prev_vowel = True  # remember we are inside a vowel run
            v = ARPA_VOWEL[base]  # start from the table's IPA vowel; the two special cases below may replace it
            if base == "AH" and stress == "0":  # unstressed AH is the reduced schwa
                v = "ə"  # ə instead of the strut vowel ʌ
            if base == "ER":  # the r-coloured vowel of "bird" and "butter", whose IPA depends on its stress
                v = "ɚ" if stress == "0" else "ɝ"  # unstressed ɚ as in "butter", stressed ɝ as in "bird"
                nxt = phones[k + 1] if k + 1 < len(phones) else ""  # the next symbol, or "" at the end
                if re.sub(r"\d", "", nxt) in ARPA_VOWEL:  # followed by a vowel (stress digit removed before the lookup)
                    v += "ɹ"  # add a linking ɹ before the next vowel, as in "hurry" (HH ER1 IY0) or "worry"
            out.append(mark + v)  # stress mark (or syllable dot) goes before the vowel, espeak style
        elif base in ARPA_CONS:  # a consonant symbol: no stress, just a one-to-one IPA lookup
            prev_vowel = False  # a consonant ends any vowel run
            out.append(ARPA_CONS[base])  # append the consonant's IPA, e.g. DH becomes ð and NG becomes ŋ
        else:  # neither vowel nor consonant
            # stop on a malformed dictionary entry instead of guessing
            raise ValueError(f"unknown ARPAbet symbol {p!r}")
    return "".join(out)  # one IPA string with no spaces


# Loads the CMU pronouncing dictionary once per process (about 135,000 words) and keeps it in memory.
# Returns {word: [variant, ...]} with each variant a list of ARPAbet symbols. Imported lazily so the
# cmudict package is only needed when the lexicon probe runs.
@lru_cache(maxsize=1)
def cmu() -> dict:  # maxsize=1 is enough: the function takes no arguments
    import cmudict  # the pip package bundling the dictionary
    return cmudict.dict()  # word -> list of ARPAbet variants


# Removes accents from a word: "café" -> "cafe". s is any string; returns it without combining marks.
def strip_accents(s: str) -> str:
    # NFD splits é into e plus a combining acute; the combining marks are dropped
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))


# Finds a word in CMU. word is the lowercase spoken word; alt_keys are extra spellings to try, such as
# the display form with its apostrophe ("i'll" for spoken "ill"). Returns (the key that matched or None,
# list of ARPAbet variants, empty when nothing matched).
def cmu_lookup(word: str, alt_keys=()) -> tuple[str | None, list[list[str]]]:
    """(key used, ARPAbet variants). Tries the word, its accent-free form, then alt_keys (e.g. display forms)."""
    d = cmu()  # the CMU dictionary, loaded on first use and then served from the lru_cache
    for k in [word, strip_accents(word), *alt_keys]:  # the word, its accent-free form, then each alternative
        k = k.lower().replace("’", "'")  # CMU keys are lowercase with straight apostrophes
        if k in d:  # the first spelling CMU knows wins; the remaining alternatives are not tried
            return k, d[k]  # report which spelling matched and its variants
    return None, []  # out of the dictionary


# --------------------------------------------------------------------------- coarse common alphabet

# espeak's stress marks and the stress level each records (1 primary, 2 secondary).
STRESS_MARKS = {"ˈ": 1, "ˌ": 2}
# Two-character IPA units read as one symbol: diphthongs, centring diphthongs, the affricates tʃ and dʒ,
# and ɜ with a rhotic hook. They are tried before single characters while scanning.
_MULTI = sorted(["aɪ", "aʊ", "eɪ", "oʊ", "əʊ", "ɔɪ", "eə", "ɛə", "ɪə", "iə", "ʊə", "tʃ", "dʒ", "ɜ˞"],
                key=len, reverse=True)  # longest first, so a longer unit wins over its prefix
# IPA to coarse symbols. Distinctions that differ between espeak "en" (British) and CMU (American) but
# do not make a different word are merged, so only real mispronunciations add distance.
_COARSE = {
    # vowels: ɑ/ɒ/ɔ merge, ə/ʌ/ɐ/ɚ merge, ɜ/ɝ merge, ᵻ/ɪ merge, e/ɛ merge, a/æ merge, əʊ/oʊ merge;
    # the non-rhotic centring diphthongs (here/care/poor) become the plain vowel because CMU's
    # following R is dropped before a consonant or word end
    "ɑ": "O", "ɒ": "O", "ɔ": "O", "ə": "@", "ʌ": "@", "ɐ": "@", "ɚ": "@", "ɘ": "@", "ɵ": "@",
    "ɜ": "3", "ɝ": "3", "ɜ˞": "3", "ɪ": "I", "ᵻ": "I", "ɨ": "I", "i": "i", "e": "E", "ɛ": "E",
    "eə": "E", "ɛə": "E", "ɪə": "I", "iə": "I", "a": "a", "æ": "a", "ʊ": "U", "ʊə": "U", "u": "u",
    "o": "oU", "oʊ": "oU", "əʊ": "oU", "aɪ": "aI", "aʊ": "aU", "eɪ": "eI", "ɔɪ": "oI", "y": "i",
    # consonants
    "ɡ": "g", "g": "g", "ɹ": "r", "r": "r", "ɾ": "t", "ʔ": "t", "ɫ": "l", "ɬ": "l", "l": "l",
    "ʍ": "w", "tʃ": "C", "dʒ": "J", "x": "k", "ç": "h", "ɲ": "n",
}
# coarse vowel symbols: used for stress placement, syllable counting and r-dropping
VOWELS = {"O", "@", "3", "I", "i", "E", "a", "U", "u", "oU", "aI", "aU", "eI", "oI"}
# length marks, palatalization, hyphen, apostrophe, nasal tilde, rhotic hook and punctuation are ignored
_DROP = set("ːˑʲ-'̃˞") | set(".,;:!?\"()")


# Reduces an IPA string to the coarse alphabet used by every comparison in this file. ipa is espeak output
# or an IPA string made by arpabet_to_ipa(). Returns (symbols, stress per symbol) as two equal-length
# lists, stress being 1, 2 or 0 and only ever non-zero on vowels.
def coarse(ipa: str) -> tuple[list[str], list[int]]:
    """IPA -> (coarse symbols, stress per symbol: 1 primary, 2 secondary, 0 none).

    Spaces are word boundaries (used for ɹ dropping) and are not symbols. ɹ is dropped before a
    consonant or a word end; a syllabic consonant (n̩) becomes ə + consonant.
    """
    # NFD splits precomposed characters so combining marks can be handled one by one
    s = unicodedata.normalize("NFD", ipa)
    syms, stress, bound = [], [], []  # coarse symbols, their stress, and whether each ends a word
    pending = 0  # stress mark seen but not yet attached to a vowel
    i = 0  # scan position in s
    while i < len(s):  # a manual index loop, because a two-character unit moves the position on by two
        c = s[i]  # the character at the scan position, after NFD decomposition
        if c in STRESS_MARKS:  # ˈ or ˌ: espeak writes it before the syllable, so it is held for the next vowel
            pending = pending or STRESS_MARKS[c]  # remember it until the next vowel; the first mark wins if two appear
            i += 1  # marks are not symbols themselves
            continue  # on to the character after the mark
        if c.isspace():  # a space separates two words; it is not a sound but marks a word boundary
            if bound:  # at least one symbol exists
                bound[-1] = True  # mark the previous symbol as the end of a word
            i += 1  # step past the space; nothing is appended for it
            continue  # continue scanning after it
        if c == "̩":  # syllabic mark on the previous consonant
            if syms and syms[-1] not in VOWELS:  # previous symbol is a consonant (a syllabic n or l)
                # insert a schwa before it, unstressed and not a word end, so "button" n̩ compares to CMU AH0 N
                syms.insert(len(syms) - 1, "@"); stress.insert(len(stress) - 1, 0); bound.insert(len(bound) - 1, False)
            i += 1  # skip the syllabic mark
            continue  # resume with the following character
        if c in _DROP or unicodedata.combining(c):  # dropped marks and any other combining character
            i += 1  # step over the ignored character without recording any symbol
            continue  # back to the top of the loop for the next character
        # take a two-character unit if one starts here, else the single character
        tok = next((m for m in _MULTI if s.startswith(m, i)), c)
        i += len(tok)  # advance past what was consumed
        sym = _COARSE.get(tok, tok)  # map to the coarse symbol; anything not in the table stays as itself
        syms.append(sym)  # add the coarse symbol; its word-end and stress entries are appended next
        bound.append(False)  # word-end status is filled in later
        if sym in VOWELS:  # vowels take the pending stress
            # a vowel takes the waiting stress level, then the pending mark is reset to none
            stress.append(pending); pending = 0
        else:  # consonants never carry stress
            stress.append(0)  # stress 0 for the consonant
    if bound:  # at the end of the string, provided at least one symbol was produced
        bound[-1] = True  # so its last symbol ends a word
    # drop ɹ before a consonant or at a word end
    out_s, out_t = [], []
    for k, sym in enumerate(syms):  # walk the symbols with their index
        # an r at a word end or before a consonant (the non-rhotic "en" voice never says it there)
        if sym == "r" and (bound[k] or (k + 1 < len(syms) and syms[k + 1] not in VOWELS)):
            continue  # leave it out, so British and American readings of "door" match
        out_s.append(sym); out_t.append(stress[k])  # keep the symbol and its stress
    # ɪə-type sequences across a syllable boundary (idea, theatre, real) read the same as the diphthong
    s2, t2 = [], []
    for sym, st in zip(out_s, out_t):  # walk the r-reduced symbols
        if sym == "@" and s2 and s2[-1] == "i" and st == 0:  # an unstressed schwa right after i
            s2[-1] = "I"  # becomes the single vowel ɪ, matching the centring diphthong ɪə
            continue  # the schwa itself is dropped
        s2.append(sym); t2.append(st)  # every other symbol is copied unchanged with its stress level
    return s2, t2  # coarse symbols and their stress levels


# substitutions between these count half: unstressed reduction (ə~ɪ, i~ɪ, u~ʊ) and the
# lexical-set splits between British and American English (STRUT/LOT ʌ~ɒ, BATH a~ɑː, GOAT/LOT, NURSE)
# Vowel pairs whose substitution costs only half: reduced vowels and British/American vowel splits.
NEAR = {frozenset(p) for p in [("i", "I"), ("u", "U"), ("@", "I"), ("@", "O"), ("a", "O"), ("@", "a"),
                                # NURSE vowel vs schwa, schwa vs ʊ, and the GOAT/LOT and GOAT/schwa pairs
                                ("3", "@"), ("@", "U"), ("oU", "O"), ("oU", "@")]}


# Cost of replacing coarse symbol x with y in the weighted edit distance: 0 when equal, 0.5 for a NEAR
# pair, 1 otherwise.
def _sub_cost(x, y) -> float:
    if x == y:  # identical coarse symbols, e.g. both @ after the schwa-like vowels were merged
        return 0.0  # an exact match adds nothing to the edit distance
    return 0.5 if frozenset((x, y)) in NEAR else 1.0  # half for a near pair, full otherwise


# Weighted edit distance between two symbol lists a and b, with the alignment that produced it.
# Insertions and deletions cost 1, substitutions use _sub_cost(). Returns (total cost as a float,
# list of (index in a, index in b) pairs for symbols aligned to each other).
def levenshtein(a, b):
    """Weighted Levenshtein over symbol lists -> (cost, aligned index pairs (i, j))."""
    n, m = len(a), len(b)  # lengths of the two sequences
    D = [[0.0] * (m + 1) for _ in range(n + 1)]  # (n+1) x (m+1) table of best costs for prefixes
    for i in range(1, n + 1):  # fill column 0: turning each prefix of a into an empty sequence
        D[i][0] = float(i)  # turning i symbols into nothing costs i deletions
    for j in range(1, m + 1):  # fill row 0: building each prefix of b from an empty sequence
        D[0][j] = float(j)  # building j symbols from nothing costs j insertions
    for i in range(1, n + 1):  # fill the table row by row
        for j in range(1, m + 1):  # each prefix length of b within the current row
            # cheapest of deleting a[i-1], inserting b[j-1] or substituting one for the other
            D[i][j] = min(D[i - 1][j] + 1, D[i][j - 1] + 1, D[i - 1][j - 1] + _sub_cost(a[i - 1], b[j - 1]))
    pairs, i, j = [], n, m  # trace back from the bottom-right corner
    while i > 0 and j > 0:  # until either sequence is used up
        if D[i][j] == D[i - 1][j - 1] + _sub_cost(a[i - 1], b[j - 1]):  # the diagonal step explains this cell
            pairs.append((i - 1, j - 1)); i -= 1; j -= 1  # those two symbols are aligned; step diagonally
        elif D[i][j] == D[i - 1][j] + 1:  # otherwise a deletion explains it
            i -= 1  # a[i-1] was deleted, so move back one row only
        else:  # otherwise it was an insertion
            j -= 1  # b[j-1] was inserted, so move back one column only
    return D[n][m], pairs[::-1]  # total cost and the alignment in forward order


# Normalized coarse distance between two IPA strings: 0.0 means the same sounds, 1.0 means nothing in
# common. Edit cost is divided by the longer length so long words are not penalized; rounded to 4 places.
def distance(ipa_a: str, ipa_b: str) -> float:
    a, _ = coarse(ipa_a)  # coarse symbols of the first string (stress ignored)
    b, _ = coarse(ipa_b)  # coarse symbols of the second
    if not a and not b:  # neither string has a phoneme symbol, e.g. two empty espeak results
        return 0.0  # treat them as equal and avoid dividing by a zero length
    return round(levenshtein(a, b)[0] / max(len(a), len(b)), 4)  # cost over the longer length


# Indexes of the primary-stressed vowels in a coarse symbol list (syms, st from coarse()). Falls back to
# secondary stress when no primary exists; returns [] when the word has no stress marks at all.
def _primaries(syms, st):
    """Symbol indexes of the primary-stressed vowels (all ˈ; if none, all ˌ)."""
    for want in (1, 2):  # try primary first, then secondary
        ks = [k for k, (s, x) in enumerate(zip(syms, st)) if s in VOWELS and x == want]  # vowels with that stress level
        if ks:  # at least one found
            return ks  # the stressed vowel positions at the first level that has any
    return []  # no ˈ or ˌ anywhere, so callers treat the stress check as not applicable


# The first primary-stressed vowel index (see _primaries), or None when nothing is stressed.
def _primary(syms, st):
    ks = _primaries(syms, st)  # positions of every primary vowel, or of the secondary ones when no primary exists
    return ks[0] if ks else None  # first one, or None


# 0-based syllable number of the vowel at index k in syms (the count of vowels before it); None stays None.
def syllable_of(syms, k):
    return None if k is None else sum(1 for s in syms[:k] if s in VOWELS)  # each vowel before k is one earlier syllable


# Checks whether espeak stresses the same syllable as one CMU variant. Takes two IPA strings, the espeak
# reading and one CMU variant. Returns a bool; True when the comparison does not apply.
def stress_match(ipa_espeak: str, ipa_cmu: str) -> bool:
    """True when espeak's primary stress lands on CMU's primary-stressed syllable (n/a -> True).

    Several primaries are allowed on either side (CMU compounds such as outside AW1 T S AY1 D;
    espeak gives each hyphen part its own primary): at least one espeak primary must fall on a CMU
    primary and every espeak primary must fall on a syllable CMU stresses (primary or secondary).
    Syllables are matched through the phoneme alignment, falling back to the syllable index when the
    stressed vowel has no counterpart.
    """
    a, ta = coarse(ipa_espeak)  # espeak symbols and stress levels
    b, tb = coarse(ipa_cmu)  # CMU symbols and stress levels
    # a one-syllable word on either side has no stress placement to get wrong
    if sum(s in VOWELS for s in a) < 2 or sum(s in VOWELS for s in b) < 2:
        return True  # report a match, so words of one syllable are never flagged for stress
    pa, pb = _primaries(a, ta), _primaries(b, tb)  # primary-stressed vowel positions on each side
    if not pa or not pb:  # one side has no stress marks
        return True  # cannot be judged, so it passes
    # every CMU vowel with primary or secondary stress
    stressed_b = [k for k, (s, x) in enumerate(zip(b, tb)) if s in VOWELS and x]
    amap = dict(levenshtein(a, b)[1])  # espeak index -> CMU index from the phoneme alignment
    syl = lambda ks: {syllable_of(b, k) for k in ks}  # noqa: E731
    # at least one espeak primary on a CMU primary, and none on an unstressed CMU syllable
    hit_primary, all_stressed = False, True
    for k in pa:  # each espeak primary vowel
        if k in amap:  # the vowel was aligned to a CMU symbol
            on_primary, on_stressed = amap[k] in pb, amap[k] in stressed_b  # check that symbol directly
        else:  # no counterpart in the alignment
            s = syllable_of(a, k)  # its syllable number in the espeak reading
            on_primary, on_stressed = s in syl(pb), s in syl(stressed_b)  # compare by syllable number instead
        hit_primary |= on_primary  # any espeak primary hitting a CMU primary is enough
        all_stressed &= on_stressed  # every espeak primary must land on a stressed CMU syllable
    # True only if some espeak primary hit a CMU primary and none fell on an unstressed syllable
    return hit_primary and all_stressed


# Compares one espeak reading with all CMU variants of a word. espeak_ipa is the espeak IPA string,
# cmu_variants_ipa the variants as IPA, prefer_stress picks the best variant by stress first. Returns
# {"distance": smallest distance, "stress_ok": True if any variant's stress matches, "best": index}.
def compare(espeak_ipa: str, cmu_variants_ipa: list[str], prefer_stress: bool = False) -> dict:
    """Min distance over CMU variants and whether any variant has the same primary-stress syllable.

    best = index of the closest variant by (distance, stress mismatch), or by (stress mismatch,
    distance) with prefer_stress (used to tell which reading of a heteronym espeak chose).
    """
    if not cmu_variants_ipa:  # word not in CMU
        return {"distance": None, "stress_ok": None, "best": None}  # nothing to compare: every field is None
    scored = []  # (distance, stress mismatch, index) per variant
    # score every dictionary pronunciation; k keeps its position for the best index
    for k, v in enumerate(cmu_variants_ipa):
        # distance, and True when the stress does NOT match
        scored.append((distance(espeak_ipa, v), not stress_match(espeak_ipa, v), k))
    d = min(x[0] for x in scored)  # distance to the closest variant
    # key=None sorts by distance then mismatch then index; prefer_stress puts the stress test first
    best = min(scored, key=(lambda x: (x[1], x[0], x[2])) if prefer_stress else None)
    # stress is acceptable if any variant matches
    return {"distance": d, "stress_ok": any(not x[1] for x in scored), "best": best[2]}


# The 0-based syllable carrying the primary stress of an IPA string, or None when nothing is stressed.
def stress_syllable(ipa: str):
    s, t = coarse(ipa)  # coarse symbols and stress
    return syllable_of(s, _primary(s, t))  # syllable of the first primary vowel


# --------------------------------------------------------------------------- numbers

# Number words up to nineteen, indexed by value.
_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
         "sixteen seventeen eighteen nineteen").split()  # the second half of the list
# tens words indexed by n // 10; the two placeholders fill slots 0 and 1
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
# ordinals that are not just the cardinal plus "th"
_ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
        "nine": "ninth", "twelve": "twelfth"}


# Spells a whole number below one million the way espeak "en" reads digits, with British "and" after
# hundred and before a final number under 100. n is a non-negative int; returns the words as a string.
# Raises ValueError for one million and above.
def number_words(n: int) -> str:
    """British-style cardinal as espeak en reads it ("two thousand one hundred and five")."""
    if n < 20:  # zero to nineteen each have their own word
        return _ONES[n]  # read straight from the table, e.g. 13 gives "thirteen"
    if n < 100:  # 20 to 99: a tens word with an optional ones word
        # tens word plus a ones word unless the number is a round ten
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:  # 100 to 999: the hundreds word followed by the remainder
        # "x hundred", plus "and" and the remainder unless it is a round hundred
        return _ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " and " + number_words(n % 100))
    if n < 1_000_000:  # 1,000 to 999,999: spell the thousands, then the remainder
        r = n % 1000  # the part below one thousand
        # "and" before a remainder under 100 ("one thousand and five"), a plain space otherwise
        tail = "" if r == 0 else (" and " if r < 100 else " ") + number_words(r)
        return number_words(n // 1000) + " thousand" + tail  # "x thousand" plus the spelled remainder
    raise ValueError("number too large")  # larger numbers are not supported


# Spells a number token as words: "1,250" -> "one thousand two hundred and fifty", "21st" -> "twenty
# first". tok is the normalized token text; returns None when the token is not a plain number, or when it is one
# million or more (number_words stops there), such as a date written 20261031 or a long reference number.
def spell_number(tok: str) -> str | None:
    # digits with optional commas and an optional ordinal suffix
    m = re.fullmatch(r"(\d[\d,]*)(st|nd|rd|th)?", tok.lower())
    if not m:  # not a number token
        return None  # None marks the token as unspellable, so it adds no number words to the vocabulary
    n = int(m.group(1).replace(",", ""))  # the integer, without its commas
    if n >= 1_000_000:  # too large to spell: unspellable like any other odd token, instead of stopping the measurement
        return None  # the same None as above: no number words are added for it
    words = number_words(n).split()  # the integer spelled, as a list of words
    if m.group(2):  # an ordinal suffix was present
        w = words[-1]  # only the last word changes
        # irregular ordinal, or -y to -ieth (twenty -> twentieth), or append "th"
        words[-1] = _ORD.get(w) or (w[:-1] + "ieth" if w.endswith("y") else w + "th")
    return " ".join(words)  # back to one string


# --------------------------------------------------------------------------- heteronyms

# Words with two readings that espeak may confuse; each occurrence is checked against its sentence.
# Duplicates in the literal (lives, rebel, close) are harmless in a set.
HETERONYMS = {
    "read", "lead", "leads", "live", "lives", "wind", "winds", "tear", "tears", "close", "closes", "use", "uses",
    "used", "present", "presents", "record", "records", "object", "objects", "content", "minute", "minutes",
    "wound", "bow", "bows", "desert", "does", "house", "houses", "refuse", "excuse", "abuse", "increase",
    "decrease", "progress", "subject", "perfect", "separate", "estimate", "conduct", "project", "produce",
    "permit", "contract", "conflict", "address", "attribute", "contrast", "convert", "insult", "invalid",
    "moderate", "polish", "row", "sow", "bass", "dove", "entrance", "graduate", "intimate", "appropriate",
    "associate", "alternate", "approximate", "advocate", "duplicate", "elaborate", "deliberate", "aged",
    "blessed", "learned", "beloved", "crooked", "job", "rebel", "reject", "resume", "sewer", "wicked",
    "lives", "invite", "combat", "compound", "discount", "export", "import", "rebel", "upset", "close",
}
# Block kinds treated as headings; their capital letters say nothing about proper names.
HEADING_KINDS = {"cover", "h1", "h2", "th"}

# Which CMU variant is the verb reading, for noun/adjective-vs-verb heteronyms.
# Heteronyms whose verb reading ends in a z sound where the noun has s (to use vs a use).
_Z_VERB = {"use", "uses", "used", "close", "closes", "excuse", "abuse"}
# -ate words whose verb reading has a full "ate" (EY T) and the adjective or noun a reduced vowel
_ATE_VERB = {"separate", "estimate", "moderate", "graduate", "associate", "appropriate", "alternate",
             "approximate", "advocate", "duplicate", "elaborate", "deliberate", "intimate"}
# Words whose verb reading stresses a later syllable (to reCORD vs a REcord); the first vowel decides.
_STRESS_VERB = {"present", "presents", "record", "records", "object", "objects", "content", "contents", "conduct",
                "project", "produce", "permit", "contract", "conflict", "convert", "insult", "increase",
                "increases", "decrease", "progress", "subject", "perfect", "refuse", "reject", "rebel",
                "contrast", "import", "export", "transfer", "transfers", "research", "discount", "upset", "combat"}
_IH_VERB = {"live", "lives"}  # verb with short i (to live, he lives) vs long i (a live wire, nine lives)
# Words that, right before a heteronym, point to a noun or adjective reading ("the record", "a close").
_DET = {"a", "an", "the", "these", "those", "my", "your", "his", "her", "our", "their", "its",
        "every", "each", "no", "any", "some", "first", "next", "same", "good", "real", "very", "so", "too",
        "more", "most", "of", "for", "in", "on"}
# Words that, right before a heteronym, point to a verb reading ("to record", "we object").
_VERB_BEFORE = {"to", "not", "you", "i", "we", "they", "he", "she", "can", "cannot", "will", "would", "should",
                "must", "could", "may", "might", "please", "dont", "never", "who", "whoever", "also", "always",
                "often", "people", "anyone", "someone", "nobody", "everyone"}
_VERB_BEFORE_WEAK = {"and", "or", "then"}  # only with an object-like next word
# Words that, right after a heteronym at a clause start, suggest an imperative ("Record the ...").
_IMPERATIVE_NEXT = {"the", "a", "an", "it", "them", "this", "that", "these", "those", "your", "my", "his", "her",
                    "our", "their", "everything", "few", "what", "up", "out", "all", "one", "every", "each", "some"}


# Tells which reading a CMU variant of a heteronym is. word is the heteronym and arpabet one of its CMU
# variants as a space-separated string. Returns True for the verb reading, False for the noun or
# adjective reading, and None when the word is not in any of the rule sets above.
def verb_reading(word: str, arpabet: str):
    """True if this CMU variant is the verb reading of a noun/adjective-vs-verb heteronym, None if unknown."""
    ph = arpabet.split()  # list of ARPAbet symbols
    if word in _Z_VERB:  # use, close, excuse and similar: the verb ends in a z sound, the noun in an s
        tail = [p for p in ph if not p[-1].isdigit()][-2:]  # the last two consonants of the variant
        return "Z" in tail or (ph[-1] == "D" and "Z" in ph[-3:])  # a z there, or a final D shortly after a z ("used")
    if word in _ATE_VERB:  # separate, estimate and similar: the verb ends in a full EY T
        return ph[-2:] in (["EY2", "T"], ["EY1", "T"])  # verb when the word ends in a stressed "ate" (EY T)
    if word in _IH_VERB:  # live and lives: the verb has the short stressed i, IH1
        return "IH1" in ph  # verb when the short stressed i (IH1) is present
    if word in _STRESS_VERB:  # record, present and similar: the verb moves the stress off the first vowel
        vow = [p for p in ph if p[-1].isdigit()]  # the vowels in order
        return bool(vow) and not vow[0].endswith("1")  # verb when the first vowel is not the primary-stressed one
    return None  # the word belongs to no rule set, so this variant cannot be classified


# The heteronyms whose CMU variants verb_reading() can tell apart: the only words a role rule can be checked for.
ROLE_WORDS = frozenset(_Z_VERB | _ATE_VERB | _IH_VERB | _STRESS_VERB)
# Rule set 2 only: words right after a clause-initial heteronym that point to a noun subject ("Record of ...").
_NOUN_NEXT = {"of", "is", "was", "are", "were", "and", "or", "to", "in", "for", "with", "that", "has", "have"}
# Rule set 2 only: words right after the heteronym that open its object ("costs increase the pressure").
_OBJECT_NEXT = {"the", "a", "an", "your", "my", "his", "her", "our", "their", "its", "this", "that", "these", "those",
                "every", "each", "some", "few"}


# Guesses whether a heteronym occurrence should be read as a verb or as a noun/adjective from its neighbours, and says
# why. words are the sentence piece's normalized words, k the heteronym's index, word the heteronym and punct_after a
# list of booleans marking clause punctuation after each word. version 1 is the original rule set (what the lexicon
# probe records as expected_role); version 2 adds two rules for ROLE_WORDS where version 1 has no opinion:
#   A  a clause-initial imperative ("Use pencils ...", "...: record."), unless a noun subject's word follows
#   B  a plural or bare-noun subject, then the heteronym, then a determiner ("costs increase the pressure")
# Returns (role, reason): role is "verb", "noun-adj" or None when the neighbours give no clear signal; reason is a
# short text naming the rule that decided. Raises ValueError for any other version.
def explain_role(words: list[str], k: int, word: str, punct_after=None, version: int = 1):
    """(role, reason): 'verb' / 'noun-adj' / None from the neighbouring words (a heuristic for triage, not a parser)."""
    if version not in (1, 2):  # only the two published rule sets exist
        raise ValueError(f"unknown role rule version {version!r} (use 1 or 2)")  # a typo must not silently mean 1
    punct_after = punct_after or [False] * len(words)  # with no punctuation information, assume no clause breaks
    prev = words[k - 1] if k > 0 else None  # the word before, if any
    nxt = words[k + 1] if k + 1 < len(words) else None  # the word after, if any
    # first word of the sentence piece, or right after a comma, semicolon or full stop
    clause_start = k == 0 or punct_after[k - 1]
    if word in ("close", "closes") and nxt == "to":  # "close to" is the adjective (near), never the verb
        return "noun-adj", "v1: 'close to' means near"  # the adjective reading with the s sound
    if prev in _DET and not clause_start:  # a determiner or similar word right before, inside a clause
        return "noun-adj", f"v1: '{prev}' before it"  # noun or adjective reading
    if prev in _VERB_BEFORE and not clause_start:  # a pronoun, modal, "to" or similar right before, inside a clause
        return "verb", f"v1: '{prev}' before it"  # a pronoun, modal or "to" just before the word suggests the verb
    # no break after the word, an object-like word follows, and it begins a clause (or follows and/or/then)
    if not punct_after[k] and nxt in _IMPERATIVE_NEXT and (clause_start or prev in _VERB_BEFORE_WEAK):
        # looks like an imperative or a coordinated verb; the reason names the word that made it look so
        return "verb", f"v1: '{nxt}' after it " + ("at a clause start" if clause_start else f"after '{prev}'")
    if version == 2 and word in ROLE_WORDS:  # the two extra rules, only for words whose readings can be told apart
        # A. clause-initial imperative: nothing follows, a clause break follows, or no noun subject's word follows
        if clause_start and (nxt is None or punct_after[k] or nxt not in _NOUN_NEXT):
            return "verb", "v2-A: clause-start imperative"  # "Use pencils ...", "Separate them ..."
        # B. a determiner follows inside the clause, and the word before is not a determiner itself
        if nxt in _OBJECT_NEXT and not punct_after[k] and prev not in _DET:
            return "verb", f"v2-B: a subject, then '{nxt}' opens its object"  # "costs increase the pressure"
    return None, "no signal from the neighbouring words"  # this occurrence is not judged


# Guesses whether a heteronym occurrence should be read as a verb or as a noun/adjective from its
# neighbours (explain_role() without the reason). words are the sentence piece's normalized words, k the
# heteronym's index, word the heteronym and punct_after a list of booleans marking clause punctuation after
# each word; version picks the rule set (1, the default, is what the lexicon probe uses).
# Returns "verb", "noun-adj" or None when the neighbours give no clear signal.
def expected_role(words: list[str], k: int, word: str, punct_after=None, version: int = 1):
    """'verb' / 'noun-adj' from the neighbouring words (a crude heuristic for triage, not a parser).

    words: normalized words of the sentence piece; punct_after[i]: the spoken token ends after word i
    with , ; : . ! or ? (a clause boundary). version 1 (default) is the original rule set, 2 adds two rules.
    """
    return explain_role(words, k, word, punct_after, version)[0]  # the role alone


# The reading one CMU variant of a heteronym stands for, in the same terms explain_role() and the tagger use: "verb"
# or "noun-adj" for a ROLE_WORDS heteronym, "past" (R EH1 D) or "present" (R IY1 D) for "read", None for any other
# word or an unknown variant. word is the heteronym and arpabet one variant as a space-separated string.
def variant_role(word: str, arpabet: str):
    """'verb' / 'noun-adj' for role heteronyms, 'past' / 'present' for read, else None."""
    if word == "read":  # the tense heteronym: both readings are verbs
        vowels = [p for p in arpabet.split() if p[-1].isdigit()]  # its vowels; "read" has exactly one
        return {"EH1": "past", "IY1": "present"}.get(vowels[0] if vowels else "")  # red is past, reed present
    vr = verb_reading(word, arpabet)  # True for the verb variant, False for the other, None when unknown
    return None if vr is None else ("verb" if vr else "noun-adj")  # the same labels the lexicon probe stores


# Which reading espeak gave one heteronym occurrence in its sentence. word is the heteronym, ctx_ph its in-context
# phonemes (from context_phonemes) and variants its CMU variants as ARPAbet strings. The variant nearest to ctx_ph is
# chosen with stress compared first, exactly as the lexicon probe does. Returns (that variant or None, its role from
# variant_role() or None); empty phonemes or no variants give (None, None).
def context_reading(word: str, ctx_ph: str, variants: list[str]):
    """(nearest CMU variant, its role) for an occurrence's in-context phonemes."""
    if not ctx_ph or not variants:  # nothing was heard for it, or the dictionary does not know it
        return None, None  # no reading can be told
    best = compare(ctx_ph, [arpabet_to_ipa(v) for v in variants], prefer_stress=True)["best"]  # stress decides first
    used = variants[best] if best is not None else None  # the variant espeak's reading is closest to
    return used, (variant_role(word, used) if used else None)  # the variant and the reading it stands for

# --------------------------------------------------------------------------- in-context phonemes


# Characters that are not phonemes in espeak output: spaces, punctuation and hyphens.
_PUNCT = set(" .,;:!?\"()-")


# Keeps only phoneme characters of an espeak string ph, each with its index in ph, as (char, index) pairs.
# Stress marks and punctuation are skipped. The owner parameter is unused.
def _seq(ph: str, owner=None):
    """Phoneme chars without spaces/punctuation/stress, with their index in ph."""
    # filter out punctuation, spaces and stress marks
    return [(c, k) for k, c in enumerate(ph) if c not in _PUNCT and c not in STRESS_MARKS]


# Splits a whole sentence piece's phonemes among its words. espeak reads a word differently in a sentence
# (weak forms, heteronym choice), so the piece is phonemized as the narration did and divided here.
# piece_ph is the piece's IPA string; word_ph the isolated IPA of each word in order. Returns one IPA string
# per word (stress marks kept), "" for a word that got no characters.
def context_phonemes(piece_ph: str, word_ph: list[str]) -> list[str]:
    """Split a sentence piece's phoneme string among its words by aligning it with the words' isolated phonemes."""
    owner_tok = []  # word index of each isolated phoneme character
    tok_chars = []  # all isolated phoneme characters, concatenated
    for w, ph in enumerate(word_ph):  # each word with its isolated phonemes
        for c, _ in _seq(ph):  # each phoneme character of this word read alone, punctuation and stress marks dropped
            tok_chars.append(c); owner_tok.append(w)  # remember the character and which word it belongs to
    piece = _seq(piece_ph)  # phoneme characters of the whole piece with their positions
    pchars = [c for c, _ in piece]  # just the characters, for the matcher
    owner = [None] * len(piece)  # owner word of each piece character, unknown so far
    # character alignment of words-in-isolation against the piece; autojunk off so common phonemes are not ignored
    sm = SequenceMatcher(None, tok_chars, pchars, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():  # each aligned block: equal, replace, insert or delete
        if tag in ("equal", "replace"):  # matching or substituted characters
            for k in range(j1, j2):  # each piece character in the block
                # proportional position inside the block on the isolated side
                ti = i1 + ((k - j1) * (i2 - i1)) // max(j2 - j1, 1)
                owner[k] = owner_tok[min(ti, i2 - 1)]  # borrow the owner of that isolated character
        elif tag == "insert":  # characters only the sentence reading has
            # attach them to the word just before the gap (or the first word at the very start)
            ref = i1 - 1 if i1 > 0 else i1
            for k in range(j1, j2):  # each phoneme character that only the sentence reading has
                # owner of the reference character, falling back to word 0
                owner[k] = owner_tok[ref] if 0 <= ref < len(owner_tok) else 0
    # keep owners monotone
    for k in range(1, len(owner)):  # owners must never go backwards through the sentence
        # a character assigned to an earlier word than its predecessor
        if owner[k] is not None and owner[k - 1] is not None and owner[k] < owner[k - 1]:
            owner[k] = owner[k - 1]  # takes the predecessor's word instead
    out = [""] * len(word_ph)  # one phoneme string per word
    pos_owner = {}  # piece index -> owner word
    for (c, idx), w in zip(piece, owner):  # pair each phoneme character position with its owner
        pos_owner[idx] = w  # store by position in piece_ph
    pend = ""  # stress marks waiting for the next owned character
    for idx, c in enumerate(piece_ph):  # walk the original piece string, stress marks included
        if c in STRESS_MARKS:  # ˈ or ˌ has no owner of its own; it belongs to the next phoneme character
            pend += c  # keep it until the next phoneme character
            continue  # go to the next character without assigning the mark yet
        if idx in pos_owner and pos_owner[idx] is not None:  # a phoneme character with a known owner
            out[pos_owner[idx]] += pend + c  # append the pending marks and the character to that word
            pend = ""  # marks are used up
    return out  # one in-context IPA string per word, in the same order as word_ph


# --------------------------------------------------------------------------- the probe


# Returns the key with the largest count in d (a {value: count} dict), or default when d is empty.
# Used to pick a word's most common spoken surface form; the name says "first" but it means "most frequent".
def _first_or(d, default=None):
    # max by count; ties go to the first key in insertion order
    return max(d.items(), key=lambda kv: kv[1])[0] if d else default


# Runs the whole dictionary probe for one narration project. karaoke_root is the project folder (the one
# holding work/), inv an inventory already built by inventory.build() or None to build it here. Returns the
# lexicon measurement dict: per-word rows, flagged words, review list, display mismatches, heteronyms,
# numbers and totals. Side effects: reads the project's JSON files and starts the espeak helper once (two
# voices); it writes nothing, the callers (main, measure.lexicon_metrics) save the result.
def measure(karaoke_root=inventory.DEFAULT_KARAOKE, inv: dict | None = None) -> dict:
    # reuse the caller's inventory or build one from request.json, docmodel.json and the timing records
    inv = inv or inventory.build(karaoke_root)
    # rows: one per spoken token; unique: word -> counts and tokens; sentences: segment -> sentence piece texts
    rows_inv, uniq, sentences = inv["rows"], inv["unique"], inv["sentences"]

    # ---- numbers -> spelled words
    numbers = {}
    for w, u in uniq.items():  # every unique normalized word
        if any(c.isdigit() for c in w):  # contains a digit, so it is a number token such as "1914" or "21st"
            # how often it occurs, its spelled-out words (None if unparseable) and where it occurs
            numbers[w] = {"count": u["count"], "spelled": spell_number(w), "tokens": u["tokens"]}

    # ---- vocabulary: letter words + spelled number words
    vocab = {}
    for w, u in uniq.items():  # every unique word again
        if w in numbers:  # numbers are handled separately
            continue  # number tokens stay out of the word vocabulary; their spelled words are added below
        # occurrences in the text, occurrences via spelled numbers (filled below), and token ids
        vocab[w] = {"count": u["count"], "from_numbers": 0, "tokens": list(u["tokens"]),
                    # display forms and spoken surface forms with their counts
                    "displays": u["displays"], "surface": u["surface"]}
    for n, info in numbers.items():  # go through the number tokens to add the words they are spoken as
        for sw in (info["spelled"] or "").split():  # each word of its spelled form
            # add the number word to the vocabulary if the text never used it
            v = vocab.setdefault(sw, {"count": 0, "from_numbers": 0, "tokens": [], "displays": {}, "surface": {}})
            v["from_numbers"] += info["count"]  # it is heard as often as the number occurs

    # ---- display forms that the spoken text changed (apostrophes dropped: I’ll -> Ill)
    disp_occ = defaultdict(list)
    for r in rows_inv:  # every spoken token in the inventory, to find display forms the spoken text altered
        if len(r["norm"]) != 1:  # only single-word tokens can be compared with their display form
            continue  # a token that splits into several words (or none) has no single display form to compare
        # display text without edge punctuation, lowercase, curly apostrophe straightened
        dn = inventory.clean_display(r["display"]).lower().replace("’", "'")
        # letters and apostrophes only, same letters as the spoken word, and an apostrophe was dropped
        if re.fullmatch(r"[a-z']+", dn) and dn.replace("'", "") == r["norm"][0] and "'" in dn:
            # group token ids by (display form, spoken form), e.g. ("i'll", "ill")
            disp_occ[(dn, r["norm"][0])].append(r["token"])
    # spoken words that are contractions; possessives ending in 's are not counted
    contraction = {n for (dn, n) in disp_occ if not dn.endswith("'s")}

    # ---- proper names: never lowercase, and capitalized mid-sentence in body text or in a
    #      capitalized possessive (Elisha’s); single letters, acronyms and contractions excluded
    cap_mid, lower, upper = Counter(), Counter(), Counter()
    for r in rows_inv:  # scan every spoken token again, this time to tally how each word is capitalised
        # each word with its normalized form; k is its position inside the token
        for k, (s, n) in enumerate(zip(r["words"], r["norm"])):
            if not s[:1].isalpha():  # skip words that do not start with a letter
                continue  # digits or a leading apostrophe say nothing about capitalisation
            if s[:1].islower():  # seen in lowercase at least once
                lower[n] += 1  # so it is an ordinary word somewhere
            elif len(s) > 1 and s.isupper():  # all capitals and longer than one letter: an acronym or a shouted heading
                upper[n] += 1  # counted separately so it does not look like a name
            # capitalized in body text, not the first word of a sentence piece and not the first token of a block
            elif r["block_kind"] not in HEADING_KINDS and not (r["pos"] == 0 and k == 0) and not r["first_in_block"]:
                cap_mid[n] += 1  # a capital that only a proper name explains
    # base names of capitalized possessives such as "Elisha’s", lowercased
    poss = {re.sub(r"['’]s$", "", inventory.clean_display(d)).lower()
            # taken from every display form of every word that has that shape
            for v in vocab.values() for d in v["displays"] if re.fullmatch(r"[A-Z][a-z]+['’]s", inventory.clean_display(d))}
    poss |= {p + "s" for p in poss}  # also the apostrophe-free spoken form ("elishas")
    # a name: capitalized mid-sentence or possessive, never lowercase, never all caps
    proper = {w for w in vocab if (cap_mid[w] or w in poss) and not lower[w] and not upper[w]
              # and not a single letter, a contraction or the pronoun "I"
              and len(w) > 1 and w not in contraction and w != "i"}

    # ---- one espeak call: words (as spoken, most common surface), numbers, spelled numbers,
    #      display forms, all sentence pieces and every surface word (for in-context alignment)
    # The form the voice actually got for each word (a respelling where an override applies), its most
    # frequent one; falls back to the word itself for spelled number words that have no surface form.
    surf_of = {w: _first_or(v["surface"], w) for w, v in vocab.items()}
    # (segment, piece index, piece text) for every sentence piece in the book
    pieces = [(sid, k, p) for sid, ps in sentences.items() for k, p in enumerate(ps)]
    # every distinct string the voice was given for a word, overrides included
    all_surfaces = sorted({s for r in rows_inv for s in r.get("voiced", r["words"]) if s})
    # Everything that must be phonemized, in one set: surface forms, normalized words, number tokens,
    # their spelled forms, display forms with apostrophes, every sentence piece and every surface word.
    texts = sorted(set(surf_of.values()) | set(vocab) | set(numbers) |
                   {i["spelled"] for i in numbers.values() if i["spelled"]} |  # spelled forms of the numbers
                   # display forms, whole sentence pieces and all surface words
                   {dn for dn, _ in disp_occ} | {p for _, _, p in pieces} | set(all_surfaces))
    # one helper run for both the narration voice and the American reference voice
    ph = espeak(texts, voices=(NARRATION_VOICE, REFERENCE_VOICE))
    en = dict(zip(texts, ph[NARRATION_VOICE]))  # text -> phonemes in the narration voice "en"
    us = dict(zip(texts, ph[REFERENCE_VOICE]))  # text -> phonemes in the reference voice "en-us"

    # ---- in-context phonemes for every occurrence
    by_piece = defaultdict(list)
    for r in rows_inv:  # group inventory rows by sentence piece
        by_piece[(r["seg"], r["sent"])].append(r)  # keyed by (segment id, piece index)
    ctx = defaultdict(Counter)           # word -> Counter(in-context phonemes)
    ctx_tok = {}                         # (token, k) -> in-context phonemes
    for sid, k, ptext in pieces:  # each sentence piece of the book with its segment id, piece index and text
        prow = sorted(by_piece[(sid, k)], key=lambda r: r["pos"])  # its tokens in spoken order
        # (token id, word index inside the token, voiced surface, normalized word)
        words = [(r["token"], j, s, n) for r in prow
                 # words the voice was given; empty entries (the tail of an overridden token) are skipped
                 for j, (s, n) in enumerate(zip(r.get("voiced", r["words"]), r["norm"])) if s]
        if not words:  # a piece with no voiced words
            continue  # nothing voiced in this piece, so there are no words to share its phonemes among
        # split the piece's phonemes among its words using their isolated phonemes
        parts = context_phonemes(en[ptext], [en[s] for _, _, s, _ in words])
        for (tok, j, s, n), pph in zip(words, parts):  # each word with its share of the piece phonemes
            ctx_tok[(tok, j)] = pph  # remember the reading of this exact occurrence
            if n in vocab:  # number tokens are not vocabulary rows, so only their per-occurrence reading is kept
                ctx[n][pph] += 1  # count how often each in-context reading occurs

    # ---- per-word rows
    rows = []
    for w in sorted(vocab):  # one row per vocabulary word, alphabetical
        v = vocab[w]  # the word's counts and forms
        # display forms of this word that carry the apostrophe the spoken text dropped
        alt = [dn for (dn, n) in disp_occ if n == w]
        # CMU key that matched and its pronunciation variants (display forms are tried last)
        key, variants = cmu_lookup(w, alt)
        cmu_ipa = [arpabet_to_ipa(x) for x in variants]  # each variant as IPA in espeak notation
        # espeak readings of the word as voiced, narration voice and reference voice
        e_en, e_us = en.get(surf_of[w], ""), us.get(surf_of[w], "")
        cmp = compare(e_en, cmu_ipa)  # distance and stress check against CMU
        oov = not variants  # out of vocabulary: CMU has no entry
        compound = None  # best two-word split, OOV words only
        if oov:  # CMU does not know the word
            compound = _compound_guess(w)  # maybe it is two dictionary words joined ("doorkeeper")
            if compound:  # a split was found
                # compare espeak's reading with the joined pronunciation
                c2 = compare(e_en, [arpabet_to_ipa(compound["arpabet"])])
                compound.update(distance=c2["distance"], stress_ok=c2["stress_ok"])  # store how close that guess is
        reasons = []  # reasons this word might be mispronounced
        if cmp["distance"] is not None and cmp["distance"] >= FLAG_DISTANCE:  # far from every CMU variant
            # record the distance and the threshold
            reasons.append(f"distance {cmp['distance']:.2f} >= {FLAG_DISTANCE}")
        if cmp["stress_ok"] is False:  # espeak stresses a syllable that no CMU variant stresses
            # record the syllable numbers on both sides
            reasons.append(f"primary stress on syllable {stress_syllable(e_en)}, CMU has "
                           # one syllable number per CMU variant, joined by slashes
                           + "/".join(str(stress_syllable(c)) for c in cmu_ipa))
        if oov:  # CMU has no entry for the word or for any of its alternative spellings
            reasons.append("not in CMUdict")  # recorded as a reason, but sent to review rather than flagged
        is_name = w in proper  # treated as a proper name
        review = oov or is_name  # names and OOV words go to human review: CMU is no authority on them
        flag = bool(reasons) and not review  # flagged only when there is a reason and the word is not for review
        cvar = ctx.get(w, Counter())  # in-context readings of the word
        # the readings that differ from the isolated one
        cdiff = {p: c for p, c in cvar.items() if distance(p, e_en) > 0}
        row = {  # the row stored in the measurement
            # total occurrences, occurrences in the text
            "word": w, "count": v["count"] + v["from_numbers"], "count_text": v["count"],
            # occurrences via numbers, and phonemes in both espeak voices
            "count_numbers": v["from_numbers"], "espeak_en": e_en, "espeak_us": e_us,
            # CMU key, variants as ARPAbet strings and as IPA
            "cmu_key": key, "cmu_arpabet": [" ".join(x) for x in variants], "cmu_ipa": cmu_ipa,
            # closest distance and whether any variant's stress matches
            "distance": cmp["distance"], "stress_ok": cmp["stress_ok"],
            # index of the closest variant and espeak's stressed syllable
            "best_variant": cmp["best"], "stress_syllable_en": stress_syllable(e_en),
            "stress_syllable_cmu": [stress_syllable(c) for c in cmu_ipa],  # stressed syllable of each CMU variant
            # how far the British and American espeak voices differ for this word
            "distance_en_us": distance(e_en, e_us) if e_en and e_us else None,
            # reference only: does the American espeak voice agree with CMU? (if not, CMU may be the outlier)
            "us_vs_cmu": {k: v for k, v in compare(e_us, cmu_ipa).items() if k != "best"} if cmu_ipa else None,
            # CMU has an r (R not before a vowel, or ER) that the non-rhotic "en" phonemes do not carry,
            # so the American voice has to infer it (door and daw are both dɔː in "en")
            "r_dropped": bool(variants) and _has_coda_r(variants[cmp["best"]]) and "ɹ" not in e_en,
            # out of vocabulary, proper name, flagged as a likely error, and sent to human review
            "oov": oov, "proper_name": is_name, "flag": flag, "review": review,
            "reason": "; ".join(reasons) if reasons else None,  # human-readable reasons, or None
            # display forms and token ids, for tracing back into the book
            "displays": v["displays"], "tokens": v["tokens"],
            "context_variants": dict(cvar.most_common()),  # in-context readings, most common first
            # how many occurrences were read differently in context than in isolation
            "context_differs": sum(cdiff.values()),
        }
        if compound:  # an OOV word with a two-word guess
            row["compound_guess"] = compound  # attach it to the row
        rows.append(row)  # add this word's row; rows stay in alphabetical order
    byword = {r["word"]: r for r in rows}  # word -> row, for the later sections

    # ---- display mismatches (spoken text changed the word)
    display_mismatch = []
    for (dn, n), toks in sorted(disp_occ.items()):  # each (display form, spoken form) pair, in a stable order
        sp, dp = en.get(surf_of.get(n, n), ""), en.get(dn, "")  # phonemes of the spoken form and of the display form
        if distance(sp, dp) == 0:  # espeak reads both the same, so the dropped apostrophe does no harm
            continue  # the apostrophe makes no audible difference, so no mismatch is recorded
        key, variants = cmu_lookup(dn)  # CMU entry of the display form ("i'll")
        # the display form's dictionary pronunciations converted to IPA for comparison
        cmu_ipa = [arpabet_to_ipa(x) for x in variants]
        cmp = compare(sp, cmu_ipa)  # how far the spoken reading is from the dictionary reading of the display form
        display_mismatch.append({  # add an entry describing how the dropped apostrophe changed the reading
            # word is the display form; spoken_as is what the voice was given
            "word": dn, "kind": "display_mismatch", "spoken_as": n, "count": len(toks), "tokens": toks,
            # both readings and the CMU variants
            "espeak_en": sp, "espeak_en_display": dp, "cmu_arpabet": [" ".join(x) for x in variants],
            # CMU as IPA, the distance and the stress check
            "cmu_ipa": cmu_ipa, "distance": cmp["distance"], "stress_ok": cmp["stress_ok"],
            "oov": not variants, "flag": True,  # always flagged: the text the voice got differs from the book
            # explanation shown in the report
            "reason": f"spoken text '{n}' drops the apostrophe of '{dn}'; espeak reads {sp} instead of {dp}",
        })

    # ---- numbers: check the spelled form is what espeak says
    num_rows = []
    for n, info in sorted(numbers.items(), key=lambda kv: (len(kv[0]), kv[0])):  # numbers ordered by length, then text
        sp = info["spelled"]  # spelled form, or None
        # espeak's reading of the digits and of the spelled words
        e_num, e_sp = en.get(n, ""), en.get(sp, "") if sp else ""
        # one row per number token
        num_rows.append({"number": n, "count": info["count"], "spelled": sp, "espeak_en": e_num,
                         # distance between the two readings, when a spelled form exists
                         "espeak_en_spelled": e_sp, "spelled_distance": distance(e_num, e_sp) if sp else None,
                         # "and" is reduced (ən) when espeak reads digits, full when it reads the words
                         # close enough to call it the same reading
                         "spelled_matches": bool(sp) and distance(e_num, e_sp) < 0.15,
                         "tokens": info["tokens"]})  # where the number occurs

    # ---- heteronyms, with each occurrence's sentence and in-context reading
    tok_rows = {r["token"]: r for r in rows_inv}  # token id -> inventory row
    het = []  # one record per heteronym found in the book, filled in below
    auto = set()  # words found to be heteronyms from CMU itself
    for r in rows:  # scan every word row for CMU variants that disagree on the stressed syllable
        # CMU variants that put the primary stress on different syllables (transfer, research, impulse)
        # skip OOV words, names, single-variant words, short words and words heard only via numbers
        if r["oov"] or r["proper_name"] or len(r["cmu_ipa"]) < 2 or len(r["word"]) < 5 or not r["count_text"]:
            continue  # such words cannot be judged reliably as heteronyms, so they are left out
        if len({stress_syllable(c) for c in r["cmu_ipa"]}) > 1:  # CMU variants stress different syllables
            auto.add(r["word"])  # treat the word as a heteronym automatically
    piece_words = {}  # per sentence piece: word list with clause-break flags for expected_role()
    for (sid, k), prow in by_piece.items():  # each piece and its rows
        # the shared word splitter over the piece's tokens in spoken order: reference text and what the voice got
        pw = inventory.piece_words([(r["token"], r["ref"], r["spoken"]) for r in sorted(prow, key=lambda r: r["pos"])])
        # (token, word index, normalized word, token ends in clause punctuation after its last word)
        piece_words[(sid, k)] = [(w.token, w.sub, w.norm, w.punct_after) for w in pw]
    # listed heteronyms present in the book, plus the automatic ones
    for w in sorted((HETERONYMS & set(byword)) | auto):
        r = byword[w]  # the word's measurement row: its CMU variants, counts and token ids
        if not r["count_text"]:  # only heard through spelled numbers
            continue  # no sentence to inspect
        occ = []  # occurrences of this heteronym
        for t in r["tokens"]:  # every token where it occurs
            ir = tok_rows[t]  # the inventory row of that token
            # position of the word inside the token (0 when not found)
            j = ir["norm"].index(w) if w in ir["norm"] else 0
            # how espeak read this exact occurrence inside its sentence, or empty when unknown
            cp = ctx_tok.get((t, j), "")
            # which CMU variant espeak used, judged by stress first
            near = compare(cp, r["cmu_ipa"], prefer_stress=True) if cp else {"best": None, "distance": None}
            # that variant as ARPAbet, or None without context phonemes
            used = r["cmu_arpabet"][near["best"]] if near["best"] is not None else None
            pw = piece_words[(ir["seg"], ir["sent"])]  # words of the sentence piece with clause flags
            # index of this occurrence within the piece
            kk = next(i for i, (tt, jj, _, _) in enumerate(pw) if tt == t and jj == j)
            exp = expected_role([x[2] for x in pw], kk, w, [x[3] for x in pw])  # the reading the context suggests
            vr = verb_reading(w, used) if used else None  # whether espeak's chosen variant is the verb reading
            # the reading espeak gave: verb, noun-adj or unknown
            role = None if vr is None else ("verb" if vr else "noun-adj")
            # location and timing of the occurrence
            occ.append({"token": t, "seg": ir["seg"], "start": ir["start"], "end": ir["end"],
                        # its display text and full sentence piece
                        "display": ir["display"], "sentence": sentences[ir["seg"]][ir["sent"]],
                        # espeak's in-context reading, the CMU variant it matches and the distance
                        "espeak_in_context": cp, "nearest_cmu": used, "nearest_distance": near["distance"],
                        "reading_role": role, "expected_role": exp,  # the reading heard and the one the context expects
                        # suspect only when both are known and they disagree
                        "suspect": bool(role and exp and role != exp)})
        readings = Counter(o["nearest_cmu"] for o in occ)  # how often each CMU variant was used
        # word, occurrences in text, and whether it came from the list or from CMU
        het.append({"word": w, "count": r["count_text"], "source": "list" if w in HETERONYMS else "cmu-variants",
                    # its CMU variants and isolated espeak reading
                    "cmu_arpabet": r["cmu_arpabet"], "espeak_en": r["espeak_en"],
                    "readings_used": dict(readings.most_common()),  # variant usage, most common first
                    # number of suspect occurrences and the detail of each
                    "suspect": sum(o["suspect"] for o in occ), "occurrences": occ})
    # words with suspects first, then listed before automatic, then alphabetical
    het.sort(key=lambda h: (-h["suspect"], h["source"] != "list", h["word"]))

    flagged = sorted([r for r in rows if r["flag"]] + display_mismatch,  # flagged word rows plus display mismatches
                     # most frequent first, then most distant, then alphabetical
                     key=lambda r: (-r["count"], -(r["distance"] or 0), r["word"]))
    # unflagged dictionary words just under the threshold
    near = sorted([r["word"] for r in rows if not r["flag"] and r["distance"] is not None
                   # distance between 0.24 and 0.34, most distant first
                   and FLAG_DISTANCE - 0.1 <= r["distance"] < FLAG_DISTANCE], key=lambda w: -byword[w]["distance"])
    review = sorted([r for r in rows if r["review"]],  # words left to a human: OOV words and proper names
                    # names with a real issue first, then OOV words, then by frequency and name
                    key=lambda r: (not (r["reason"] and not r["oov"]), not r["oov"], -r["count"], r["word"]))
    have = sum(1 for r in rows if r["espeak_en"])  # rows for which espeak returned something
    # number of distinct words and total occurrences
    totals = {"unique": len(rows), "occurrences": sum(r["count"] for r in rows),
              # flagged entries and how many of them are word rows
              "flagged": len(flagged), "flagged_words": sum(1 for r in rows if r["flag"]),
              # display mismatches and OOV words
              "display_mismatch": len(display_mismatch), "oov": sum(r["oov"] for r in rows),
              # proper names and review rows
              "proper_names": sum(r["proper_name"] for r in rows), "review": len(review),
              # heteronyms found and suspect occurrences (the second feeds lexicon_flagged_words)
              "heteronyms": len(het), "heteronym_suspects": sum(h["suspect"] for h in het),
              "numbers": len(num_rows),  # how many different number tokens the book uses
              # number tokens whose spelled form reads like the digits
              "numbers_spelled_ok": sum(n["spelled_matches"] for n in num_rows),
              "coverage": round(have / max(len(rows), 1), 4),  # share of words that espeak phonemized
              # words read differently in some sentence than alone
              "with_context_change": sum(1 for r in rows if r["context_differs"]),
              "r_dropped_words": sum(r["r_dropped"] for r in rows),  # words whose CMU r the non-rhotic voice leaves out
              "r_dropped_occurrences": sum(r["count"] for r in rows if r["r_dropped"]),  # occurrences of those words
              "espeak_calls": ESPEAK_CALLS}  # helper subprocesses used in this process
    return {  # the measurement dict; main() and measure.lexicon_metrics() save it as lexicon_measure.json
        "schema": SCHEMA,  # the format name and version, emblem-rsi/pron-lexicon/1, so readers can check what they load
        # inventory sources plus the espeak voices
        "source": {**inv["source"], "voice_espeak": NARRATION_VOICE, "reference_espeak": REFERENCE_VOICE,
                   "runtime_python": RUNTIME_PY},  # and which runtime interpreter ran the helper
        "method": {  # a plain-language description of each method, stored with the numbers
            "espeak": "EspeakPhonemizer(ESPEAK_DATA_DIR).phonemize(voice, text) in the runtime venv; word rows use the "
                      "word's most common spoken surface form in isolation; context_variants come from phonemizing "
                      "each whole sentence piece (as the narration did) and splitting it among its words by alignment",
            "coarse_alphabet": "stress and length dropped; ɑ/ɒ/ɔ, ə/ʌ/ɐ/ɚ, ɜ/ɝ, ɪ/ᵻ, e/ɛ, a/æ, əʊ/oʊ merged; "
                               "eə/ɪə/ʊə -> ɛ/ɪ/ʊ; ɹ dropped before consonant or word end; ʲ dropped; ɫ->l; ɾ,ʔ->t; n̩->ən",
            "distance": "weighted Levenshtein over coarse symbols / max length; 0.5 for near vowel pairs "
                        # the NEAR pairs listed in a stable order
                        + ", ".join("~".join(sorted(p)) for p in sorted(NEAR, key=sorted)) + "; min over CMU variants",
            "stress": "espeak primary (else secondary) stressed vowel must align to the CMU primary vowel of some variant",
            # the flag rule with the current threshold
            "flag": f"distance >= {FLAG_DISTANCE} or stress mismatch with every variant; OOV and proper names go to review",
            "proper_name": "capitalized at least once mid-sentence in body text and never lowercase",
        },
        "totals": totals,  # the summary counts computed just above
        "rows": rows,  # one row per vocabulary word, alphabetical
        "flagged": flagged,  # flagged words plus display mismatches, most frequent first
        "review": review,  # proper names and OOV words left for a human to judge
        "near_threshold": near,  # words just under the threshold
        "display_mismatch": display_mismatch,  # display mismatches on their own
        "heteronyms": het,  # heteronym records, each with its occurrences and suspect count
        "numbers": num_rows,  # one row per number token, comparing the digit and spelled readings
    }


# True when a CMU variant has an r sound a non-rhotic voice would drop. phones is one variant as a list of
# ARPAbet symbols: an ER vowel always counts, and an R counts at the end or before a consonant.
def _has_coda_r(phones) -> bool:
    # check each ARPAbet symbol with its position, so the following symbol can be inspected
    for k, p in enumerate(phones):
        if p.startswith("ER"):  # any ER vowel (bird, butter)
            return True  # has the r colour
        # an R at the end, or followed by a symbol without a stress digit (a consonant)
        if p == "R" and (k + 1 == len(phones) or not phones[k + 1][-1].isdigit()):
            return True  # an R closing a syllable, which the non-rhotic voice would not pronounce
    return False  # no r that could be dropped


# For a word missing from CMU, finds the best split into two CMU words: the most balanced split wins.
# w is the lowercase word. Returns {"split": "a b", "arpabet": joined pronunciation} or None.
def _compound_guess(w: str):
    """For an OOV word, the best split into two CMU words (first part primary, second secondary)."""
    d = cmu()  # the cached CMU dictionary, for looking up both halves of each split
    best = None  # best split so far
    for i in range(2, len(w) - 1):  # every split leaving at least two letters on each side
        a, b = w[:i], w[i:]  # left and right part
        if a in d and b in d and len(b) >= 2:  # both parts are dictionary words
            # first variant of the left part, with any secondary stress raised to primary
            pa = [re.sub(r"[12]", "1", p) for p in d[a][0]]
            # first variant of the right part, its primary lowered to secondary (compound stress)
            pb = [re.sub(r"1", "2", p) for p in d[b][0]]
            cand = {"split": f"{a} {b}", "arpabet": " ".join(pa + pb)}  # the candidate split and its pronunciation
            # prefer the split whose shorter part is longest
            if best is None or min(len(a), len(b)) > min(len(x) for x in best["split"].split()):
                best = cand  # this split is the most balanced so far, so it becomes the answer
    return best  # best split or None


# Writes obj as indented UTF-8 JSON at path atomically: a .tmp file first, then a rename, so a reader never
# sees half a file. Creates the parent folders. Returns the final path. Also used by candidates.py.
def write_json(obj, path: Path) -> Path:
    path = Path(path)  # accept a str too
    path.parent.mkdir(parents=True, exist_ok=True)  # make sure the folder exists
    tmp = path.with_suffix(path.suffix + ".tmp")  # sibling name such as lexicon_measure.json.tmp
    # IPA stays readable (ensure_ascii=False); trailing newline
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)  # rename over the target in one step
    return path  # where the data ended up


# Formats one flagged row as a fixed-width console line: word, count, distance, stress warning, espeak
# reading and CMU variants. r is a row from measure(); returns the string.
def _fmt(r):
    cmu_s = " | ".join(r["cmu_ipa"]) or "-"  # CMU variants side by side, or "-" for none
    st = "" if r.get("stress_ok") in (True, None) else " STRESS"  # show STRESS only when the stress check failed
    # distance with two decimals, or "-" when there is none
    d = "-" if r.get("distance") is None else f"{r['distance']:.2f}"
    # display mismatches say what the voice was given
    extra = f" (spoken as '{r['spoken_as']}')" if r.get("spoken_as") else ""
    # the padded console line
    return f"{r['word']:<16}{r['count']:>4}  d={d}{st}  espeak en {r['espeak_en']:<18} CMU {cmu_s}{extra}"


# Command-line entry point: builds the inventory, runs the probe and prints a summary. argv is the argument
# list (None means sys.argv). Writes inventory.json and lexicon_measure.json into --outdir and starts the
# espeak helper. Returns 0.
def main(argv=None) -> int:
    # the docstring's first line serves as the --help text
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--karaoke", default=str(inventory.DEFAULT_KARAOKE))  # project folder to audit
    ap.add_argument("--outdir", default=str(OUTDIR))  # where both JSON files go
    ap.add_argument("--top", type=int, default=40)  # how many flagged words to print
    a = ap.parse_args(argv)  # read --karaoke, --outdir and --top from argv (None means the real command line)
    out = Path(a.outdir)  # output folder as a Path
    inv = inventory.build(a.karaoke)  # build the inventory once
    inventory.write(inv, out / "inventory.json")  # save it for inspection
    res = measure(a.karaoke, inv)  # run the probe on that inventory
    # write the full measurement atomically as lexicon_measure.json in the output folder
    write_json(res, out / "lexicon_measure.json")
    t = res["totals"]  # totals for the summary
    print(f"wrote {out / 'lexicon_measure.json'}")  # say where the measurement went
    # unique words and flagged counts
    print(f"  unique {t['unique']}  flagged {t['flagged']} (words {t['flagged_words']}, display mismatches "
          # OOV, proper names and review counts
          f"{t['display_mismatch']})  OOV {t['oov']}  proper names {t['proper_names']}  review {t['review']}  "
          # heteronyms, espeak coverage as a percentage, and numbers
          f"heteronyms {t['heteronyms']}  coverage {t['coverage']:.1%}  numbers spelled ok "
          f"{t['numbers_spelled_ok']}/{t['numbers']}")  # numbers whose spelled form matches the digit reading
    print(f"\ntop {a.top} flagged (espeak en vs CMU):")  # header for the flagged list
    for r in res["flagged"][: a.top]:  # the top N flagged entries
        print("  " + _fmt(r))  # one formatted line each
    # every suspect heteronym occurrence with its word
    sus = [(h["word"], o) for h in res["heteronyms"] for o in h["occurrences"] if o["suspect"]]
    # header with the count
    print(f"\nheteronym occurrences whose reading disagrees with the context heuristic ({len(sus)}):")
    for w, o in sus:  # print one line per occurrence whose reading disagrees with its context
        # segment, CMU variant used, the reading heard and the one expected
        print(f"  {w:<10} {o['seg']} read {o['nearest_cmu']:<14} ({o['reading_role']}, expected {o['expected_role']}): "
              f"{o['sentence'][:80]}")  # the start of its sentence
    return 0  # exit code 0: findings are reported, never treated as a failure


if __name__ == "__main__":  # only when run as a script
    sys.exit(main())  # exit with main()'s return value
