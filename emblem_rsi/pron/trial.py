# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Acoustic trial of pronunciation fixes.

Each candidate spelling (or Piper raw-phoneme block, "[[...]]") is voiced in every real sentence where the
word occurs, several takes each because the voice is stochastic, then heard back by both offline
recognizers. The current reading is voiced the same way as the control, so a candidate is compared with
fresh takes of the current reading, not with one lucky or unlucky narration take.

    python3 -m emblem_rsi.pron.trial run --plan plan.json --out runs/pron-1/trial [--takes 3]

plan.json: {"words": {"boaz": ["Bo-az", "[[bˈəʊaz]]"], ...}}   (the current reading is always added)
"""
from __future__ import annotations  # postponed annotations, so `dict | None` hints work on every Python 3 in use

import argparse  # command line parsing for `python -m emblem_rsi.pron.trial run`
import json  # the plan, job specs, timing records and the trial result are JSON
import re  # word matching, sentence ends and safe job ids
import subprocess  # starts the synthesis workers in the speech runtime
import sys  # exit status when run as a script
import tempfile  # unnamed files that collect each synthesis worker's error output
from pathlib import Path  # file paths that work on Windows and Linux

from . import asr  # the recognizers, the text normaliser and the word scorer

from . import paths  # narration locations and voice files from environment variables
KARAOKE = paths.ROOT  # project root (kept for callers; not used in this module)
RUNTIME_PY = paths.RUNTIME_PY  # the speech runtime Python that has piper and onnxruntime installed
VOICE_MODEL = paths.VOICE_MODEL  # the aligned voice model (ONNX) used for trial takes
VOICE_CONFIG = paths.VOICE_CONFIG  # the voice's config JSON (phoneme settings, sample rate)
CURRENT = "(current)"  # label of the control variant: the narration's current reading of the word
# optional leading quotes or bracket, the word itself (letters, accents, apostrophes, hyphens), then the rest
_WORD = re.compile(r"^([“”‘’\"(]*)([A-Za-zÀ-ÿ'’-]+)([^A-Za-zÀ-ÿ]*)$")


# Map every token id to what a listener should hear: the spoken token, or its spoken_ref when it was overridden.
# Parameters: request is the narration request; docmodel is the document model dict or None.
# Returns {token id: reference text}. Pure; used so a respelling such as "Bo-az" is still scored as "Boaz".
def reference_tokens(request: dict, docmodel: dict | None) -> dict:
    """token id -> what the listener should hear (the spoken form before any pronunciation override)."""
    ref = {}  # mapping from global token id to the text a listener should hear
    for seg in request["segments"]:  # walk every segment of the narration request in order
        for tok, tid in zip(seg["tokens"], seg["tokenIndex"]):  # the spoken tokens and their ids
            ref[tid] = tok  # start from what the voice was given
    if docmodel:  # overrides are recorded in the document model
        for t in docmodel["tokens"]:  # scan each document token for a recorded reference reading
            # it has a reference reading and appears in the narration
            if t.get("spoken_ref") is not None and t["i"] in ref:
                ref[t["i"]] = t["spoken_ref"]  # use the reference reading instead of the respelling
    return ref  # complete id-to-reference mapping, ready for building the scoring request


# Copy of the request whose tokens are the reference readings, keeping every other segment field.
# Parameters as for reference_tokens. Returns {"segments": [...]}; this is what the recognizers are scored against.
def reference_request(request: dict, docmodel: dict | None) -> dict:
    ref = reference_tokens(request, docmodel)  # look up the reference reading for every token id
    # each segment copied with its tokens replaced, in tokenIndex order
    return {"segments": [{**s, "tokens": [ref[t] for t in s["tokenIndex"]]} for s in request["segments"]]}


# Key used to match a token to a plan word: its normalised words joined without spaces ("Boaz," becomes "boaz").
# Parameter token is a spoken token string. Returns a lowercase string (empty for pure punctuation).
def word_key(token: str) -> str:
    # the same normaliser the scorer uses, so plan keys and scored words agree
    return "".join(asr.normalize_expected(token))


# Put a candidate spelling into a token while keeping its surrounding punctuation.
# Parameters: token is the reference token (e.g. "Boaz,"); variant is a respelling, a "[[...]]" phoneme block or
# CURRENT. Returns the new token text, e.g. "Bo-az,"; a capital letter is kept for spellings, not phoneme blocks.
def apply_variant(token: str, variant: str) -> str:
    """Replace the word inside a spoken token, keeping its punctuation (and a leading capital for spellings)."""
    if variant == CURRENT:  # the control variant leaves the token untouched
        return token  # the narration's current token is returned without any substitution
    m = _WORD.match(token)  # split into leading punctuation, word and trailing part
    if not m:  # the token is not a simple word (for example it contains digits)
        return variant  # replace the whole token with the variant
    word = m.group(2)  # the bare word between the leading and trailing punctuation groups
    # a capitalised word keeps its capital, except inside a raw phoneme block
    if not variant.startswith("[[") and word[:1].isupper():
        variant = variant[:1].upper() + variant[1:]  # capitalise the first character of the respelling
    return m.group(1) + variant + m.group(3)  # reattach the original punctuation around the variant


# Find the sentence, within its segment, that contains a given token, so the candidate is voiced in context.
# Parameters: seg is a segment dict; tid is a token id. Returns (tokens, ids) of that sentence, or the whole
# segment if the id is not found. Sentences end at tokens ending in . ! or ?, as in the narration.
def sentence_of(seg: dict, tid: int) -> tuple[list, list]:
    """Spoken tokens and ids of the sentence (within its segment) that contains token tid."""
    toks, ids = seg["tokens"], seg["tokenIndex"]  # spoken tokens and their ids
    bounds, start = [], 0  # (start, end) position pairs of each sentence, and the current start
    for k, t in enumerate(toks):  # walk the tokens with their positions to find sentence boundaries
        # a sentence end, or the last token closing the final sentence
        if re.search(r"[.!?]$", t) or k == len(toks) - 1:
            bounds.append((start, k + 1))  # remember this sentence as a half-open range of token positions
            start = k + 1  # the next sentence starts after this token
    for a, b in bounds:  # check each sentence range in turn for the target token
        if tid in ids[a:b]:  # the target token id falls inside this sentence's slice of ids
            return toks[a:b], ids[a:b]  # hand back only that sentence's tokens and their matching ids
    return toks, ids  # fallback when the id is missing: voice the whole segment instead


# Every occurrence of a plan word in the reference request, in narration order.
# Parameters: ref_request is the reference request; word is a plan key. Returns [(segment dict, token id), ...].
def occurrences(ref_request: dict, word: str) -> list:
    out = []  # list of (segment, token id) pairs where the word occurs
    for seg in ref_request["segments"]:  # scan every segment of the reference request
        for tok, tid in zip(seg["tokens"], seg["tokenIndex"]):  # pair each reference token with its global token id
            if word_key(tok) == word:  # the token normalises to the plan word
                out.append((seg, tid))  # remember the segment and the token id of this occurrence
    return out  # occurrences in narration order, earliest first


# The length scale (speaking-rate factor) the narration used for a segment, so trial takes match its pace.
# Parameters: sid is the segment id; speech_dir holds <id>.wav.json. Returns a float (larger is slower).
def segment_scale(sid: str, speech_dir: Path) -> float:
    try:  # any problem with the record falls back to the default
        # the timing record written when the segment was voiced
        rec = json.loads((speech_dir / f"{sid}.wav.json").read_text(encoding="utf-8"))
        return float(rec["attempts"][-1]["lengthScale"])  # lengthScale of the final synthesis attempt
    except Exception:  # missing file, missing key or bad JSON
        return 1.24  # 1.24 is the narration's usual length scale


# Build one synthesis job per word, variant, occurrence and take.
# Parameters: ref_request (reference tokens), words ({plan word: [variants]}), takes per case, wav_dir (output
# folder), speech_dir (for length scales), max_occurrences per word, voiced (token id -> current spoken form).
# Returns a list of job dicts with the sentence text to voice and where to write it. Pure; nothing is voiced here.
def build_jobs(ref_request: dict, words: dict, takes: int, wav_dir: Path, speech_dir: Path,
               # at most 9 occurrences per word by default, plus the current spoken forms
               max_occurrences: int = 9, voiced: dict | None = None) -> list:
    """voiced: token id -> spoken form the narration currently uses (other overrides stay in force)."""
    jobs = []  # accumulated synthesis jobs across all words and variants
    for word, variants in words.items():  # every plan word and its candidate spellings
        occ = occurrences(ref_request, word)[:max_occurrences]  # its occurrences, capped to keep the trial short
        # the control (current reading) first as v0, then each distinct candidate: dict.fromkeys drops repeats
        # (and any CURRENT in the plan, already first) while keeping the plan's order
        for vi, variant in enumerate(dict.fromkeys([CURRENT, *variants])):
            for oi, (seg, tid) in enumerate(occ):  # every capped occurrence of the word, numbered for the job id
                # the surrounding sentence gives the candidate realistic context and prosody
                toks, ids = sentence_of(seg, tid)
                # what the narration currently says for each token, so other overrides stay in force
                spoken = [voiced.get(i, t) if voiced else t for t, i in zip(toks, ids)]
                # only real candidates change the text; the control keeps the narration's wording
                if variant != CURRENT:
                    # swap the candidate in at the target token only; every other token keeps its voiced form
                    spoken = [apply_variant(t, variant) if i == tid else s for t, s, i in zip(toks, spoken, ids)]
                text = " ".join(spoken)  # the sentence text the voice will read
                for k in range(takes):  # several takes, because the voice varies from take to take
                    # job id such as boaz_v1_o2_t0: word, variant, occurrence and take numbers
                    jid = f"{re.sub(r'[^a-z0-9]', '', word)}_v{vi}_o{oi}_t{k}"
                    # job record: id, word, variant, segment and target token id
                    jobs.append({"id": jid, "word": word, "variant": variant, "seg": seg["id"], "tid": tid,
                                 # reference tokens and ids for scoring, and the text to voice
                                 "ref_tokens": toks, "ids": ids, "text": text,
                                 # the segment's speaking rate and the WAV path
                                 "scale": segment_scale(seg["id"], speech_dir), "out": str(wav_dir / f"{jid}.wav")})
    return jobs  # complete job list, ready for synthesize() and scoring


# Voice every job whose WAV does not exist yet, using parallel worker processes in the speech runtime.
# Parameters: jobs from build_jobs; workers is the number of processes (default 2). Returns nothing.
# Side effects: starts `_trial_synth.py` workers and writes WAV files; raises RuntimeError if a worker fails.
def synthesize(jobs: list, workers: int = 2) -> None:
    # skip takes already on disk, so an interrupted trial can be resumed
    todo = [j for j in jobs if not Path(j["out"]).exists()]
    if not todo:  # every take already exists on disk from an earlier run
        return  # nothing left to synthesise, so return early
    n = max(1, min(workers, len(todo)))  # never more workers than jobs, and at least one
    runtime = paths.need(RUNTIME_PY, "EMBLEM_PIPER_PYTHON", "the speechkit runtime Python")  # fail clearly when unset
    voice_config = paths.need(VOICE_CONFIG, "EMBLEM_VOICE_CONFIG", "the voice config JSON")  # likewise
    procs = []  # (process, error file) of each running worker
    for k in range(n):  # one worker per share of the jobs
        part = todo[k::n]  # round-robin slice: every n-th job, so worker shares differ by at most one
        spec = {"model": str(VOICE_MODEL), "config": str(voice_config),  # voice model and config paths for the worker
                # the worker needs only each job's text, length scale and output path
                "jobs": [{"text": j["text"], "scale": j["scale"], "out": j["out"]} for j in part]}
        # The worker's error output goes to an unnamed temporary file and its progress lines (one per job) are
        # discarded. Neither stream is a pipe, so no worker can block on a full pipe buffer while this process
        # waits on another worker, however many jobs each one has.
        err = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        # the runtime Python in isolated mode runs the helper script beside this file
        p = subprocess.Popen([runtime, "-I", str(Path(__file__).with_name("_trial_synth.py"))],
                             # the job spec goes in through a UTF-8 text pipe
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err, text=True,
                             encoding="utf-8", errors="replace")
        p.stdin.write(json.dumps(spec, ensure_ascii=False))  # send the job spec as JSON on stdin
        p.stdin.close()  # close stdin so the worker sees the end of its input
        procs.append((p, err))  # keep the handle and its error file so the exit status can be checked later
    failed = []  # error text of every worker that failed
    for p, err in procs:  # wait for every worker, collecting failures
        code = p.wait()  # block until this worker exits
        err.seek(0)  # rewind the error file to read what the worker wrote
        text = err.read()  # its whole error output
        err.close()  # the unnamed file disappears when closed
        if code != 0:  # a non-zero exit status means synthesis failed
            failed.append(text[-1500:])  # keep the last 1500 characters of the error output
    if failed:  # at least one worker failed
        raise RuntimeError(f"trial synthesis failed: {failed[0]}")  # show the first failure


# Score every trial take with each recognizer, focusing on the target word.
# Parameters: jobs from build_jobs; transcripts maps model name to {job id: transcript}. Returns
# {job id: {model: {status, heard, p, asr_t, other_errors, other_words}}}. Pure computation.
def score_jobs(jobs: list, transcripts: dict) -> dict:
    """Per job and model: the target word's status/heard/p, and errors on the other words of the sentence."""
    # token ids must be unique across jobs: several jobs voice the same sentence, and score() keys by token id
    # The offset is 10 million per job, so job k's ids start at (k + 1) * 10,000,000.
    off = {j["id"]: (n + 1) * 10_000_000 for n, j in enumerate(jobs)}
    # one pseudo-segment per job: its reference sentence with offset token ids
    req = {"segments": [{"id": j["id"], "tokens": j["ref_tokens"], "tokenIndex": [off[j["id"]] + i for i in j["ids"]]}
                        for j in jobs]}  # one pseudo-segment for every job, in job order
    out = {}  # results per job and model
    for model, tr in transcripts.items():  # each recognizer's transcripts, keyed by job id
        sc = asr.score(req, tr)  # align and score every take against its reference sentence
        by_seg = {}  # occurrences grouped by job (segment) id
        for tid, o in sc["occurrences"].items():  # every scored token occurrence from this recognizer
            by_seg.setdefault(o["seg"], {})[tid] = o  # group the occurrence under its job, keyed by offset token id
        for j in jobs:  # look up each job's results for this recognizer
            occ = by_seg.get(j["id"], {})  # scored token occurrences belonging to this take
            tid = off[j["id"]] + j["tid"]  # the target token's id after applying this job's offset
            tgt = occ.get(tid, {"status": "del", "heard": asr.DEL})  # a target the scorer never saw counts as deleted
            # how many other words of the sentence were heard wrong
            other = sum(1 for t, o in occ.items() if t != tid and o["status"] != "ok")
            # the target's status and what was heard
            out.setdefault(j["id"], {})[model] = {"status": tgt["status"], "heard": tgt["heard"],
                                                  # its lowest token probability and its time span in the take
                                                  "p": tgt.get("p"), "asr_t": tgt.get("asr_t"),
                                                  # collateral errors and how many other words there were
                                                  "other_errors": other, "other_words": max(0, len(occ) - 1)}
    return out  # nested results: job id, then model name, then target statistics


