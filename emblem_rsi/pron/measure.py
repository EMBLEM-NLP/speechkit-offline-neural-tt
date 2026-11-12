# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Pronunciation measurement for the emblem-rsi loop.

One measurement hears the whole narration back and checks every spoken word:
  * words tracked: every spoken token has a time span in the narration and was scored by the recognizer;
  * ASR probe (asr.py): word error rate, words both recognizers mishear (confirmed), their occurrences;
  * lexicon probe (lexicon.py, when present): voice phonemes vs the CMU dictionary.

What a listener should hear is the reference reading (the spoken form before any pronunciation override),
so a respelling such as "Bo-az" is still scored against "Boaz".

The measurement file has the shape loop.decide() expects (schema emblem-rsi.measurement/1):
  page_sha256       hash of the narration script plus the pronunciation overrides (what was changed)
  signature_sha256  hash of every narration WAV (what was heard)
  probe_sha256      hash of the probe code
"""
from __future__ import annotations  # postponed annotations, so `Path | None` hints parse on every Python 3 in use

import hashlib  # SHA-256 fingerprints of the script, overrides, audio and probe code
import json  # reads the request, docmodel and overrides, writes the measurement
import os  # creates the cache symlink and computes relative paths
from pathlib import Path  # file paths that work on Windows and Linux

from .. import loop  # the generic loop: seal checks, hashing helpers, schema tag, timestamps
from . import asr, trial  # the speech recognition probe and the reference-reading helper

HERE = Path(__file__).resolve().parent  # folder holding the probe modules, hashed as the probe fingerprint
from . import paths  # narration locations taken from environment variables
KARAOKE = paths.ROOT  # project root of the narration being audited
DEFAULT_OVERRIDES = paths.OVERRIDES  # the pronunciation overrides file inside work/
# the bundled pronunciation rubric that `pron seal` uses by default
RUBRIC = HERE.parent / "rubrics" / "pronunciation.json"


# SHA-256 of a bytes value as 64 hex characters. Parameter b is the data to hash.
def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()  # hex form, the way every fingerprint in the loop is stored


# Fingerprint of "the page" in loop terms: the narration script plus the pronunciation overrides.
# Parameters: request_path is request.json; overrides_path is pronunciations.json or None.
# Returns one hex digest; a missing overrides file is recorded as None, so creating one changes the hash.
def page_sha256(request_path: Path, overrides_path: Path | None) -> str:
    parts = {"request": loop.sha256_file(request_path),  # hash of the script the voice was given
             # hash of the overrides when they exist, else None
             "overrides": loop.sha256_file(overrides_path) if overrides_path and Path(overrides_path).exists() else None}
    return _sha_bytes(loop.canonical(parts))  # canonical JSON of both parts, hashed again into a single value


# Fingerprint of "the signature" in loop terms: every narration WAV that was heard, in segment order.
# Parameters: speech_dir holds <id>.wav files; segment_ids lists the segments measured. Returns a hex digest.
# A re-voiced segment changes its WAV hash and therefore this value; a missing WAV is recorded as "missing".
def signature_sha256(speech_dir: Path, segment_ids: list) -> str:
    h = hashlib.sha256()  # one running hash over all segments
    for sid in segment_ids:  # walk the segments in the order given, so ordering changes the hash
        p = Path(speech_dir) / f"{sid}.wav"  # path of the segment's narration WAV file
        # feed "id:hash" lines, so both the order and the contents matter
        h.update(f"{sid}:{asr.sha256_file(p) if p.exists() else 'missing'}\n".encode())
    return h.hexdigest()  # combined digest of every segment's audio


# Fingerprint of the probe code: every module in this folder except the package marker.
# Takes no parameters; returns a hex digest. Editing any probe file changes it, which the log records.
def probe_sha256() -> str:
    # sorted for a stable order; __init__.py holds only the package docstring
    files = sorted(p for p in HERE.glob("*.py") if p.name not in ("__init__.py",))
    # map file name -> file hash, then hash the canonical JSON of that map
    return _sha_bytes(loop.canonical({p.name: loop.sha256_file(p) for p in files}))


# Count spoken tokens that are fully tracked: timed in the segment's .wav.json and scored by the recognizer.
# Parameters: request (reference tokens), speech_dir (timing records), occurrences (token id -> ASR record).
# Returns (tracked, total). Feeds the words_tracked_ratio gate, which must be 1.0 for a promotion.
def words_tracked(request: dict, speech_dir: Path, occurrences: dict) -> tuple[int, int]:
    """(tracked, total) spoken tokens: tracked = has a time span in its WAV record and was scored by the recognizer."""
    total = tracked = 0  # both the tracked and total counters start at zero
    for seg in request["segments"]:  # every segment included in this pronunciation measurement
        rec = Path(speech_dir) / f"{seg['id']}.wav.json"  # the voice's timing record for this segment
        spans = {}  # token id -> (start, end) seconds; empty when the record is missing
        if rec.exists():  # a missing record leaves every token in the segment untracked
            # read the word timings written by the voice
            spans = {w["token"]: (w["start"], w["end"]) for w in json.loads(rec.read_text(encoding="utf-8")).get("words", [])}
        for tok, tid in zip(seg["tokens"], seg["tokenIndex"]):  # every spoken token together with its global id
            if not asr.normalize_expected(tok):  # tokens that produce no words (punctuation, symbols)
                # They are skipped and not counted in the total.
                continue          # punctuation-only tokens are not words
            total += 1  # count one more spoken word in the total
            sp = spans.get(tid)  # its recorded time span, or None when missing
            # needs a real span (end after start) and an ASR result; keys may be ints or JSON strings
            if sp and sp[1] > sp[0] and (tid in occurrences or str(tid) in occurrences):
                tracked += 1  # this token is timed and scored, so it counts as tracked
    return tracked, total  # numerator and denominator of the tracked-words ratio


# Count occurrences of confirmed words that both recognizers got wrong.
# Parameter am is the asr_measure dict from asr.run. Returns an int for the asr_confirmed_occurrences metric.
def confirmed_occurrences(am: dict) -> int:
    """Occurrences of confirmed words that both recognizers got wrong."""
    words = {e["word"] for e in am["confirmed"]}  # the confirmed words as a set for fast lookups
    n = 0  # running count of failed confirmed-word occurrences
    for rec in am["occurrences"].values():  # every scored token occurrence from the fast recognizer
        # the fast model failed it, and the confirm model also failed it (a missing confirm result counts as ok)
        if rec["status"] != "ok" and rec.get("confirm", {}).get("status", "ok") != "ok":
            if set(rec["expected"].split()) & words:  # the token holds at least one confirmed word
                n += 1  # count one failed occurrence of a confirmed word
    return n  # total failed occurrences of confirmed words


# Run the dictionary probe (lexicon.py) and turn its result into loop metrics.
# Parameters: karaoke_root is the narration project, out_dir where lexicon_measure.json goes, log is unused.
# Returns (metrics, detail); both are empty when lexicon.py or the espeak runtime is unavailable.
# Side effects: runs espeak in the runtime Python and writes OUT_DIR/lexicon_measure.json.
def lexicon_metrics(karaoke_root: Path, out_dir: Path, log=print) -> tuple[dict, dict]:
    """Phonemes the voice was given vs CMU (lexicon.py). Returns ({}, {}) when the probe is not available.

    lexicon_flagged_words = flagged words (distance, stress, dropped apostrophe) + heteronym occurrences read
    the wrong way; lexicon_mean_distance = occurrence-weighted mean distance of dictionary words."""
    try:  # the lexicon probe is optional and may be missing
        from . import lexicon  # import inside the function so a missing module simply disables it
    except ImportError:  # lexicon.py is not shipped or failed to import
        return {}, {}  # no lexicon metrics or detail can be produced then
    if not lexicon.espeak_available():  # the runtime Python or the helper script is missing
        return {}, {}  # skip the probe instead of failing the whole measurement
    res = lexicon.measure(karaoke_root)  # phonemize and compare every unique word with CMU (the slow part)
    lexicon.write_json(res, Path(out_dir) / "lexicon_measure.json")  # keep the full result beside the measurement
    # dictionary words with a distance, excluding out-of-vocabulary words and proper names
    rows = [r for r in res["rows"] if r["distance"] is not None and not r["review"]]
    n = sum(r["count"] for r in rows)  # total occurrences across those words
    # occurrence-weighted mean distance (0 = identical phonemes); None when nothing qualifies
    mean = sum(r["distance"] * r["count"] for r in rows) / n if n else None
    t = res["totals"]  # the probe's summary totals, including flagged counts
    # flagged words plus heteronym occurrences read the wrong way, one combined count
    metrics = {"lexicon_flagged_words": t["flagged"] + t["heteronym_suspects"],
               "lexicon_mean_distance": round(mean, 5) if mean is not None else None}  # rounded mean distance, or None
    # per flagged word: spelling, count, espeak phonemes, CMU phonemes and the reason
    detail = {"flagged": [{k: r.get(k) for k in ("word", "count", "espeak_en", "cmu_ipa", "reason")} for r in res["flagged"]],
              # every suspect heteronym occurrence (e.g. "read" as present where past was expected)
              "heteronym_suspects": [{"word": h["word"], "token": o["token"], "read_as": o["espeak_in_context"],
                                      # what espeak said in context and which reading was expected
                                      "expected": o["expected_role"], "sentence": o["sentence"]}
                                     # flatten heteronyms into their suspect occurrences only
                                     for h in res["heteronyms"] for o in h["occurrences"] if o["suspect"]]}
    return metrics, detail  # metrics for the rubric, detail for people


# Take one pronunciation measurement of the narration and write RUN/measure-NNN.json for loop.decide().
# Parameters: run_dir (sealed run folder), request/docmodel/speech paths, overrides_path, models_dir (ASR models),
# segments (optional id subset), use_lexicon, note (free text stored in the file), log (message callback).
# Returns the measurement path. Side effects: runs both recognizers, optionally Praat and espeak, and writes
# RUN/measure-NNN/ (reference request, ASR output, r-colour and lexicon results) plus the JSON file itself.
def measure(run_dir: Path, request_path: Path = asr.DEFAULT_REQUEST, docmodel_path: Path = asr.DEFAULT_DOCMODEL,
            # speech folder with the WAVs and timing records, and the overrides file (None to ignore overrides)
            speech_dir: Path = asr.DEFAULT_SPEECH, overrides_path: Path | None = DEFAULT_OVERRIDES,
            # recognizer model folder, optional segment subset, and whether to run the dictionary probe
            models_dir: Path = asr.DEFAULT_MODELS, segments: list | None = None, use_lexicon: bool = True,
            # free-text note stored in the measurement, and a logging function (print by default)
            note: str | None = None, log=print, watch=()) -> Path:
    # accept strings as well as Paths
    run_dir, request_path, speech_dir = Path(run_dir), Path(request_path), Path(speech_dir)
    # The seal is verified first, so a rubric cannot be written after seeing a result.
    sealed = loop.load_sealed(run_dir)                      # nothing is measured before the rubric is sealed
    # next measurement number, counted from existing measure-*.json files
    n = len(list(run_dir.glob("measure-*.json"))) + 1
    mdir = run_dir / f"measure-{n:03d}"  # folder for this measurement's working files
    (mdir / "asr").mkdir(parents=True, exist_ok=True)  # create the measurement folder together with its asr subfolder
    # transcript cache shared by every measurement in the run, so unchanged audio is not decoded twice
    shared_cache = run_dir / "asr-cache"
    shared_cache.mkdir(exist_ok=True)  # create the shared transcript cache folder if needed
    link = mdir / "asr" / "cache"  # where asr.run expects its cache inside this measurement
    if not link.exists():  # only create the cache link once per measurement folder
        # relative symlink to the shared cache (on Windows this needs Developer Mode or admin rights)
        os.symlink(os.path.relpath(shared_cache, link.parent), link)

    # the narration request: segments, voiced tokens and their ids
    request = json.loads(request_path.read_text(encoding="utf-8"))
    # document model when present; it holds each token's spoken_ref
    docmodel = json.loads(Path(docmodel_path).read_text(encoding="utf-8")) if docmodel_path and Path(docmodel_path).exists() else None
    # swap overridden tokens back to what a listener should hear (a respelling is scored as the real word)
    ref = trial.reference_request(request, docmodel)
    ref = {**request, "segments": ref["segments"]}  # keep the request's other fields and use the reference segments
    if segments:  # an optional subset of segment ids was requested
        keep = set(segments)  # segment ids to keep, as a set for quick lookups
        # drop every segment outside the requested subset
        ref["segments"] = [s for s in ref["segments"] if s["id"] in keep]
    ref_path = mdir / "request.ref.json"  # the reference request written for this measurement
    # saved so the ASR probe reads exactly what was scored
    ref_path.write_text(json.dumps(ref, ensure_ascii=False), encoding="utf-8")

    # run both recognizers, score, flag and confirm; writes asr_measure.json
    am = asr.run(ref_path, speech_dir, mdir / "asr", models_dir, docmodel_path, log=log, watch=watch)
    occ = am["occurrences"]  # token id mapped to its recognizer verdict (ok, sub or del) and the word that was heard
    tracked, total = words_tracked(ref, speech_dir, occ)  # how many spoken words are timed and scored
    t = am["totals"]["ctc"]  # totals of the fast (ctc) recognizer
    # The scored metrics; their names match the sealed pronunciation rubric.
    metrics = {
        # share of spoken words tracked (gate: must be 1.0)
        "words_tracked_ratio": round(tracked / total, 6) if total else None,
        # share of segments the recognizer scored (gate: must be 1.0)
        "asr_segments_coverage": round(t["segments_scored"] / t["segments_total"], 6) if t["segments_total"] else None,
        "asr_wer": t["wer"],  # word error rate of the fast recognizer over the whole narration
        "asr_confirmed_words": len(am["confirmed"]),  # words both recognizers mishear in most occurrences
        "asr_confirmed_occurrences": confirmed_occurrences(am),  # how many times those words occur wrongly
    }
    # informational (not in the sealed rubric, so not scored): r-colouring where the spelling has no r
    rc_detail = {}  # r-colour summary for the measurement file, filled in below when Praat is available
    # Words heard with an r their spelling does not have, from the third formant (rcolour.py).
    try:  # rcolour needs numpy and parselmouth; missing packages raise ImportError
        from . import rcolour  # imported lazily so the measurement still works without it
        rc = rcolour.run(ref, speech_dir)  # measure F3 per word occurrence
        # keep the full result in the measurement folder
        (mdir / "rcolour.json").write_text(json.dumps(rc, indent=1), encoding="utf-8")
        # reported as a metric, but the rubric does not score it
        metrics["unspelled_r_occurrences"] = rc["flagged_occurrences"]
        # summary for the measurement file: flagged words with their low counts and median F3
        rc_detail = {"flagged": [{k: r[k] for k in ("word", "low", "count", "f3_median")} for r in rc["flagged"]],
                     # plus all low dips and the number of words measured
                     "low_occurrences_all": rc["low_occurrences_all"], "words_measured": rc["words_measured"]}
    except ImportError:  # praat-parselmouth or numpy is not installed on this machine
        pass  # carry on without the r-colour data
    lex_detail = {}  # dictionary probe results, empty unless it runs
    if use_lexicon:  # the lexicon probe can be switched off with --no-lexicon
        # project root is two folders above request.json (ROOT/work/request.json)
        lm, lex_detail = lexicon_metrics(Path(request_path).parent.parent, mdir / "lexicon", log)
        metrics.update(lm)  # add lexicon_flagged_words and lexicon_mean_distance when present

    ov = None  # the overrides in force, recorded for the log
    if overrides_path and Path(overrides_path).exists():  # read the overrides only when the file actually exists
        # keep the override table itself
        ov = json.loads(Path(overrides_path).read_text(encoding="utf-8")).get("overrides", {})
    seg_ids = [s["id"] for s in ref["segments"]]  # ids of the segments measured, in order
    # The measurement record in the shape loop.decide() expects, plus details for people.
    m = {
        # schema, sealed rubric hash and time, which decide() checks first
        "schema": loop.MEASURE_SCHEMA, "rubric_sha256": sealed["sha256"], "measured_at": loop.now(),
        # what was changed (script and overrides) and what was heard (the WAVs)
        "page_sha256": page_sha256(request_path, overrides_path), "signature_sha256": signature_sha256(speech_dir, seg_ids),
        "probe_sha256": probe_sha256(), "note": note,  # probe code fingerprint and the free-text note
        # where the inputs came from and how many segments were measured
        "inputs": {"request": str(request_path), "speech_dir": str(speech_dir), "segments": len(seg_ids),
                   # the overrides and the tracked word counts
                   "overrides": ov, "words_tracked": tracked, "words_total": total},
        "metrics": metrics,  # metric values that the sealed rubric scores
        # Detail for people: where the full ASR output is, the confirmed words, the confirm model's error rate,
        # the lexicon and r-colour findings and how many words were scored.
        "detail": {"asr_measure": str(mdir / "asr" / "asr_measure.json"),
                   # one row per confirmed word: counts, both rates, what each model heard and where
                   "confirmed": [{k: e.get(k) for k in ("word", "count", "ctc_rate", "confirm_rate", "ctc_heard",
                                                        # remaining selected fields, gathered for each confirmed word
                                                        "confirm_heard", "segs")} for e in am["confirmed"]],
                   # word error rate of the confirm model on the segments it re-heard
                   "confirm_wer": am["totals"]["confirm"].get("wer"),
                   "lexicon": lex_detail,  # lexicon detail from lexicon_metrics
                   "unspelled_r": rc_detail,  # unspelled r detail from the formant probe
                   "words_scored": t["words_scored"]},  # number of expected words the fast model scored
    }
    out = run_dir / f"measure-{n:03d}.json"  # the measurement file, beside the measurement folder of the same number
    # indented JSON with a trailing newline
    out.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"wrote {out}")  # report the path of the written measurement file
    for k, v in metrics.items():  # then print every metric name with its value
        log(f"  {k:<26} {v}")  # name padded to 26 characters, then its value
    return out  # path for loop.decide or the CLI
