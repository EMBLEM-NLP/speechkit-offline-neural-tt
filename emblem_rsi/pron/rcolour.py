# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Unspelled-r probe: words heard with an r that is not in their spelling.

The voice is American (LJSpeech) but reads British espeak phonemes, where "door" and "daw" are both dɔː
and every "-er" ending is a plain ə. It learned to add the American r back from context, and sometimes
adds it where there is none: "Ana" comes out as "Arna", "drama" as "drummer", "jaw" as "jore".

An r (or r-coloured vowel) lowers the third formant (F3) sharply; for this voice a plain vowel keeps F3
around 2700-3200 Hz and an r pulls it to about 1600-2000 Hz. For every spoken word whose spelling has no
"r", F3 is tracked over the middle 60% of the word (to stay clear of a neighbouring word's r) and the 10th
percentile is taken. A word is listed when at least half of its occurrences (and at least one) dip below
the threshold.

Needs praat-parselmouth (pip install praat-parselmouth).
"""
from __future__ import annotations  # postponed annotations for modern type hints

import json  # segment timing records are JSON
import re  # strip non-letters from tokens and look for a spelled r
import sys  # stderr for progress messages when run as a script
from collections import defaultdict  # dictionary whose missing keys get a fresh per-word record automatically
from pathlib import Path  # file paths that work on Windows and Linux

import numpy as np  # percentiles and medians of the formant values

# F3 below 2100 Hz counts as r-coloured: a plain vowel from this voice keeps F3 near 2700-3200 Hz, an r near
# 1600-2000 Hz, so the threshold sits just above the r range.
THRESHOLD_HZ = 2100.0  # threshold in hertz separating plain vowels from r-coloured ones
_R = re.compile(r"r", re.I)  # matches the letter r in either case, anywhere in the word


# Measure the lowest third formant (F3) inside each word of one WAV, to detect r-colouring.
# Parameters: wav is the segment audio; spans maps token id to (start, end) seconds; step is the analysis hop in s.
# Returns {token id: 10th-percentile F3 in Hz}, skipping words too short or too quiet to measure.
# Needs praat-parselmouth; the import is local so a missing package raises ImportError only when this is called.
def word_tracks(wav: Path, spans: dict, step: float = 0.005) -> dict:
    import parselmouth  # Python bindings for Praat, the phonetics program
    snd = parselmouth.Sound(str(wav))  # load the segment WAV into Praat's sound object
    # Burg formant tracking every 5 ms, up to 5 formants below 5500 Hz (Praat's usual setting for a female voice)
    fm = snd.to_formant_burg(time_step=step, max_number_of_formants=5, maximum_formant=5500)
    it = snd.to_intensity(time_step=step)  # loudness track on the same 5 ms grid, used to ignore quiet frames
    out = {}  # token id mapped to its low third-formant value in Hz
    for tid, (a, b) in spans.items():  # every word span requested for this segment
        d = b - a  # word length in seconds from its start and end times
        if d < 0.06:  # words under 60 ms are too short for a stable formant track
            continue  # skip words too short for formant tracking
        # sample times across the middle 60% of the word, avoiding the edges shared with neighbouring words
        ts = np.arange(a + 0.2 * d, b - 0.2 * d, step)
        if len(ts) < 3:  # fewer than 3 samples is not enough for a percentile
            continue  # move on to the next word without a value
        # F3 in Hz at each sample time (NaN where Praat finds none)
        f3 = np.array([fm.get_value_at_time(3, t) for t in ts])
        db = np.array([it.get_value(t) for t in ts])  # intensity in dB at each sample time, for the loudness filter
        ok = np.isfinite(f3) & np.isfinite(db)  # keep only frames where both values exist
        if ok.sum() < 3:  # too few valid frames
            continue  # nothing reliable to measure for this word
        # also drop frames more than 12 dB below the word's loudest frame (closures, breath)
        ok &= db > np.nanmax(db[ok]) - 12
        if ok.sum() < 3:  # still need at least 3 frames
            continue  # the word was mostly quiet, so leave it out
        # 10th percentile: the deepest dip of F3, robust to a single tracking error
        out[tid] = float(np.percentile(f3[ok], 10))
    return out  # per-token third-formant results for this segment


# Measure unspelled r-colouring across the narration: every word without an r in its spelling, F3 per occurrence.
# Parameters: request (reference tokens), speech_dir (WAVs and .wav.json timing), segments (optional id subset),
# threshold in Hz, log (optional progress callback). Returns totals plus the flagged words, where a word is flagged
# when at least half of its occurrences dip below the threshold. Reads WAVs; writes nothing.
def run(request: dict, speech_dir: Path, segments=None, threshold: float = THRESHOLD_HZ, log=None) -> dict:
    """request: the reference request (tokens = what a listener should hear)."""
    # per-word record: occurrences, how many dipped low, the F3 values and token ids
    per_word = defaultdict(lambda: {"count": 0, "low": 0, "f3": [], "tokens": []})
    measured = 0  # total number of word occurrences measured
    # all request segments, or only the requested subset of ids
    segs = [s for s in request["segments"] if not segments or s["id"] in segments]
    for n, seg in enumerate(segs, 1):  # numbered from 1 so progress messages read naturally
        rec = Path(speech_dir) / f"{seg['id']}.wav.json"  # timing record written by the voice for this segment
        if not rec.exists():  # a segment without timing cannot be measured
            continue  # skip segments that have no timing record
        # token id -> (start, end) seconds within the segment WAV
        times = {w["token"]: (w["start"], w["end"]) for w in json.loads(rec.read_text(encoding="utf-8"))["words"]}
        want = {}  # tokens worth measuring: token id -> (word, span)
        for tok, tid in zip(seg["tokens"], seg["tokenIndex"]):  # walk the tokens with their ids
            # lowercase letters and apostrophes only, curly apostrophes normalised
            w = re.sub(r"[^a-z']", "", tok.lower().replace("’", "'"))
            # skip one-letter words, words spelled with r, tokens without timing and numbers (spoken digits may contain
            # r)
            if len(w) < 2 or _R.search(w) or tid not in times or any(c.isdigit() for c in tok):
                continue  # this token is not a candidate for the unspelled r check
            want[tid] = (w, times[tid])  # remember the word and its time span
        # measure F3 for all candidate words of this segment in one Praat pass
        f3 = word_tracks(Path(speech_dir) / f"{seg['id']}.wav", {t: sp for t, (_, sp) in want.items()})
        for tid, v in f3.items():  # fold each measurement into its word's record
            w = want[tid][0]  # the spelled word that this token measured
            e = per_word[w]  # per-word record (created on first use)
            e["count"] += 1  # count one more measured occurrence of the word
            e["low"] += v < threshold  # True counts as 1 when the dip is below the threshold
            e["f3"].append(round(v))  # keep the third-formant value rounded to whole hertz
            e["tokens"].append(tid)  # keep the token id for follow-up listening
            measured += 1  # overall count of measured word occurrences
        if log and n % 100 == 0:  # progress every 100 segments, when a logger was given
            log(f"rcolour: {n}/{len(segs)} segments")  # progress line such as "rcolour: 300/1200 segments"
    rows = []  # summary rows, one per word
    for w, e in per_word.items():  # each measured word with its accumulated record
        # word, counts, share of low occurrences, median F3, token ids and raw values
        rows.append({"word": w, "count": e["count"], "low": e["low"], "rate": round(e["low"] / e["count"], 4),
                     # median third formant in Hz, plus token ids and raw values
                     "f3_median": float(np.median(e["f3"])), "tokens": e["tokens"], "f3": e["f3"]})
    # flag words that dipped at least once and in at least half of their occurrences
    flagged = sorted([r for r in rows if r["low"] and r["rate"] >= 0.5],
                     key=lambda r: (-r["low"], r["f3_median"]))  # most low occurrences first, then the lowest median F3
    occ = sum(r["low"] for r in flagged)  # total flagged occurrences, the number reported to the measurement
    # result block with the threshold and measured totals
    return {"threshold_hz": threshold, "words_measured": measured, "unique": len(rows),
            # flagged rows and their counts
            "flagged": flagged, "flagged_words": len(flagged), "flagged_occurrences": occ,
            "low_occurrences_all": sum(r["low"] for r in rows)}  # low dips across all words, flagged or not


# Run as a module (`python -m emblem_rsi.pron.rcolour [OUT.json]`): measure the default narration and print a report.
if __name__ == "__main__":
    from . import asr, trial  # relative imports, so this only works when run with -m from the package
    # the narration request with the voice's spoken tokens
    req = json.loads(asr.DEFAULT_REQUEST.read_text(encoding="utf-8"))
    # the document model holding each token's reference spelling
    dm = json.loads(asr.DEFAULT_DOCMODEL.read_text(encoding="utf-8"))
    ref = trial.reference_request(req, dm)  # replace overridden tokens with what a listener should hear
    # measure with progress messages on stderr
    res = run(ref, asr.DEFAULT_SPEECH, log=lambda m: print(m, file=sys.stderr))
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else None  # optional output path from the first argument
    if out:  # write the JSON only when a path was given
        out.parent.mkdir(parents=True, exist_ok=True)  # make sure the output folder exists
        out.write_text(json.dumps(res, indent=1), encoding="utf-8")  # indented JSON so the result is easy to read
    # headline totals: measured words and low third-formant dips
    print(f"measured {res['words_measured']} words without a spelled r; low F3 in {res['low_occurrences_all']}; "
          # how many words were flagged and their occurrences
          f"flagged {res['flagged_words']} words ({res['flagged_occurrences']} occurrences)")
    for r in res["flagged"][:60]:  # list at most sixty flagged words in the report
        # word, low count over total, and median F3 in Hz
        print(f"  {r['word']:14s} {r['low']}/{r['count']}  F3 median {r['f3_median']:.0f}")