# Vowel quality at the start of a word: median first and second formants (F1, F2) over its loudest early frames.
# Parameters: wav is the take; span is the word's (start, end) in seconds from the recognizer; share is the
# fraction of the word to examine (0.4). Returns [F1, F2] in Hz, or None without a span, without Praat, or with
# too few usable frames. Used for plan words listed under "vowel_words".
def onset_formants(wav: str, span, share: float = 0.4):
    """Median F1/F2 (Hz) over the loudest frames of the first `share` of a word: the quality of its first
    vowel (e.g. American "door" is about F1 500 / F2 900 for this voice; "dar" is F1 750 / F2 1200)."""
    if not span:  # no time span for the word means nothing can be located
        return None  # without a span there is no audio region to measure
    try:  # numpy and parselmouth are optional dependencies for this measurement
        import numpy as np  # numpy for the sample-time grid and the medians
        import parselmouth  # Praat bindings for formant tracking
    except ImportError:  # either optional package is missing on this machine
        return None  # None tells the caller the vowel was not measured
    snd = parselmouth.Sound(wav)  # load the take's WAV into Praat's sound object
    # formant tracks every 5 ms, 5 formants up to 5500 Hz (Praat's setting for a female voice)
    fm = snd.to_formant_burg(time_step=0.005, max_number_of_formants=5, maximum_formant=5500)
    it = snd.to_intensity(time_step=0.005)  # loudness track on the same 5 ms grid, used to pick vowel frames
    # the first 40% of the word, clipped to the audio's length
    a, b = max(0.0, span[0]), min(snd.duration, span[0] + share * (span[1] - span[0]))
    ts = np.arange(a, b, 0.005)  # analysis times every 5 ms across the examined part of the word
    if len(ts) < 3:  # under 3 samples gives no reliable median
        return None  # too few frames for a stable median, so report nothing
    # first formant in Hz at each analysis time (vowel openness)
    f1 = np.array([fm.get_value_at_time(1, t) for t in ts])
    # second formant in Hz at each analysis time (front or back vowel)
    f2 = np.array([fm.get_value_at_time(2, t) for t in ts])
    db = np.array([it.get_value(t) for t in ts])  # intensity in dB at each analysis time, for the loudness filter
    # keep frames with all values present and within 6 dB of the loudest, i.e. the vowel itself
    ok = np.isfinite(f1) & np.isfinite(f2) & np.isfinite(db) & (db > np.nanmax(db) - 6)
    if ok.sum() < 3:  # fewer than three usable frames survive the filters
        return None  # give up on this take rather than report a noisy value
    # median first and second formants, rounded to whole hertz
    return [round(float(np.median(f1[ok]))), round(float(np.median(f2[ok])))]


