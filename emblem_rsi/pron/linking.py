# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Intrusive-r probe.

The pinned voice config phonemizes with espeak's British English ("en"), which joins a word ending in a
vowel to a following vowel with an r that is not spelled: "Ana and Marcos" becomes /ˈɑːnəɹ and/
("Anar and"). The voice itself is American (LJSpeech), so the inserted r is heard as a real r.
Linking r on words spelled with r ("for a", "after all") is normal American speech and is not counted.

Every adjacent pair of spoken words inside a sentence is phonemized exactly as the voice sees it, and an
occurrence is reported when the first word's phonemes end in ɹ although its spelling has no final r.
"""
from __future__ import annotations  # postponed evaluation of annotations, for modern type hint syntax

import json  # the helper's input and output are JSON lists of strings
import re  # regular expressions for letters, punctuation and vowel starts
import subprocess  # runs the espeak helper inside the speech runtime's own Python
from pathlib import Path  # file paths that work on Windows and Linux

from . import paths  # narration locations and the runtime interpreter, read once from environment variables
RUNTIME_PY = paths.RUNTIME_PY  # the speechkit runtime Python, which has piper and its espeak-ng bridge installed
# helper script beside this file that phonemizes text exactly the way the narration did
HELPER = Path(__file__).with_name("_espeak_helper.py")
# runs of letters (including accented Latin letters) and apostrophes, i.e. the spelled word
_LETTERS = re.compile(r"[A-Za-zÀ-ÿ']+")
# next word begins with a vowel letter, possibly after an opening quote or bracket
VOWEL_START = re.compile(r"^[“”‘’\"(]*[AEIOUaeiouÀ-ÿ]")


# Phonemize a batch of strings with espeak through the narration runtime, in one subprocess call.
# Parameters: texts is a list of strings; voice is the espeak voice name ("en" is British, as narrated).
# Returns one IPA phoneme string per input, in order. Starts a Python process; raises CalledProcessError on failure.
def phonemize(texts: list, voice: str = "en") -> list:
    if not texts:  # skip the process entirely for an empty batch
        return []  # an empty batch yields an empty phoneme list
    # isolated mode (-I) keeps the runtime from picking up modules in the current folder; texts go in on stdin as JSON
    runtime = paths.need(RUNTIME_PY, "EMBLEM_PIPER_PYTHON", "the speechkit runtime Python")  # clear error when unset
    r = subprocess.run([runtime, "-I", str(HELPER), "--voice", voice], input=json.dumps(texts, ensure_ascii=False),
                       # capture UTF-8 output; check=True turns a helper failure into an exception
                       capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")
    return json.loads(r.stdout)  # the helper prints a JSON list of phoneme strings


# Find adjacent word pairs inside a sentence where espeak's British voice could insert a linking r.
# Parameter request is the narration request dict (segments with tokens and tokenIndex).
# Returns a list of (segment id, token id of the first word, first word, next word). Pure; nothing is written.
def candidate_pairs(request: dict) -> list:
    """(seg, token id, word, next word) for adjacent words inside a sentence that could get an intrusive r."""
    out = []  # candidate word pairs collected across all segments
    for seg in request["segments"]:  # every narration segment listed in the request dictionary
        toks, ids = seg["tokens"], seg["tokenIndex"]  # the spoken tokens and their global token ids, in parallel lists
        # each token paired with the token after it, so the last token is never a first word
        for k in range(len(toks) - 1):
            a, b = toks[k], toks[k + 1]  # the candidate first word and the word that follows it
            if a.startswith("[[") or b.startswith("[["):  # skip raw phoneme blocks on either side
                # A raw block is spoken exactly as written, so espeak never adds an r to it.
                continue                  # raw phoneme blocks are voiced as written, no linking
            if re.search(r"[^A-Za-zÀ-ÿ']$", a):  # the first word ends in punctuation or a digit, not a letter
                # Punctuation means a pause, so the two words are not joined.
                continue                  # punctuation after the word: a pause, no linking
            # the letters of the first word in lowercase, apostrophes kept
            letters = "".join(_LETTERS.findall(a)).lower()
            # no letters at all, or the spelling already ends in r, re or r's (where linking r is normal in America too)
            if not letters or letters.endswith(("r", "re", "r's")):
                continue  # those spellings cannot produce an unspelled linking r
            if not VOWEL_START.match(b):  # an r can only intrude before a vowel sound
                continue  # the next word starts with a consonant, so no linking happens
            out.append((seg["id"], ids[k], a, b))  # keep this pair (segment, token id, both words) for phonemizing
    return out  # the full candidate list, ready for one espeak batch


# The intrusive-r probe: phonemize each candidate pair together and report pairs where the first word gains a final r.
# Parameter request is the narration request dict. Returns {"pairs_checked", "occurrences", "words"}.
# Side effect: one espeak subprocess for all pairs. Words are sorted with the most frequent offender first.
def intrusive_r(request: dict) -> dict:
    pairs = candidate_pairs(request)  # candidate pairs from the whole narration
    # phonemize "first next" for each pair, the way the voice saw the text
    ph = phonemize([f"{a} {b}" for _, _, a, b in pairs])
    occ, words = [], {}  # every flagged occurrence, and a per-word summary keyed by the spelled word
    for (sid, tid, a, b), p in zip(pairs, ph):  # walk the pairs alongside their phoneme strings
        # phonemes of the first word, or nothing if espeak returned a single joined chunk
        first = p.split(" ")[0] if " " in p else ""
        if first.endswith("ɹ"):  # espeak ended the first word in ɹ although its spelling has no final r
            w = "".join(_LETTERS.findall(a)).lower()  # the spelled first word in lowercase, used as the summary key
            # record where it happens and what espeak produced
            occ.append({"seg": sid, "token": tid, "word": w, "next": b, "phonemes": p})
            # per-word summary, keeping the first phoneme form seen
            e = words.setdefault(w, {"count": 0, "examples": [], "phonemes": first})
            e["count"] += 1  # count this occurrence for the per-word summary
            if len(e["examples"]) < 3:  # keep at most three examples per word
                e["examples"].append(f"{a} {b}")  # store the pair as readable text, for example "Ana and"
    # totals, every occurrence, and words ordered by count, highest first
    return {"pairs_checked": len(pairs), "occurrences": occ,
            # sorting by negative count gives descending order
            "words": dict(sorted(words.items(), key=lambda kv: -kv[1]["count"]))}


# Run as a script: check a request file (first argument, or the narration default) and print a summary.
if __name__ == "__main__":
    import sys  # imported here because only the script path needs sys.argv
    # load the request JSON from the given path or the configured default
    req = json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else str(paths.REQUEST)).read_text(encoding="utf-8"))
    res = intrusive_r(req)  # run the intrusive-r probe over the whole request
    # headline: how many pairs were checked and how many gained an r
    print(f"pairs checked {res['pairs_checked']}, intrusive r {len(res['occurrences'])}")
    for w, e in res["words"].items():  # one line per affected word
        # word, count, its espeak phonemes and up to three example pairs
        print(f"  {w:14s} x{e['count']:<3d} {e['phonemes']:16s} e.g. {' | '.join(e['examples'])}")
