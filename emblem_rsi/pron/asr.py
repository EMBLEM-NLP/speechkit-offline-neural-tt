# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Listening probe: offline speech recognition of the narration, scored word by word against the script.

The narration is transcribed by a small CTC model with no large language model, so it writes down
what it hears rather than what a sentence "should" say. Every spoken token in the script is turned
into the words a listener should hear (normalize_expected), the two word sequences are aligned, and
each word gets a hit rate. Words heard wrong in most of their occurrences are flagged, and a second,
stronger model (confirm) re-listens to just those segments: a word stays flagged only if both fail it.

Transcripts are cached by the actual recognizer/dependency/settings identity and the WAV hash.
Changed weights, resampling, decoding or audio invalidate the cached transcription.

Run (from the repository root):
    python3 -m emblem_rsi.pron.asr run [--watch doorkeeper]
    python3 -m emblem_rsi.pron.asr transcribe --model ctc path/to/s0215.wav
The module uses relative imports, so it must be run with -m (a plain `python3 emblem_rsi/pron/asr.py`
fails with ImportError). `python3 -I -m emblem_rsi.pron.asr` works only where the package is importable
(installed, or on a path added by the caller): -I keeps the current directory off sys.path.
"""
from __future__ import annotations  # lets annotations such as dict | None be used on any supported Python

import argparse  # command line of the run and transcribe subcommands
import datetime as dt  # timestamp of the measurement (created_at)
import difflib  # character similarity between a heard word and the expected word
import functools  # lru_cache memoizes the normalization and similarity helpers
import hashlib  # SHA-256 of WAV files (cache keys) and of model and request files (provenance)
import importlib.metadata
import shutil
import json  # request, docmodel, cached transcripts and the output measurement are all JSON
import math  # greatest common divisor for resampling, and exp() for token probabilities
import os  # atomic file replacement and the process id in temporary file names
import re  # regular expressions for text normalization
import subprocess  # ffmpeg fallback for audio that is not plain 16-bit WAV
import sys  # sys.stderr carries the progress lines and the error messages
import time  # wall-clock timing of decoding and scoring
import unicodedata  # accent stripping during normalization
import wave  # reads PCM WAV files without third-party libraries
# Path objects for the repository, model, cache and output locations, joined with the / operator
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # repository root: three levels above this file
DEFAULT_OUT = REPO / "runs" / "pron-probe" / "asr"  # default output folder when no --out is given (runs/pron-probe/asr)
# Relative import: this file only works as part of the emblem_rsi.pron package (python -m or an import).
# Started as a plain script (`python3 emblem_rsi/pron/asr.py`) this line raises ImportError, because there
# is no parent package and no script-mode fallback like lexicon.py has; the docstring gives the -m form.
from . import paths as _paths
DEFAULT_MODELS = _paths.MODELS  # folder holding the sherpa-onnx model folders
DEFAULT_REQUEST = _paths.REQUEST  # default narration script (work/request.json)
DEFAULT_SPEECH = _paths.SPEECH  # default folder of narration WAVs and their timing records
DEFAULT_DOCMODEL = _paths.DOCMODEL  # default document model, used for display text in the report

SCHEMA = "emblem-rsi/pron-asr/1"  # format tag of asr_measure.json
# Sample rate the recognizers expect: 16 kHz mono. The narration is 22.05 kHz and is resampled on load.
TARGET_SR = 16000
SIM_THRESHOLD = 0.8      # character (difflib) similarity counted as "heard right"
FAIL_RATE = 0.6          # share of occurrences heard wrong that flags a word
CACHE_VERSION = 2        # bump when audio preparation or decoding settings change
DEL = "<del>"  # marker used as the "heard" text of a word the recognizer skipped

# Words ASR routinely drops or swaps in fluent speech. They count in WER but are never flagged.
FUNCTION_WORDS = frozenset("""
a an the of and to in on at by for from with as into onto than then
is it its are was were be been am being do does did has have had will would can could shall should may might must
i you he she we they me him her us them my your his our their mine yours this that these those there here
or but if so not no nor yet just also up out off over all any each what who whom which when where how
""".split())  # split on whitespace into an immutable set for fast membership tests

# Model candidates per role, in order of preference. Files are glob patterns inside the model folder.
MODELS = {
    # The primary listener ("ctc"): a small NeMo Conformer CTC model. With no language model it writes what it
    # hears. frame_s = 0.04 s is its output frame length, used to estimate word end times.
    "ctc": [
        # folder name under the models directory, decoder type, and frame length in seconds
        {"name": "sherpa-onnx-nemo-ctc-en-conformer-small", "kind": "nemo_ctc", "frame_s": 0.04,
         # the model file (full precision preferred, int8 accepted) and the token list
         "files": {"model": ["model.onnx", "model.int8.onnx"], "tokens": ["tokens.txt"]}},
    ],
    # The confirming listener, tried in order: Parakeet TDT 0.6b first (stronger, 80 ms frames), then a small
    # Zipformer transducer if Parakeet is absent or fails to load.
    "confirm": [
        # Parakeet TDT: sherpa-onnx loads it as a NeMo transducer
        {"name": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8", "kind": "transducer", "model_type": "nemo_transducer",
         "frame_s": 0.08,  # one output frame is 80 ms for this model
         # int8 encoder and decoder preferred, any variant accepted
         "files": {"encoder": ["encoder.int8.onnx", "encoder*.onnx"], "decoder": ["decoder.int8.onnx", "decoder*.onnx"],
                   # joiner network and token list
                   "joiner": ["joiner.int8.onnx", "joiner*.onnx"], "tokens": ["tokens.txt"]}},
        # fallback confirm model: an English Zipformer transducer
        {"name": "sherpa-onnx-zipformer-small-en-2023-06-26", "kind": "transducer", "model_type": "transducer",
         "frame_s": 0.04,  # 40 ms output frames
         # its encoder (int8 first) and decoder files
         "files": {"encoder": ["encoder*.int8.onnx", "encoder*.onnx"], "decoder": ["decoder*.onnx"],
                   "joiner": ["joiner*.int8.onnx", "joiner*.onnx"], "tokens": ["tokens.txt"]}},  # joiner and token list
    ],
}


# Error raised for problems the user can fix, such as no usable model on disk; main() prints it and exits 2.
class AsrError(RuntimeError):
    pass  # no extra behaviour beyond RuntimeError


# ----------------------------------------------------------------------------- text normalization

# Translation table that deletes straight, curly and low quotes, backticks, acute accents and guillemets.
_DROP = dict.fromkeys(map(ord, "\"'`´‘’‚‛“”„‟«»"), None)
_SPLIT = re.compile(r"[\s\-‐‑‒–—―/_]+")  # split on whitespace, hyphens, Unicode dashes, slashes and underscores
_EDGE = ".,;:!?()[]{}<>…*#~|"  # punctuation stripped from both ends of each chunk
_LETTERS = re.compile(r"^[a-z](?:\.[a-z])+\.?$")                 # p.m.  a.m.  u.s.
# A number: comma-grouped digits ("4,200") or plain digits, then optional decimals and an ordinal suffix.
_NUMBER = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(st|nd|rd|th)?")
_WORD = re.compile(r"[a-z]+")  # runs of lowercase letters: the words that remain after normalization


# re.sub callback that spells one number match as words. m is the match of _NUMBER (whole part, decimals,
# ordinal suffix). Returns the words padded with spaces so they never fuse with neighbouring letters.
def _spell_number(m: re.Match) -> str:
    from num2words import num2words  # imported on first use so the module loads without num2words installed
    # whole part without commas, decimal digits (or None), ordinal suffix (or None)
    whole, frac, suffix = m.group(1).replace(",", ""), m.group(2), m.group(3)
    if suffix and not frac:  # an ordinal like "10th" (decimals with a suffix make no sense)
        # ordinal words for the whole part: 10th becomes "tenth", 21st becomes "twenty-first"
        words = num2words(int(whole), to="ordinal")
    else:  # no ordinal suffix, or decimals present: the number is read as a cardinal
        words = num2words(int(whole))  # "four thousand, two hundred"
        if frac:  # the number had a decimal part, such as the 14 in 3.14
            # read digit by digit after "point", e.g. 3.14 -> three point one four
            words += " point " + " ".join(num2words(int(d)) for d in frac)
    # American reading, as an en-US voice says it: "four hundred fifty", not "four hundred and fifty"
    # remove every "and" so the expectation matches an American reading
    return " " + re.sub(r"\band\b", " ", words) + " "


# Cached core of normalize_expected(). The same tokens repeat across segments and models, so 65,536
# entries save most of the work. Returns a tuple because cached values must not be mutable.
@functools.lru_cache(maxsize=65536)
# token is one spoken token or one recognizer word. Returns the lowercase words a listener would hear.
def _normalize(token: str) -> tuple:
    # NFKD splits accented letters and turns compatibility forms (ligatures, full-width) into plain ones
    s = unicodedata.normalize("NFKD", token)
    # drop accents, lowercase, delete quotes and apostrophes ("don't" -> dont)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().translate(_DROP)
    s = s.replace("&", " and ").replace("%", " percent ")  # symbols that are spoken as words
    s = re.sub(r"[$€£¥]", " ", s)  # currency symbols are dropped; the amount itself is still spelled
    out = []  # words found so far
    for chunk in _SPLIT.split(s):  # split into chunks at spaces, hyphens, dashes, slashes and underscores
        chunk = chunk.strip(_EDGE)  # remove edge punctuation such as commas and brackets
        if not chunk:  # the chunk was only punctuation (a lone comma or bracket), so it holds no word
            continue  # skip the empty chunk
        if _LETTERS.match(chunk):  # a dotted abbreviation such as "p.m."
            # spoken as its letters: extending with a string adds one item per character
            out.extend(chunk.replace(".", ""))
            continue  # its letters are already added, so number spelling and word extraction are skipped
        chunk = _NUMBER.sub(_spell_number, chunk)  # spell any numbers inside the chunk
        out.extend(_WORD.findall(chunk))  # keep only letter runs, which also splits "twenty-first" into two words
    return tuple(out)  # immutable result for the cache


# Public entry point of the text normalization used on both sides of the comparison: the script and the
# recognizer output. spoken_token is one token (any value; it is stringified). Returns a list of words.
def normalize_expected(spoken_token: str) -> list:
    """Lowercase words a listener should hear for one spoken token (also used on what ASR wrote).

    Accents and quotes are dropped, numbers are spelled out ("4,200" -> four thousand two hundred,
    "10th" -> tenth), hyphenated words are split ("21-day" -> twenty one day), and dotted letter
    abbreviations become letters ("p.m." -> p m; the aligner also accepts "pm").
    """
    return list(_normalize(str(spoken_token)))  # copy the cached tuple into a fresh list the caller may change


# ----------------------------------------------------------------------------- word similarity

# Strength of a word match: 2 exact, 1 near (same sound or similar spelling), 0 no match.
EXACT, NEAR, NONE = 2, 1, 0


# Loads the CMU pronouncing dictionary once, or an empty dict when the cmudict package is missing.
@functools.lru_cache(maxsize=1)
def _cmu():  # no arguments, so one cached value is enough
    try:  # the dictionary is optional here
        import cmudict  # only available where pip installed cmudict
        return cmudict.dict()  # word -> list of ARPAbet variants
    except Exception:          # optional: without it only character similarity is used
        return {}  # no dictionary: pronunciation matching is simply off


# The CMU pronunciations of a word, stress digits removed, as a set of space-joined strings. Two words
# sharing any entry sound the same (grey/gray). Returns an empty set for words CMU lacks.
@functools.lru_cache(maxsize=65536)
def _prons(word: str) -> frozenset:  # cached because the aligner asks about the same words many times
    # e.g. "G R EY" for both "grey" and "gray"
    return frozenset(" ".join(re.sub(r"\d", "", ph) for ph in p) for p in _cmu().get(word, ()))


# Decides whether expected word a and heard word b match: EXACT (identical), NEAR (same CMU sound, or
# spelling similarity >= SIM_THRESHOLD) or NONE. Cached with room for 500,000 pairs because the aligner
# tests every expected word against every nearby heard word.
@functools.lru_cache(maxsize=500000)
def match_kind(a: str, b: str) -> int:  # both arguments are normalized lowercase words
    """EXACT, NEAR (same CMU pronunciation, e.g. grey/gray, ok/okay, or spelling similarity >= 0.8) or NONE."""
    if a == b:  # the heard word is spelled exactly as expected, the cheapest and most common case
        return EXACT  # strongest match, which costs nothing in the alignment
    if not a or not b:  # an empty string on either side leaves nothing to compare
        return NONE  # an empty word never matches, and the pronunciation lookups below are skipped
    pa = _prons(a)  # pronunciations of the expected word
    if pa and pa & _prons(b):  # a shared CMU pronunciation
        return NEAR  # counts as a near match (heard the right sound, spelled differently)
    la, lb = len(a), len(b)  # lengths of both words
    # the best possible difflib ratio is 2*min/(la+lb); when even that is below the threshold
    if 2 * min(la, lb) / (la + lb) < SIM_THRESHOLD:
        return NONE  # skip the slower SequenceMatcher
    # near when the character similarity reaches 0.8, otherwise no match
    return NEAR if difflib.SequenceMatcher(None, a, b).ratio() >= SIM_THRESHOLD else NONE


# Match test for a word against several words joined together, used for merges in align(). a and b are
# strings; returns EXACT, NEAR (same CMU pronunciation) or NONE. Cached for 200,000 pairs.
@functools.lru_cache(maxsize=200000)
def merge_kind(a: str, b: str) -> int:  # same argument types as match_kind
    """Stricter test for a word against joined pieces ("door"+"keeper", "p"+"m"): exact join or same
    CMU pronunciation only. Spelling similarity is not enough here: "dork"+"eeper" must stay an error."""
    if a == b:  # joined spelling is identical ("door"+"keeper" = "doorkeeper")
        return EXACT  # the joined pieces spell the expected word exactly; only the per-word merge penalty applies
    pa = _prons(a)  # CMU pronunciations of the first string, stress removed; empty when CMU does not know it
    return NEAR if pa and pa & _prons(b) else NONE  # near only when CMU says they sound the same


# ----------------------------------------------------------------------------- alignment

# Aligns the expected word list exp with the heard word list hyp by dynamic programming. max_merge is the
# largest number of words that may be joined into one match. Costs: exact 0, near 0.2, substitution,
# deletion and insertion 1, and each merge adds 0.1 per extra word (plus 0.2 when only near).
def align(exp: list, hyp: list, max_merge: int = 3) -> list:
    """Edit-distance alignment of expected words to heard words.

    Besides match / substitution / deletion / insertion it allows one expected word to match 2-3
    heard words ("doorkeeper" ~ "door keeper") and 2-3 expected words to match one heard word
    ("p m" ~ "pm"), when the joined spelling matches exactly or by CMU pronunciation (merge_kind).
    Returns ops (kind, exp_idx list, hyp_idx list) with kind in ok | near | sub | del | ins.
    """
    n, m = len(exp), len(hyp)  # lengths of the two word lists
    INF = float("inf")  # infinity marks a cell not reached yet
    # D[i][j]: cheapest cost of aligning the first i expected words with the first j heard words
    D = [[INF] * (m + 1) for _ in range(n + 1)]
    # B[i][j]: the step that produced that cost, as (kind, expected words used, heard words used)
    B = [[None] * (m + 1) for _ in range(n + 1)]
    D[0][0] = 0.0  # aligning nothing with nothing costs nothing
    for i in range(n + 1):  # every row, including row 0 (only insertions)
        Di, Bi = D[i], B[i]  # direct references to the current rows, for speed
        for j in range(m + 1):  # every column, including column 0 (only deletions)
            if i == 0 and j == 0:  # cell (0, 0) is the starting point and its cost of 0 is already set
                continue  # nothing to compute for the start cell, so move on to the next column
            best, bp = INF, None  # best cost and step for this cell so far
            if i and j:  # both lists have a word left
                k = match_kind(exp[i - 1], hyp[j - 1])  # how well the last expected word matches the last heard word
                # diagonal step: free if exact, 0.2 if near, 1 for a substitution
                c = D[i - 1][j - 1] + (0.0 if k == EXACT else 0.2 if k == NEAR else 1.0)
                if c < best:  # better than anything seen
                    # record the step as ok, near or sub, consuming one word from each side
                    best, bp = c, ("ok" if k == EXACT else "near" if k == NEAR else "sub", 1, 1)
                for kk in range(2, max_merge + 1):  # try merges of 2 up to max_merge words
                    if j >= kk:  # enough heard words to join kk of them
                        # one expected word against kk heard words joined ("doorkeeper" vs "door"+"keeper")
                        k = merge_kind(exp[i - 1], "".join(hyp[j - kk:j]))
                        if k:  # the joined heard words match the expected word (NONE is 0, which is false)
                            # cost of the cell before those words, plus the merge penalty
                            c = D[i - 1][j - kk] + 0.1 * (kk - 1) + (0.0 if k == EXACT else 0.2)
                            if c < best:  # cheaper than the current best
                                # one expected word, kk heard words
                                best, bp = c, ("ok" if k == EXACT else "near", 1, kk)
                    if i >= kk:  # enough expected words to join kk of them
                        # kk expected words joined against one heard word ("p"+"m" vs "pm")
                        k = merge_kind("".join(exp[i - kk:i]), hyp[j - 1])
                        if k:  # the joined expected words match the single heard word, exactly or by sound
                            # same merge penalty in the other direction
                            c = D[i - kk][j - 1] + 0.1 * (kk - 1) + (0.0 if k == EXACT else 0.2)
                            if c < best:  # cheaper than the current best
                                # kk expected words, one heard word
                                best, bp = c, ("ok" if k == EXACT else "near", kk, 1)
            if i and D[i - 1][j] + 1.0 < best:  # skipping the expected word (the recognizer missed it) costs 1
                # a deletion: the step uses one expected word and no heard word
                best, bp = D[i - 1][j] + 1.0, ("del", 1, 0)
            if j and Di[j - 1] + 1.0 < best:  # an extra heard word costs 1
                # an insertion: the step uses one heard word and no expected word
                best, bp = Di[j - 1] + 1.0, ("ins", 0, 1)
            Di[j], Bi[j] = best, bp  # store the best cost and its step
    ops, i, j = [], n, m  # trace back from the bottom-right cell
    while i or j:  # until both lists are used up
        kind, di, dj = B[i][j]  # the step that reached this cell
        # record it with the expected and heard indexes it covers
        ops.append((kind, list(range(i - di, i)), list(range(j - dj, j))))
        i, j = i - di, j - dj  # move to the cell it came from
    ops.reverse()  # steps were collected backwards
    return ops  # list of (kind, expected indexes, heard indexes)


# Returns a transcript's word list ({"w", "start", "end", "p"?} dicts). Older transcripts without word
# timing fall back to splitting the text on spaces, with no times.
def _word_dicts(tr: dict) -> list:
    words = tr.get("words") or []  # word list with timing, if present
    # or one untimed entry per space-separated word of the text
    return words if words else [{"w": w, "start": None, "end": None} for w in (tr.get("text") or "").split()]


# Scores recognizer transcripts against the script, word by word. request is a narration script
# ({"segments": [{"id", "tokens", "tokenIndex"}]}); transcripts maps segment id -> transcript from
# transcribe(). function_words is accepted for symmetry with flag() but not used here.
# Used for the full narration (run), for trial takes (trial.score_jobs) and for retakes (cli.retake).
def score(request: dict, transcripts: dict, function_words=FUNCTION_WORDS) -> dict:
    """Score transcripts against the script. Segments without a transcript are skipped.

    Returns {"totals", "per_word", "occurrences", "segments"}:
      occurrences[token_id] = {"status": ok|sub|del, "heard", "seg", "expected", "near"?, "asr_t"?, "p"?}
      per_word[word] = {"count", "ok", "rate", "near", "heard": {variant: n}, "segs": [...]}
    """
    per_word, occ, segs_out = {}, {}, {}  # per-word statistics, per-token results and per-segment summaries
    N = S = Dn = I = near = 0  # totals: words, substitutions, deletions, insertions and near matches
    scored = 0  # segments that had a transcript
    for seg in request["segments"]:  # every segment of the script
        sid = seg["id"]  # segment id such as "s0215", used to find its transcript and to label results
        tr = transcripts.get(sid)  # transcript of that segment
        if tr is None:  # no audio or not decoded
            continue  # skipped, so it does not count as errors
        exp, owner = [], []  # expected words and the token id each came from
        for tok, tid in zip(seg["tokens"], seg["tokenIndex"]):  # spoken tokens with their document token ids
            for w in normalize_expected(tok):  # one token may expand to several words ("21-day" -> twenty one day)
                exp.append(w)  # add the word to the expected sequence the aligner will compare
                owner.append(tid)  # remember which token owns it
        if not exp:  # a segment with no speakable words
            continue  # a segment of only punctuation or symbols adds nothing to the totals
        raw = _word_dicts(tr)  # what the recognizer wrote
        hyp, src = [], []  # heard words and which transcript word each came from
        for wi, wd in enumerate(raw):  # walk the recognizer's words together with their position in the transcript
            for x in normalize_expected(wd.get("w", "")):  # normalized the same way as the script
                hyp.append(x)  # add to the heard sequence, normalized exactly like the expected side
                src.append(wi)  # its transcript word index, used for times and confidence
        # edit-distance alignment: ok, near, sub, del and ins steps with the word indexes each covers
        ops = align(exp, hyp)
        # per expected word: [status, hyp indices, near]
        info = [None] * len(exp)  # per expected word: [status, heard indexes, near flag]
        for kind, ei, hj in ops:  # each step gives its kind, the expected indexes it covers and the heard indexes
            for e in ei:  # every expected word it covers (none for insertions)
                # ok and near both count as heard right; sub and del keep their name
                status = "ok" if kind in ("ok", "near") else kind
                info[e] = [status, list(hj), kind == "near"]  # store a fresh list so later edits stay local
        # show a run of insertions next to a substitution as part of what was heard there
        x = 0  # start at the first step
        while x < len(ops):  # an index loop, because each run of insertions is skipped over in one jump
            if ops[x][0] != "ins":  # only an insertion step can start a run worth attaching to a neighbour
                x += 1  # advance past a step that is not an insertion
                continue  # go back and test the following step
            y = x  # start of a run of insertions
            while y < len(ops) and ops[y][0] == "ins":  # find where the run ends
                y += 1  # one past the last insertion
            extra = [h for op in ops[x:y] for h in op[2]]  # heard indexes of all inserted words
            for z in (x - 1, y):  # look at the step before the run, then the one after it
                # the neighbour exists and is a substitution, so the extra heard words belong with it
                if 0 <= z < len(ops) and ops[z][0] == "sub":
                    tgt = info[ops[z][1][0]][1]  # heard indexes of that substituted expected word
                    # merge the inserted words into it, in order, without duplicates
                    tgt[:] = sorted(set(tgt) | set(extra))
                    break  # attach to one neighbour only
            x = y  # continue after the run
        # expected words of this segment that were heard as a different word
        s_sub = sum(1 for v in info if v[0] == "sub")
        s_del = sum(1 for v in info if v[0] == "del")  # expected words of this segment the recognizer skipped entirely
        # extra heard words, including any shown beside a substitution above
        s_ins = sum(len(hj) for kind, _, hj in ops if kind == "ins")
        s_near = sum(1 for v in info if v[2])  # words matched only by near similarity
        N += len(exp); S += s_sub; Dn += s_del; I += s_ins; near += s_near  # add this segment to the totals
        scored += 1  # one more segment scored
        # segment summary: words, substitutions, deletions, insertions
        segs_out[sid] = {"n": len(exp), "sub": s_sub, "del": s_del, "ins": s_ins,
                         # word error rate of this segment and the raw heard text
                         "wer": round((s_sub + s_del + s_ins) / len(exp), 4), "heard": tr.get("text", "")}
        by_token = {}  # token id -> per-word results
        # walk the expected words with their positions to build per-word and per-token results
        for e, w in enumerate(exp):
            # ok, sub or del, the heard indexes, and whether it matched only by similarity
            status, hj, is_near = info[e]
            variant = " ".join(hyp[h] for h in hj) if hj else DEL  # what was heard there, or the deletion marker
            if status == "del":  # the recognizer skipped this word, so its heard variant must be the deletion marker
                variant = DEL  # was heard as nothing
            # statistics for this word across the whole book
            pw = per_word.setdefault(w, {"count": 0, "ok": 0, "near": 0, "heard": {}, "segs": []})
            pw["count"] += 1  # this occurrence counts toward the word's total across the book
            pw["ok"] += status == "ok"  # booleans add as 0 or 1
            pw["near"] += is_near  # occurrences matched only by near similarity
            pw["heard"][variant] = pw["heard"].get(variant, 0) + 1  # tally what was heard in place of the word
            # segments arrive in order, so comparing with the last one avoids duplicates
            if not pw["segs"] or pw["segs"][-1] != sid:
                pw["segs"].append(sid)  # note the segment once, so the report can point to where the word occurs
            by_token.setdefault(owner[e], []).append((w, status, hj, is_near))  # group the word under its token
        for tid, items in by_token.items():  # one result per token
            sts = {st for _, st, _, _ in items}  # statuses of all its words
            # ok if all ok, del if all deleted, otherwise counted as a substitution
            status = "ok" if sts == {"ok"} else "del" if sts == {"del"} else "sub"
            # heard indexes of the token's words that were not deleted
            hj = sorted({h for _, st, hh, _ in items if st != "del" for h in hh})
            # token result: status, heard text, segment
            rec = {"status": status, "heard": " ".join(hyp[h] for h in hj) if hj else DEL, "seg": sid,
                   "expected": " ".join(w for w, *_ in items)}  # the words that were expected
            if any(nr for *_, nr in items):  # some word of the token matched only by similarity
                rec["near"] = True  # mark the token as matched by similarity only, worth a human listen
            wis = sorted({src[h] for h in hj})  # transcript words behind the heard indexes
            if wis:  # the token was heard at all
                # start of the first and end of the last transcript word
                t0, t1 = raw[wis[0]].get("start"), raw[wis[-1]].get("end")
                # older untimed transcripts give None, and then no time span is written
                if t0 is not None and t1 is not None:
                    # time span in the recognizer's view, seconds within the WAV
                    rec["asr_t"] = [round(t0, 3), round(t1, 3)]
                # token probabilities of those words, when the model reported them
                ps = [raw[w_]["p"] for w_ in wis if raw[w_].get("p") is not None]
                if ps:  # at least one word carried a probability; older transcripts have none
                    rec["p"] = min(ps)  # keep the least confident one
            occ[tid] = rec  # store under the document token id
    for pw in per_word.values():  # finish each word's statistics
        pw["rate"] = round(pw["ok"] / pw["count"], 4)  # share of occurrences heard right
        # heard variants, most frequent first
        pw["heard"] = dict(sorted(pw["heard"].items(), key=lambda kv: (-kv[1], kv[0])))
    # word error rate over everything scored, or None when nothing was
    totals = {"wer": round((S + Dn + I) / N, 4) if N else None, "words_scored": N, "sub": S, "del": Dn, "ins": I,
              # segments scored versus segments in the script
              "near_matches": near, "segments_scored": scored, "segments_total": len(request["segments"])}
    # everything the callers need
    return {"totals": totals, "per_word": per_word, "occurrences": occ, "segments": segs_out}


# Lists words heard wrong in at least min_rate of their occurrences. per_word is score()'s per_word,
# function_words are never flagged. Returns dicts with word, count, ok, rate, fail_rate, heard, segs.
def flag(per_word: dict, min_rate: float = FAIL_RATE, function_words=FUNCTION_WORDS) -> list:
    """Words heard wrong in at least `min_rate` of their occurrences (a single failed occurrence counts).

    `min_rate` is the minimum share of occurrences heard wrong, i.e. a word is flagged when
    rate (= ok / count) <= 1 - min_rate. Function words are never flagged. Sorted by occurrences.
    """
    out = []  # words that reach the failure threshold collect here
    for w, r in per_word.items():  # walk each word with its statistics (count, ok, heard, segs)
        if w in function_words or not r.get("count"):  # function words, or a word with no occurrences
            continue  # skipped: function words are dropped too often in fluent speech to mean anything
        fail = (r["count"] - r["ok"]) / r["count"]  # share of occurrences heard wrong
        if fail + 1e-9 >= min_rate:  # the tiny epsilon keeps rounding from missing an exact 0.6
            # word, occurrences, correct hearings and success rate
            out.append({"word": w, "count": r["count"], "ok": r["ok"], "rate": round(r["ok"] / r["count"], 4),
                        # failure rate, what was heard and where
                        "fail_rate": round(fail, 4), "heard": r.get("heard", {}), "segs": list(r.get("segs", []))})
    # most frequent first, then worst, then alphabetical
    out.sort(key=lambda e: (-e["count"], -e["fail_rate"], e["word"]))
    return out  # flagged word entries, ready for the confirm pass and the report


# Lists words that passed only through near matches (same CMU pronunciation or similar spelling) in at least
# min_rate of their occurrences. per_word is score()'s output, flagged the result of flag() (those are skipped). Returns review entries sorted by frequency.
def near_review(per_word: dict, flagged: list, min_rate: float = FAIL_RATE, function_words=FUNCTION_WORDS) -> list:
    """Words not flagged but matched only near in >= min_rate of occurrences (e.g. 'brothelles').

    A near match is a heard word with a shared CMU pronunciation (grey/gray) or a spelling similarity of
    at least 0.8; it is not an exact match. These pass the probe's matching rules, so they are not errors
    by the probe's definition, but a vowel or stress change can hide here. Listed for a human ear, not counted as failures.
    """
    skip = {e["word"] for e in flagged}  # already flagged words are not repeated
    # word, occurrences, near matches, exact matches and heard variants
    out = [{"word": w, "count": r["count"], "near": r["near"], "exact": r["ok"] - r["near"], "heard": r["heard"],
            "segs": r["segs"]}  # where the word occurs
           for w, r in per_word.items()  # every word in the book's statistics is a candidate for review
           # not flagged, not a function word, and near-only in at least min_rate of occurrences
           if w not in skip and w not in function_words and r["near"] and r["near"] / r["count"] + 1e-9 >= min_rate]
    # most frequent first, with ties broken alphabetically so the order is stable
    out.sort(key=lambda e: (-e["count"], e["word"]))
    return out  # near-only words for a human to listen to; they are not counted as failures


# Lets the second recognizer decide each word the first one flagged. flagged comes from flag(),
# per_word_confirm is score()'s per_word for the confirm transcripts. A word stays only if both fail it.
def combine_confirm(flagged: list, per_word_confirm: dict, min_rate: float = FAIL_RATE):
    """Keep a flagged word only if the confirm model also fails it in >= min_rate of its occurrences.

    Returns (confirmed, cleared, unchecked); entries carry both models' rates and heard variants.
    """
    # words both models fail, words the confirm model heard right, and words it never heard
    confirmed, cleared, unchecked = [], [], []
    for e in flagged:  # every word the first model flagged
        c = per_word_confirm.get(e["word"])  # the confirm model's statistics for it
        # both rates and heard variants are carried along
        row = {"word": e["word"], "count": e["count"], "ctc_rate": e["rate"], "ctc_heard": e["heard"], "segs": e["segs"]}
        if not c or not c.get("count"):  # the confirm model never heard this word
            unchecked.append(row)  # cannot confirm or clear it
            continue  # an unchecked word skips the rate comparison below and the loop moves to the next flagged word
        cfail = (c["count"] - c["ok"]) / c["count"]  # confirm model's failure rate
        # add the confirm model's count, success rate and heard variants
        row.update(confirm_count=c["count"], confirm_rate=round(c["ok"] / c["count"], 4), confirm_heard=c["heard"])
        (confirmed if cfail + 1e-9 >= min_rate else cleared).append(row)  # both fail it: confirmed; otherwise cleared
    # most frequent first, then lowest combined success rate, then alphabetical
    key = lambda e: (-e["count"], e["ctc_rate"] + e.get("confirm_rate", 0), e["word"])
    # the three lists in that order
    return sorted(confirmed, key=key), sorted(cleared, key=key), sorted(unchecked, key=key)


# ----------------------------------------------------------------------------- transcription

# SHA-256 of a file as a hex string, read in 1 MiB blocks so large WAVs and models never load whole.
# p is a path. Used for transcript cache keys and for the provenance hashes in the measurement.
def sha256_file(p) -> str:
    h = hashlib.sha256()  # a running SHA-256 state that is fed the file block by block
    with open(p, "rb") as f:  # binary mode, so the hash covers the exact bytes with no newline translation
        # read 1 MiB (1 << 20 bytes) at a time until an empty block marks the end
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)  # add this block of up to 1 MiB to the running hash
    return h.hexdigest()  # the digest as 64 lowercase hex characters, usable as a file name and a provenance value


# Finds one model's files on disk. spec is an entry of MODELS, models_dir the folder holding model folders.
# Returns {file role: Path}, taking the first glob hit per role, or None if the folder or any file is missing.
def _files(spec: dict, models_dir: Path):
    root = Path(models_dir) / spec["name"]  # each model lives in a folder named after it
    if not root.is_dir():  # no folder for this model: it was never downloaded or unpacked
        return None  # tells resolve_model() to try the next candidate for the role
    got = {}  # role -> file found
    for key, pats in spec["files"].items():  # each required file role (model, tokens, encoder, ...)
        for pat in pats:  # patterns in order of preference
            hits = sorted(root.glob(pat))  # matching files, sorted so the choice is stable
            if hits:  # this pattern matched at least one file in the model folder
                got[key] = hits[0]  # take the first match
                break  # stop trying patterns for this role
        else:  # runs only when no pattern matched (the loop ended without break)
            return None  # a required file is missing
    return got  # every role has a file


# Model names that failed to load in this process, so resolve_model() moves on to the next candidate.
_LOAD_FAILED: set = set()
_RECOGNIZERS: dict = {}  # loaded recognizers keyed by (model name, threads), reused across calls


# Picks the model for a role. model is "ctc" or "confirm"; models_dir is where models are stored.
# Returns a copy of the MODELS entry with a "paths" dict of file paths as strings. Raises AsrError for an
# unknown role or when no candidate is on disk and loadable.
def resolve_model(model: str, models_dir=DEFAULT_MODELS) -> dict:
    """The first candidate for this role that is on disk and has not failed to load."""
    if model not in MODELS:  # not a known role
        raise AsrError(f"unknown model {model!r}; use one of {sorted(MODELS)}")  # name the valid roles in the message
    if models_dir is None:  # EMBLEM_ASR_MODELS was not set and no folder was passed
        raise AsrError("no recognizer models folder: set EMBLEM_ASR_MODELS or pass --models-dir")
    for spec in MODELS[model]:  # candidates in preference order
        if spec["name"] in _LOAD_FAILED:  # this one already failed to load
            continue  # a model that already failed to load in this process is not tried again
        files = _files(spec, models_dir)  # look for its files
        if files:  # every required file of this candidate was found on disk
            return dict(spec, paths={k: str(v) for k, v in files.items()})  # the spec plus the concrete file paths
    # nothing usable: list what was tried
    raise AsrError(f"no usable {model} model under {models_dir} (tried {[s['name'] for s in MODELS[model]]})")


@functools.lru_cache(maxsize=128)
def _model_hash(path: str, size: int, mtime_ns: int, ctime_ns: int) -> str:
    return sha256_file(path)


def recognizer_identity(spec: dict, threads: int) -> dict:
    """The actual weights, decoding implementation and resampler own a transcript cache."""
    files = {}
    for key, raw in sorted(spec["paths"].items()):
        path = Path(raw)
        stat = path.stat()
        files[key] = {"name": path.name, "sha256": _model_hash(str(path.resolve()), stat.st_size,
                                                              stat.st_mtime_ns, stat.st_ctime_ns)}
    versions = {}
    for name in ("sherpa-onnx", "sherpa-onnx-core", "numpy", "scipy"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    ffmpeg = shutil.which("ffmpeg")
    ffmpeg_sha = None
    if ffmpeg:
        file = Path(ffmpeg).resolve(); stat = file.stat()
        ffmpeg_sha = _model_hash(str(file), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    components = {"name": spec["name"], "files": files, "kind": spec["kind"],
                  "model_type": spec.get("model_type"), "frame_s": spec["frame_s"],
                  "sample_rate": TARGET_SR, "decoder": "greedy_search", "threads": threads,
                  "resampler": "scipy.signal.resample_poly", "ffmpegFallbackSha256": ffmpeg_sha, "versions": versions,
                  "implementation_sha256": sha256_file(__file__), "cache_version": CACHE_VERSION}
    encoded = json.dumps(components, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "components": components}


# Loads (once per model and thread count) a sherpa-onnx offline recognizer. spec comes from resolve_model(),
# threads is the number of CPU threads for inference. Returns the recognizer; loading can take seconds and
# raises if the installed sherpa-onnx cannot read the model.
def _recognizer(spec: dict, threads: int):
    # model name plus thread count: a different thread count needs its own recognizer instance
    key = (recognizer_identity(spec, threads)["sha256"], threads)
    if key not in _RECOGNIZERS:  # not loaded yet in this process
        import sherpa_onnx as so  # imported lazily: only needed when audio is actually decoded
        p = spec["paths"]  # file paths of this model
        if spec["kind"] == "nemo_ctc":  # the primary listener: a NeMo CTC model stored as a single model file
            # NeMo CTC loader: model file, tokens and thread count
            r = so.OfflineRecognizer.from_nemo_ctc(model=p["model"], tokens=p["tokens"], num_threads=threads,
                                                   # 16 kHz input, greedy decoding (no beam search, no language model)
                                                   sample_rate=TARGET_SR, decoding_method="greedy_search")
        else:  # Parakeet or Zipformer: a transducer split into encoder, decoder and joiner files
            # encoder, decoder and joiner networks of the transducer
            r = so.OfflineRecognizer.from_transducer(encoder=p["encoder"], decoder=p["decoder"], joiner=p["joiner"],
                                                     # token list, thread count and sample rate
                                                     tokens=p["tokens"], num_threads=threads, sample_rate=TARGET_SR,
                                                     # greedy decoding; model_type tells sherpa-onnx which transducer
                                                     # family it is
                                                     decoding_method="greedy_search", model_type=spec["model_type"])
        _RECOGNIZERS[key] = r  # keep it for later calls
    # the recognizer loaded just now, or by an earlier call with the same model and thread count
    return _RECOGNIZERS[key]


# Reads an audio file as mono float32 samples in -1..1 at 16 kHz, the format the recognizers expect.
# path is a WAV (or any format ffmpeg can read). Returns a contiguous numpy array.
def load_audio_16k(path):
    """Mono float32 samples at 16 kHz (PCM16 WAV via numpy/scipy; anything else via ffmpeg)."""
    import numpy as np  # numpy is imported here so the text-only functions work without it
    try:  # fast path: 16-bit PCM WAV read with the standard library
        # standard-library reader; raises wave.Error for compressed, float or non-WAV files
        with wave.open(str(path)) as w:
            # sample rate, channels, bytes per sample and frame count
            sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
            raw = w.readframes(n)  # all samples as raw bytes
        if sw != 2:  # 8-bit, 24-bit or 32-bit samples, which this fast path does not convert
            raise wave.Error("not PCM16")  # fall through to ffmpeg
        # little-endian int16 to float, scaled to -1..1
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if ch > 1:  # stereo or multichannel audio, whose samples are interleaved frame by frame
            x = x.reshape(-1, ch).mean(axis=1)  # average the channels into mono
        if sr != TARGET_SR:  # not already 16 kHz
            from scipy.signal import resample_poly  # polyphase resampler from scipy
            g = math.gcd(TARGET_SR, sr)  # reduce the rate ratio, e.g. 16000/22050 = 320/441
            # upsample by 320 and downsample by 441 with anti-alias filtering
            x = resample_poly(x, TARGET_SR // g, sr // g)
        return np.ascontiguousarray(x, dtype=np.float32)  # float32 in one contiguous block, as sherpa-onnx requires
    except (wave.Error, EOFError):  # non-PCM16 audio; scipy is a required audit dependency
        # let ffmpeg decode, downmix to mono and resample to 16 kHz
        out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(TARGET_SR),
                              # raw signed 16-bit little-endian on stdout; check=True raises if ffmpeg fails
                              "-f", "s16le", "-"], capture_output=True, check=True).stdout
        return np.frombuffer(out, dtype="<i2").astype(np.float32) / 32768.0  # same scaling as above


# Groups the recognizer's subword tokens into words with times and confidence. tokens are BPE pieces where
# a leading space or "▁" starts a new word; timestamps (seconds), durations (seconds, transducers only)
# and log_probs line up with tokens; frame_s is the model's frame length. Returns [{"w", "start", "end", "p"?}].
def words_from_tokens(tokens, timestamps=None, durations=None, log_probs=None, frame_s=0.04) -> list:
    """Group BPE tokens (a leading space or '▁' starts a word) into words with start/end times.

    End = last token start + its duration (transducer) or + one output frame (CTC, approximate).
    `p` = lowest token probability in the word, when the model reports token log-probs.
    """
    # missing inputs become empty lists so the indexing below is safe
    timestamps, durations, log_probs = list(timestamps or []), list(durations or []), list(log_probs or [])
    words, cur, new = [], None, True  # words so far, the word being built, and whether the next token starts a new word
    for k, tok in enumerate(tokens):  # each token with its index
        t = tok.replace("▁", " ")  # treat the SentencePiece word marker like a space
        if t[:1] == " ":  # it starts with a space
            new = True  # so it begins a new word
        piece = t.strip()  # the token text without the marker
        if not piece:  # a bare marker with no letters
            continue  # only sets the new-word flag
        ts = timestamps[k] if k < len(timestamps) else None  # token start time, if reported
        # end = start + reported duration, or + one frame when the model gives none
        end = None if ts is None else ts + (durations[k] if k < len(durations) and durations[k] > 0 else frame_s)
        lp = log_probs[k] if k < len(log_probs) else None  # natural-log probability of this token, if reported
        if new or cur is None:  # start a new word
            # its text, start, end and the log-probabilities collected so far
            cur = {"w": piece, "start": ts, "end": end, "_lp": [] if lp is None else [lp]}
            words.append(cur)  # add it to the list
            new = False  # following tokens continue this word
        else:  # a token with no leading marker continues the current word, e.g. "er" after "keep"
            cur["w"] += piece  # append to the current word's text
            if end is not None:  # this piece has a known end time
                cur["end"] = end  # the word now ends where this later piece ends
            if lp is not None:  # the model reported a log-probability for this piece
                cur["_lp"].append(lp)  # kept so the word's confidence can be that of its least certain piece
    for w in words:  # second pass: finish each word's fields for the JSON transcript
        lps = w.pop("_lp")  # remove the temporary list
        for key in ("start", "end"):  # the start and end times, in seconds from the beginning of the WAV
            if w[key] is not None:  # times stay None when the model reported no timestamps
                w[key] = round(float(w[key]), 3)  # plain float with millisecond precision
        if lps:  # only words with at least one reported log-probability get a p value
            w["p"] = round(math.exp(min(lps)), 4)  # p = probability of the least certain token, as a 0..1 value
    return words  # the words of the transcript


# Transcribes WAV files with one recognizer role, using a cache keyed by the audio's SHA-256. wav_paths are
# the files; model is "ctc" or "confirm"; cache_dir is the cache root; models_dir holds the models; threads
# is the CPU thread count; progress is an optional callback for status lines; stats, if given, receives
# counts and timings. Returns {segment id: {"text", "words", "model", "sha256"}}. Side effects: decodes
# uncached audio (CPU heavy) and writes one JSON file per decoded WAV.
def transcribe(wav_paths, model: str = "ctc", *, cache_dir=None, models_dir=DEFAULT_MODELS, threads: int = 2,
               progress=None, stats: dict | None = None) -> dict:  # keyword-only options after the star
    """{seg_id: {"text", "words": [{"w", "start", "end", "p"?}], "model", "sha256"}} for each WAV.

    seg_id is the file name up to the first dot. Results are cached by WAV sha256 + model name, so
    any changed weights, decoder, resampler, implementation or audio is decoded again.
    """
    spec = resolve_model(model, models_dir)
    identity = recognizer_identity(spec, threads)
    cdir = Path(cache_dir or DEFAULT_OUT / "cache") / f"{spec['name']}-v{CACHE_VERSION}-{identity['sha256']}"
    # running statistics: segments, cache hits, decodes and seconds of audio and decoding
    st = {"model": spec["name"], "segments": 0, "cached": 0, "decoded": 0, "audio_s": 0.0, "decode_s": 0.0,
          "cached_audio_s": 0.0, "cached_decode_s": 0.0, "recognizer_identity": identity}  # what the cached segments cost when they were first decoded
    out = {}  # segment id -> transcript
    paths = [Path(p) for p in wav_paths]  # accept strings or paths
    t_start = time.time()  # start of the wall-clock timer
    for n, p in enumerate(paths, 1):  # each file, counted from 1 for the progress line
        sid = p.name.split(".")[0]  # "s0215.wav" -> "s0215" (trial files give their job id)
        sha = sha256_file(p)  # hash of the audio content
        # cache file named after the audio hash, so identical audio under a new name still hits
        cp = cdir / f"{sha}.json"
        rec = None  # None means this file still has to be decoded
        if cp.exists():  # this exact audio was decoded before by this model and cache version
            try:  # a damaged cache file is ignored
                rec = json.loads(cp.read_text(encoding="utf-8"))
                if (not isinstance(rec, dict) or rec.get("recognizer_identity") != identity or rec.get("sha256") != sha
                        or rec.get("cache_version") != CACHE_VERSION or rec.get("model") != spec["name"]
                        or not isinstance(rec.get("text"), str) or not isinstance(rec.get("words"), list)
                        or any(not isinstance(w, dict) or not isinstance(w.get("w"), str) for w in rec["words"])):
                    raise ValueError("cached transcript provenance mismatch")
                for field in ("audio_s", "decode_s"):
                    if not math.isfinite(float(rec.get(field, 0))) or float(rec.get(field, 0)) < 0:
                        raise ValueError("invalid cached transcript statistics")
                st["cached"] += 1  # count a cache hit for the progress line and the statistics
                st["cached_audio_s"] += rec.get("audio_s", 0.0)  # seconds of audio it covered
                st["cached_decode_s"] += rec.get("decode_s", 0.0)  # and how long it took to decode back then
            except (ValueError, TypeError, KeyError):  # malformed JSON, schema or provenance
                rec = None  # discard it, so the audio is decoded again and the cache file rewritten
        if rec is None:  # no cache file, or a damaged one: this WAV must be decoded now
            try:  # loading the recognizer is the step that fails for a model the installed sherpa-onnx cannot read
                # loaded on the first decode of the call, then reused from _RECOGNIZERS
                rec_ = _recognizer(spec, threads)
            except Exception as e:      # e.g. a model the installed sherpa-onnx cannot load: try the next one
                _LOAD_FAILED.add(spec["name"])  # remember the failure so resolve_model() skips this model
                # say which model failed and why
                print(f"asr: {spec['name']} failed to load ({e}); trying the next {model} model", file=sys.stderr)
                # start over with the next candidate model for this role (it has its own cache folder)
                return transcribe(wav_paths, model, cache_dir=cache_dir, models_dir=models_dir, threads=threads,
                                  progress=progress, stats=stats)  # same options as this call
            x = load_audio_16k(p)  # samples at 16 kHz
            t0 = time.time()  # time just the decoding
            s = rec_.create_stream()  # a new decoding stream for this file
            s.accept_waveform(TARGET_SR, x)  # feed all samples at once
            rec_.decode_stream(s)  # run the recognizer over the whole file; this is the CPU-heavy step
            r = s.result  # text, tokens, timestamps and more
            dt_ = time.time() - t0  # decoding time in seconds
            # what gets cached: model, cache version, audio hash and text
            rec = {"model": spec["name"], "cache_version": CACHE_VERSION, "sha256": sha,
                   "recognizer_identity": identity, "text": r.text.strip(),
                   # word list grouped from the tokens
                   "words": words_from_tokens(r.tokens, r.timestamps, getattr(r, "durations", None),
                                              # durations and log-probabilities exist only on some models, hence getattr
                                              # with a default
                                              getattr(r, "ys_log_probs", None), spec["frame_s"]),
                   # audio length and decoding time in seconds
                   "audio_s": round(len(x) / TARGET_SR, 3), "decode_s": round(dt_, 3)}
            cdir.mkdir(parents=True, exist_ok=True)  # create the cache folder on first write
            tmp = cp.with_suffix(f".tmp{os.getpid()}")  # temporary name unique to this process
            # write the transcript JSON to the temporary file, keeping non-ASCII characters as they are
            tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, cp)  # rename into place atomically, so a parallel run never reads half a file
            st["decoded"] += 1  # one more file decoded rather than read from the cache
            st["audio_s"] += rec["audio_s"]  # seconds of audio decoded
            st["decode_s"] += dt_  # add this file's decoding seconds, later used for the real-time factor
        # what the caller gets for this segment
        out[sid] = {"text": rec["text"], "words": rec["words"], "model": rec["model"], "sha256": sha,
                    "recognizer_identity": identity["sha256"]}
        st["segments"] += 1  # one more segment done
        if progress and (n % 25 == 0 or n == len(paths)):  # every 25 files and at the end
            # report counts so far
            progress(f"{model} {spec['name']}: {n}/{len(paths)}  cached {st['cached']}  decoded {st['decoded']}"
                     f"  ({st['audio_s']:.0f}s audio in {st['decode_s']:.0f}s)")  # and the decoding speed
    st["wall_s"] = round(time.time() - t_start, 2)  # wall-clock seconds for the whole call
    st["audio_s"] = round(st["audio_s"], 2)  # decoded audio seconds, rounded to hundredths for the report
    st["decode_s"] = round(st["decode_s"], 2)  # decoding seconds of this call, rounded to hundredths
    st["cached_audio_s"] = round(st["cached_audio_s"], 2)  # cached audio seconds rounded
    st["cached_decode_s"] = round(st["cached_decode_s"], 2)   # what the cached segments cost when first decoded
    # all audio covered and all decoding time, cached included
    a_all, d_all = st["audio_s"] + st["cached_audio_s"], st["decode_s"] + st["cached_decode_s"]
    # real-time factor: decoding seconds per second of audio (below 1 is faster than real time)
    st["rtf"] = round(d_all / a_all, 4) if a_all else None
    if stats is not None:  # the caller wants the statistics
        stats.update(st)  # copy them into its dict
    return out  # transcripts by segment id


# ----------------------------------------------------------------------------- the full probe

# Formats the heard variants of a word for the console, e.g. "'dork' x3 / 'door' ...". heard maps variant
# to count (most common first), limit is how many to show. Returns the string.
def _fmt_heard(heard: dict, limit: int = 3) -> str:
    items = list(heard.items())[:limit]  # the first few variants
    # each quoted, with a count when it occurred more than once
    s = " / ".join(f"'{v}'" + (f" x{c}" if c > 1 else "") for v, c in items)
    return s + (" ..." if len(heard) > limit else "")  # an ellipsis says more variants exist


# Runs the whole listening probe on a narration: both recognizers, scoring, flagging and confirmation.
# request_path is the script (measure.py passes the reference request so overridden words are judged
# against the book's spelling), speech_dir holds the WAVs, out_dir receives asr_measure.json and the cache,
# models_dir holds models, docmodel_path gives display texts, min_rate is the flag threshold, watch lists
# words to report in detail, threads is per recognizer, top limits the console list, segments restricts the
# run to some ids, log prints. Returns the measurement dict (also written to out_dir/asr_measure.json).
def run(request_path=DEFAULT_REQUEST, speech_dir=DEFAULT_SPEECH, out_dir=DEFAULT_OUT, models_dir=DEFAULT_MODELS,
        # path to the document model, flag rate, watched words, CPU threads, console list size
        docmodel_path=DEFAULT_DOCMODEL, min_rate=FAIL_RATE, watch=(), threads=2, top=40,
        segments=None, log=print) -> dict:  # optional segment subset and the logging function
    t_all = time.time()  # start of the total timer
    # accept strings for all three paths
    request_path, speech_dir, out_dir = Path(request_path), Path(speech_dir), Path(out_dir)
    request = json.loads(request_path.read_text(encoding="utf-8"))  # load the narration script
    if segments:  # a subset was requested
        keep = set(segments)  # as a set for fast lookup
        # a shallow copy of the script whose segment list holds only the requested ids
        request = dict(request, segments=[s for s in request["segments"] if s["id"] in keep])
    segs = request["segments"]  # the segments this run covers: all of them, or the requested subset
    disp = {}  # token id -> display text
    if docmodel_path and Path(docmodel_path).exists():  # the document model exists
        # map each token to its display text for the report
        disp = {t["i"]: t.get("disp") for t in json.loads(Path(docmodel_path).read_text(encoding="utf-8"))["tokens"]}
    wav = {s["id"]: speech_dir / f"{s['id']}.wav" for s in segs}  # expected WAV path per segment
    missing = sorted(sid for sid, p in wav.items() if not p.exists())  # segments with no audio, reported in the inputs
    # only WAVs on disk are transcribed; the missing ones are listed in the measurement
    present = [p for sid, p in wav.items() if p.exists()]
    tts_t = {}  # token id -> [start, end] seconds from the voice's own timing
    for p in present:  # read the voice's word timing for every WAV that exists
        j = p.with_name(p.name + ".json")  # its timing record, <id>.wav.json
        if j.exists():  # written by the narration step
            for w in json.loads(j.read_text(encoding="utf-8")).get("words", []):  # each word the voice timed
                # document token id -> start and end in seconds, rounded to milliseconds
                tts_t[w["token"]] = [round(w["start"], 3), round(w["end"], 3)]
    progress = lambda m: print(m, file=sys.stderr, flush=True)  # progress lines go to stderr so stdout stays clean
    cache = out_dir / "cache"  # shared transcript cache under the output folder

    # 1. primary listener over everything
    st_ctc = {}  # statistics of the primary listener
    # transcribe every WAV with the CTC model
    ctc_tr = transcribe(present, "ctc", cache_dir=cache, models_dir=models_dir, threads=threads, progress=progress,
                        stats=st_ctc)  # stats are filled in place
    t0 = time.time()  # start timing the scoring and flagging of the CTC pass
    sc = score(request, ctc_tr)  # score the CTC transcripts against the script
    flagged = flag(sc["per_word"], min_rate)  # words the CTC model fails often enough
    t_score = time.time() - t0  # scoring time so far

    # 2. confirmation listener on segments holding a flagged (or watched) word
    watch = [w for x in (watch or ()) for w in normalize_expected(x)]  # normalize the watched words like the script
    review = near_review(sc["per_word"], flagged, min_rate)  # near-match-only words also get a second opinion
    # segments holding a flagged or near-review word
    conf_segs = {s for e in flagged for s in e["segs"]} | {s for e in review for s in e["segs"]}
    for w in watch:  # watched words are always re-heard, even when they were not flagged
        conf_segs |= set(sc["per_word"].get(w, {}).get("segs", []))  # add the segments where it occurs
    st_conf = {}  # statistics of the confirm listener
    # transcribe only those segments with the stronger model
    conf_tr = transcribe([wav[s] for s in sorted(conf_segs)], "confirm", cache_dir=cache, models_dir=models_dir,
                         # skipped when no segment holds a flagged, near-review or watched word
                         threads=threads, progress=progress, stats=st_conf) if conf_segs else {}
    t0 = time.time()  # restart the timer for scoring the confirm pass
    # score the confirm transcripts, or an empty result
    sc2 = score(request, conf_tr) if conf_tr else {"totals": {}, "per_word": {}, "occurrences": {}, "segments": {}}
    # sort the flagged words into confirmed, cleared and unchecked
    confirmed, cleared, unchecked = combine_confirm(flagged, sc2["per_word"], min_rate)
    for e in review:                      # second opinion only; near-review words are never "confirmed"
        c = sc2["per_word"].get(e["word"])  # the confirm model's view of this near-review word
        if c and c.get("count"):  # it was heard at least once
            # add its count and success rate
            e.update(confirm_count=c["count"], confirm_rate=round(c["ok"] / c["count"], 4),
                     confirm_exact=c["ok"] - c["near"], confirm_heard=c["heard"])  # exact matches and heard variants
    t_score += time.time() - t0  # add the confirm scoring time

    occ = sc["occurrences"]  # per-token results from the CTC pass
    for tid, rec in occ.items():  # enrich each token result
        if tid in tts_t:  # the voice's own timing for the token
            rec["tts_t"] = tts_t[tid]  # add the voice's own timing, to compare with the recognizer's time span
        if tid in disp:  # the token has display text
            rec["disp"] = disp[tid]  # the book's display spelling of the token, shown in the report
        c = sc2["occurrences"].get(tid)  # the confirm model's result for the same token
        if c:  # the token's segment was also decoded by the confirm model
            # keep its status, heard text, confidence and time span
            rec["confirm"] = {k: c[k] for k in ("status", "heard", "p", "asr_t") if k in c}
    for sid, row in sc2["segments"].items():  # every segment the confirm model decoded
        sc["segments"][sid]["confirm_wer"] = row["wer"]  # add the confirm word error rate to the segment summary
        sc["segments"][sid]["confirm_heard"] = row["heard"]  # and what the confirm model heard

    watch_out = {}  # detail for watched words
    for w in watch:  # collect detail for every watched word
        tids = [t for t, r in occ.items() if w in r["expected"].split()]  # tokens whose expected words include it
        # both models' statistics for the word
        watch_out[w] = {"ctc": sc["per_word"].get(w), "confirm": sc2["per_word"].get(w),
                        "occurrences": {t: occ[t] for t in sorted(tids)}}  # and every occurrence, sorted by token id

    # Describes the model used for a role, with a hash of every file so the measurement records exactly which
    # weights listened. st is that role's statistics (empty when the role never ran), role is "ctc" or "confirm".
    def model_info(st, role):
        if not st:  # the role did not run
            return None  # the measurement shows null for a role that never decoded anything
        spec = resolve_model(role, models_dir)  # the model resolve_model() picks now, skipping any that failed to load
        return {"name": spec["name"], "role": role, "kind": spec["kind"],  # name, role and decoder type
                # file name -> SHA-256 of each model file, recording exactly which weights listened
                "files": {Path(p).name: sha256_file(p) for p in spec["paths"].values()},
                "recognizer_identity": st.get("recognizer_identity")}

    measure = {  # the measurement written to disk and returned
        "schema": SCHEMA,  # format tag emblem-rsi/pron-asr/1, so readers can recognize the layout
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),  # UTC time, to the second
        # which script was scored, with its hash
        "inputs": {"request": str(request_path), "request_sha256": sha256_file(request_path),
                   # where the audio came from, how many segments, and which were missing
                   "speech_dir": str(speech_dir), "segments_total": len(segs), "missing_wavs": missing},
        # descriptions of both models (None for a role that did not run)
        "models": {"ctc": model_info(st_ctc, "ctc"), "confirm": model_info(st_conf, "confirm")},
        # the parameters that shaped the result
        "params": {"sample_rate": TARGET_SR, "similarity_threshold": SIM_THRESHOLD, "fail_rate": min_rate,
                   "rule": "flag if ctc fails >= fail_rate of occurrences; keep only if confirm also fails >= fail_rate",
                   # function words, watched words and cache version
                   "function_words_not_flagged": sorted(FUNCTION_WORDS), "watch": watch, "cache_version": CACHE_VERSION},
        "totals": {"ctc": sc["totals"], "confirm": sc2["totals"]},  # totals of both passes
        # how many words were flagged, confirmed, cleared or unchecked
        "counts": {"flagged_ctc": len(flagged), "confirmed": len(confirmed), "cleared_by_confirm": len(cleared),
                   # segments re-heard and near-review words
                   "unchecked": len(unchecked), "confirm_segments": len(conf_segs), "near_review": len(review)},
        "confirmed": confirmed,  # words both recognizers fail
        "cleared_by_confirm": cleared,  # words the confirm model heard right
        "unchecked": unchecked,  # words the confirm model never heard
        "near_review": review,  # near-match-only words for a human ear
        "watch": watch_out,  # per-occurrence detail of every word given with --watch
        "per_word": dict(sorted(sc["per_word"].items())),  # CTC per-word statistics, alphabetical
        "per_word_confirm": dict(sorted(sc2["per_word"].items())),  # confirm per-word statistics, alphabetical
        "occurrences": dict(sorted(occ.items())),  # per-token results, by token id
        "segments": sc["segments"],  # per-segment CTC error counts, with the confirm results added where re-heard
        # timing of both passes and of scoring; the total is filled in below
        "timing": {"ctc": st_ctc, "confirm": st_conf, "score_s": round(t_score, 2), "total_s": None},
    }
    # total seconds from the start until just before the file is written
    measure["timing"]["total_s"] = round(time.time() - t_all, 2)
    out_dir.mkdir(parents=True, exist_ok=True)  # create the output folder
    path = out_dir / "asr_measure.json"  # final location of the measurement, which the speechkit-narrate audit tools read later
    tmp = path.with_suffix(".tmp")  # written first as asr_measure.tmp so a reader never sees a half-written file
    tmp.write_text(json.dumps(measure, ensure_ascii=False, indent=1), encoding="utf-8")  # write the indented JSON
    os.replace(tmp, path)  # atomic rename into place

    t = sc["totals"]  # CTC totals for the summary
    log(f"wrote {path}")  # where the file went
    # CTC model, word error rate, words and segments scored
    log(f"ctc {st_ctc.get('model')}: WER {t['wer']:.2%} over {t['words_scored']} words in {t['segments_scored']}/"
        # segment total and the error breakdown
        f"{t['segments_total']} segments (sub {t['sub']}, del {t['del']}, ins {t['ins']}, near {t['near_matches']})")
    if sc2["totals"]:  # the confirm pass ran
        t2 = sc2["totals"]  # totals of the confirm pass, for its summary line
        # confirm model and its word error rate
        log(f"confirm {st_conf.get('model')}: WER {t2['wer']:.2%} over {t2['words_scored']} words in "
            f"{t2['segments_scored']} segments")  # the number of segments the confirm model re-heard
    # how many words the CTC pass flagged, how many both models fail and how many were cleared
    log(f"flagged by ctc: {len(flagged)}  confirmed by both: {len(confirmed)}  cleared: {len(cleared)}"
        # flagged words the confirm model never heard, which therefore have no verdict
        f"  unchecked: {len(unchecked)}")
    # header of the confirmed list
    log(f"top {min(top, len(confirmed))} confirmed (word xN  ctc ok-rate / confirm ok-rate):")
    for e in confirmed[:top]:  # the top confirmed words
        # each with count, both success rates and what the CTC model heard
        log(f"  {e['word']} x{e['count']}  {e['ctc_rate']:.0%}/{e['confirm_rate']:.0%}  ctc heard {_fmt_heard(e['ctc_heard'])}"
            f" | confirm heard {_fmt_heard(e['confirm_heard'])}")  # and what the confirm model heard
    for w, wo in watch_out.items():  # print the watched-word detail
        log(f"watch '{w}':")  # a heading line before this watched word's occurrences
        for tid, r in wo["occurrences"].items():  # each token where the watched word is expected, in token id order
            c = r.get("confirm", {})  # the confirm result, if any
            # token, segment, voice timing and the CTC result
            log(f"  token {tid} in {r['seg']} at {r.get('tts_t')}: ctc {r['status']} '{r['heard']}'"
                # the confirm result and its confidence
                f" | confirm {c.get('status')} '{c.get('heard')}' p={c.get('p')}")
    # near-review words that the confirm model also never heard exactly
    odd = [e for e in review if e.get("confirm_exact", 1) == 0]
    if odd:  # print this section only when such suspect words exist
        # header with the count
        log(f"near-match only, and the confirm model never heard it exactly either ({len(odd)} of {len(review)}):")
        for e in odd[:15]:  # at most 15 of them
            # word, count and both models' heard variants
            log(f"  {e['word']} x{e['count']}  ctc heard {_fmt_heard(e['heard'])} | confirm heard "
                f"{_fmt_heard(e.get('confirm_heard', {}))}")  # confirm variants (empty when not re-heard)
    # timing summary of the CTC pass
    log(f"time: ctc {st_ctc.get('wall_s')}s ({st_ctc.get('decoded')} decoded, {st_ctc.get('cached')} cached), "
        # confirm pass and the total
        f"confirm {st_conf.get('wall_s')}s ({st_conf.get('decoded')} decoded), total {measure['timing']['total_s']}s")
    return measure  # the same dict that was just written to asr_measure.json


# Command-line entry point with two subcommands: "run" (the whole probe) and "transcribe" (print transcripts
# of given WAVs as JSON). argv is the argument list or None for sys.argv. Returns 0 on success, 2 when an
# AsrError (bad model role, missing models) is raised.
def main(argv=None) -> int:
    # program name and the docstring's first line for --help
    ap = argparse.ArgumentParser(prog="emblem_rsi.pron.asr", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)  # a subcommand is mandatory
    # the run subcommand: decode, score, flag and confirm the whole narration
    r = sub.add_parser("run", help="transcribe all segments, score, flag, confirm; write asr_measure.json")
    r.add_argument("--request", default=str(DEFAULT_REQUEST))  # narration script to score
    r.add_argument("--speech-dir", default=str(DEFAULT_SPEECH))  # folder of narration WAVs and their timing JSON files
    r.add_argument("--docmodel", default=str(DEFAULT_DOCMODEL))  # document model for display text
    r.add_argument("--out", default=str(DEFAULT_OUT))  # output folder for asr_measure.json and the cache
    r.add_argument("--models-dir", default=DEFAULT_MODELS)  # where the recognizer models are
    # failure share that flags a word
    r.add_argument("--min-rate", type=float, default=FAIL_RATE, help="share of occurrences heard wrong that flags a word")
    # may be given several times
    r.add_argument("--watch", action="append", default=None, help="word to report on in detail (repeatable)")
    # restrict the run to some segments
    r.add_argument("--segments", default=None, help="comma-separated segment ids (default: all)")
    r.add_argument("--threads", type=int, default=2)  # CPU threads per recognizer
    r.add_argument("--top", type=int, default=40)  # how many confirmed words to print
    # the transcribe subcommand: decode the given files and print JSON, with no scoring
    t = sub.add_parser("transcribe", help="print transcripts of WAV files as JSON")
    t.add_argument("wavs", nargs="+")  # one or more audio files
    t.add_argument("--model", default="ctc", choices=sorted(MODELS))  # which recognizer role to use
    t.add_argument("--out", default=str(DEFAULT_OUT))  # its cache lives under this folder
    t.add_argument("--models-dir", default=DEFAULT_MODELS)  # where the sherpa-onnx models are unpacked
    t.add_argument("--threads", type=int, default=2)  # CPU threads the recognizer uses while decoding
    a = ap.parse_args(argv)  # parse argv (or the real command line when None); bad options exit with code 2
    try:  # AsrError becomes a clean message and exit code 2
        if a.cmd == "run":  # run subcommand: the full listening probe
            # positional arguments in run()'s order
            run(a.request, a.speech_dir, a.out, a.models_dir, a.docmodel, a.min_rate,
                a.watch if a.watch is not None else [], a.threads, a.top,  # watched words, threads, list size
                a.segments.split(",") if a.segments else None)  # comma-separated ids to a list, or None for all
        else:  # transcribe subcommand: decode the listed files only
            # transcribe the given files with the cache under --out
            res = transcribe(a.wavs, a.model, cache_dir=Path(a.out) / "cache", models_dir=a.models_dir, threads=a.threads)
            print(json.dumps(res, ensure_ascii=False, indent=1))  # print the transcripts as indented JSON
    except AsrError as e:  # a problem the user can fix, such as a missing model folder
        print(f"asr: {e}", file=sys.stderr)  # one line on stderr
        return 2  # exit code 2 signals a setup or usage error to the caller
    return 0  # exit code 0: the command finished normally


if __name__ == "__main__":  # only when run as a module or script
    raise SystemExit(main())  # SystemExit carries main()'s return code