# Lowest third formant (F3) of a word, as a measure of r-colouring.
# Parameters: wav is the take; span is (start, end) seconds. Returns the 10th-percentile F3 in Hz, or None.
def f3_low(wav: str, span) -> float | None:
    """10th-percentile third formant (Hz) over a time span: low values (about < 2200 Hz for this voice)
    mean the sound is r-coloured. Needs praat-parselmouth; returns None without it."""
    if not span:  # no time span means the word was never located in the take
        return None  # without a span there is nothing to measure
    try:  # numpy and parselmouth are optional dependencies here
        import numpy as np  # numpy for the time grid and the percentile
        import parselmouth  # Praat bindings provide the Burg formant tracker
    except ImportError:  # either optional package is missing on this machine
        return None  # skip the measurement and report None
    snd = parselmouth.Sound(wav)  # load the take's WAV into Praat's sound object
    # formant tracks every 5 ms, five formants below 5500 Hz
    fm = snd.to_formant_burg(time_step=0.005, max_number_of_formants=5, maximum_formant=5500)
    it = snd.to_intensity(time_step=0.005)  # intensity track on the same 5 ms grid for the loudness filter
    # sample times across the whole word, clipped to the audio
    ts = np.arange(max(0.0, span[0]), min(snd.duration, span[1]), 0.005)
    if len(ts) < 3:  # fewer than three analysis times inside the word span
        return None  # too short to judge r-colouring reliably, so report None
    f3 = np.array([fm.get_value_at_time(3, t) for t in ts])  # third formant in Hz at each analysis time
    db = np.array([it.get_value(t) for t in ts])  # loudness in dB at each analysis time, for discarding quiet frames
    # frames with valid values, within 15 dB of the word's loudest frame
    ok = np.isfinite(f3) & np.isfinite(db) & (db > np.nanmax(db) - 15)
    # 10th percentile in whole Hz, when at least 3 frames qualify
    return round(float(np.percentile(f3[ok], 10)), 0) if ok.sum() >= 3 else None


# Module-level set filled by run() from the plan, read by summarize(); not safe for concurrent trials.
plan_vowel_words: set = set()   # words whose first-vowel formants are reported (plan key "vowel_words")


# Summarise the scored takes per word and variant, and pick a verdict for the best candidate.
# Parameters: jobs from build_jobs; scores from score_jobs. Returns {word: {variant: stats, "_best": {variant,
# verdict}}}; stats hold ok_rate, exact_rate, p_mean, F3 median, collateral error rate and what was heard.
# Reads the WAVs for formant measurements; writes nothing.
def summarize(jobs: list, scores: dict) -> dict:
    res = {}  # word -> variant -> running totals
    for j in jobs:  # fold every synthesis job into its word and variant record
        # the running record for this word and variant, created on first use
        r = res.setdefault(j["word"], {}).setdefault(j["variant"], {
            # takes, recognizer hits, probabilities, F3 values and collateral counts
            "n": 0, "ctc_ok": 0, "confirm_ok": 0, "p": [], "f3": [], "other_errors": 0, "other_words": 0,
            "heard": {}, "example": j["text"]})  # what was heard, and an example sentence
        s = scores[j["id"]]  # results from both recognizers for this particular take
        if j.get("out") and Path(j["out"]).exists():  # the WAV exists, so acoustic measurements are possible
            # F3 over the target word, using the confirm model's timing or else the ctc timing
            f = f3_low(j["out"], s["confirm"].get("asr_t") or s["ctc"].get("asr_t"))
            if f is not None:  # the third-formant measurement succeeded for this take
                r["f3"].append(f)  # keep the value for the per-variant median
            if j.get("word") in plan_vowel_words:  # vowel quality is requested for this word
                # first and second formants near the start of the word
                v = onset_formants(j["out"], s["confirm"].get("asr_t") or s["ctc"].get("asr_t"))
                if v:  # the formant measurement succeeded for this take
                    r.setdefault("onset", []).append(v)  # collect it for the per-variant medians
        r["n"] += 1  # count one more scored take for this variant
        r["ctc_ok"] += s["ctc"]["status"] == "ok"  # fast recognizer heard the target right
        r["confirm_ok"] += s["confirm"]["status"] == "ok"  # confirm recognizer heard it right
        # heard exactly the target spelling (not just a near match), counted over both models
        r["exact"] = r.get("exact", 0) + sum(s[m]["heard"].replace(" ", "") == j["word"] for m in ("ctc", "confirm"))
        if s["confirm"]["p"] is not None:  # the confirm model reported a probability
            r["p"].append(s["confirm"]["p"])  # collect the confirm probability for the per-variant mean
        # collateral errors on other words, summed over both recognizers
        r["other_errors"] += s["ctc"]["other_errors"] + s["confirm"]["other_errors"]
        # number of other words scored, summed over both recognizers
        r["other_words"] += s["ctc"]["other_words"] + s["confirm"]["other_words"]
        for m in ("ctc", "confirm"):  # tally what each recognizer heard for the target word
            h = s[m]["heard"]  # heard text for the target word from this recognizer
            r["heard"][h] = r["heard"].get(h, 0) + 1  # count how often each heard variant occurred
    for word, vs in res.items():  # turn totals into rates for every word
        for v, r in vs.items():  # every variant of this word, including the current reading
            # share of (take, model) pairs that heard the word right
            r["ok_rate"] = round((r["ctc_ok"] + r["confirm_ok"]) / (2 * r["n"]), 4)
            r["exact_rate"] = round(r.get("exact", 0) / (2 * r["n"]), 4)  # share that heard the exact spelling
            r["p_mean"] = round(sum(r["p"]) / len(r["p"]), 4) if r["p"] else None  # mean confirm probability, or None
            # upper median F3 in Hz (middle of the sorted list), or None
            r["f3_median"] = sorted(r["f3"])[len(r["f3"]) // 2] if r["f3"] else None
            del r["f3"]  # the raw list is not needed in the output
            if r.get("onset"):  # formant samples were collected
                # median F1 in Hz across the takes of this variant
                r["onset_f1_f2"] = [sorted(x[0] for x in r["onset"])[len(r["onset"]) // 2],
                                    # median second formant in Hz, sorted separately from the first
                                    sorted(x[1] for x in r["onset"])[len(r["onset"]) // 2]]
                del r["onset"]  # drop the raw formant samples to keep trial.json compact
            # share of other words heard wrong, 0 when there were none
            r["other_error_rate"] = round(r["other_errors"] / r["other_words"], 4) if r["other_words"] else 0.0
            r["heard"] = dict(sorted(r["heard"].items(), key=lambda kv: -kv[1]))  # heard variants, most frequent first
            del r["p"]  # raw probabilities are no longer needed
        base = vs[CURRENT]  # the control: the narration's current reading
        ranked = sorted((v for v in vs if v != CURRENT),  # candidate variants sorted best first, excluding the control
                        # sort key: highest ok rate, then highest mean confirm probability
                        key=lambda v: (-vs[v]["ok_rate"], -(vs[v]["p_mean"] or 0)))
        best = ranked[0] if ranked else None  # the top-ranked candidate, or None when the plan listed none
        verdict = "keep current"  # default verdict when no candidate clearly beats the narration
        if best is not None:  # there is at least one candidate to compare with the control
            b = vs[best]  # statistics of the best candidate variant
            gain = b["ok_rate"] - base["ok_rate"]  # improvement in hit rate over the current reading
            # extra errors on the other words of the sentence
            collateral = b["other_error_rate"] - base["other_error_rate"]
            # at least 25 points better with at most 3 points more collateral errors
            if gain >= 0.25 and collateral <= 0.03:
                verdict = "adopt"  # clear improvement without collateral damage: recommend adopting the spelling
            elif gain > 0 and collateral <= 0.03:  # any improvement without collateral harm
                verdict = "weak gain"  # some improvement, worth another trial before deciding
        vs["_best"] = {"variant": best, "verdict": verdict}  # recorded under a key starting with "_" so tables skip it
    return res  # per-word summaries keyed by variant, each with a _best verdict


# Run a full trial: voice every candidate in its real sentences, transcribe with both models, score, summarise.
# Parameters: plan ({"words", optional "vowel_words", "max_occurrences"}), out_dir, takes, workers, the narration
# paths (request, docmodel, speech, models) and log. Returns the result dict. Side effects: writes OUT/wav/*.wav,
# an ASR cache under OUT/asr-cache and OUT/trial.json; starts synthesis and recognition work.
def run(plan: dict, out_dir: Path, takes: int = 3, workers: int = 2, request_path=asr.DEFAULT_REQUEST,
        # narration inputs default to the paths in paths.py
        docmodel_path=asr.DEFAULT_DOCMODEL, speech_dir=asr.DEFAULT_SPEECH, models_dir=asr.DEFAULT_MODELS,
        log=print) -> dict:  # progress messages go to print unless a logger is given
    out_dir = Path(out_dir)  # accept a plain string path from the command line
    wav_dir = out_dir / "wav"  # subfolder that receives every trial take as a WAV file
    wav_dir.mkdir(parents=True, exist_ok=True)  # create the output folders, including parents, if missing
    # current narration request, with overrides already applied to tokens
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    # document model when present, for reference readings
    docmodel = json.loads(Path(docmodel_path).read_text(encoding="utf-8")) if Path(docmodel_path).exists() else None
    ref_req = reference_request(request, docmodel)  # reference request: what a listener should hear
    # token id -> what the narration currently says, including overrides
    voiced = {tid: tok for s in request["segments"] for tok, tid in zip(s["tokens"], s["tokenIndex"])}
    plan_vowel_words.clear()  # forget vowel words from an earlier trial in this process
    plan_vowel_words.update(plan.get("vowel_words", []))  # words whose vowel formants the plan asks for
    # every take to voice, across words, variants, occurrences and takes
    jobs = build_jobs(ref_req, plan["words"], takes, wav_dir, Path(speech_dir),
                      # occurrence cap from the plan, and the current spoken forms
                      plan.get("max_occurrences", 9), voiced)
    log(f"trial: {len(plan['words'])} words, {len(jobs)} takes to voice")  # how much work lies ahead
    synthesize(jobs, workers)  # voice every take that is not already on disk
    wavs = [j["out"] for j in jobs]  # WAV paths of every take, in job order
    # transcribe every take with both recognizers, cached under the trial folder
    transcripts = {m: asr.transcribe(wavs, m, cache_dir=out_dir / "asr-cache", models_dir=models_dir)
                   for m in ("ctc", "confirm")}  # the fast ctc model and the stronger confirm model
    scores = score_jobs(jobs, transcripts)  # score the target word in every take
    summary = summarize(jobs, scores)  # per-word, per-variant statistics and verdicts
    # the full result: schema, settings, plan and summary
    result = {"schema": "emblem-rsi/pron-trial/1", "takes": takes, "plan": plan, "summary": summary,
              # plus each job's identity and scores, merged with the dict union operator
              "jobs": [{k: j[k] for k in ("id", "word", "variant", "seg", "tid", "text")} | {"scores": scores[j["id"]]}
                       for j in jobs]}  # one entry per take, merged with its scores
    # save it as JSON, keeping non-ASCII spellings readable
    (out_dir / "trial.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return result  # full result dictionary for the caller to print or inspect


# Format a trial summary as a fixed-width text table, one row per word and variant, marking the best candidate.
# Parameter summary is the dict from summarize(). Returns a single string with newline-separated rows.
def table(summary: dict) -> str:
    rows = []  # formatted table lines, one per word and variant
    for word, vs in summary.items():  # each word in the summary together with its variant records
        best = vs.get("_best", {})  # best candidate and verdict recorded by summarize()
        for v, r in vs.items():  # each variant's statistics for this word, including the control
            if v.startswith("_"):  # skip bookkeeping keys such as _best
                continue  # no table row for it; go on to the next variant
            # mark the best candidate with its verdict
            mark = " <- " + best["verdict"] if v == best.get("variant") else ""
            # the three most frequent things heard, with counts
            heard = ", ".join(f"{h} x{n}" for h, n in list(r["heard"].items())[:3])
            if r.get("onset_f1_f2"):  # vowel formants were measured
                # show onset F1/F2 in Hz before the heard list
                heard = f"onset F1/F2 {r['onset_f1_f2'][0]}/{r['onset_f1_f2'][1]}  " + heard
            # word, variant, ok and exact rates, mean probability, median F3
            rows.append(f"{word:12s} {v:22s} ok {r['ok_rate']:.2f} exact {r.get('exact_rate', 0):.2f}  p {r['p_mean'] or 0:.2f}  F3 {r.get('f3_median') or 0:5.0f}  "
                        # collateral error rate, what was heard and the verdict mark
                        f"other-err {r['other_error_rate']:.3f}  heard: {heard}{mark}")
    return "\n".join(rows)  # join the rows into one printable multi-line string


# Script entry point: `python -m emblem_rsi.pron.trial run --plan PLAN --out DIR [--takes N] [--workers N]`.
# Parameter argv is an optional argument list. Returns 0 after printing the ranking table.
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="emblem_rsi.pron.trial")  # parser named after the module
    sub = ap.add_subparsers(dest="cmd", required=True)  # subcommand dispatcher; a subcommand name is mandatory
    r = sub.add_parser("run")  # the only subcommand, which runs a whole trial
    r.add_argument("--plan", required=True)  # path to the plan JSON with words and candidate spellings
    r.add_argument("--out", required=True)  # output folder for takes, cache and trial.json
    r.add_argument("--takes", type=int, default=3)  # takes per word, variant and occurrence (default 3)
    r.add_argument("--workers", type=int, default=2)  # parallel synthesis worker processes (default 2)
    a = ap.parse_args(argv)  # parse the command line into an options namespace
    # load the plan and run the trial
    res = run(json.loads(Path(a.plan).read_text(encoding="utf-8")), Path(a.out), a.takes, a.workers)
    print(table(res["summary"]))  # print the ranking table built from the summary
    return 0  # exit status 0 after a completed trial


if __name__ == "__main__":  # run main only when executed as a script
    sys.exit(main())  # exit the interpreter with main's return value
