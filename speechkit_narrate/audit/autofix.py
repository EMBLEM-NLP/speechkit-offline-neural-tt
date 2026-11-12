# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""One automatic round of the pronunciation loop (the steps that were first done by hand, automated):

  1. baseline: measure + decide, if the project has no loop yet
  2. propose fixes from the latest measurement
       - words both recognizers mishear      -> dictionary phonemes, respellings, r-free variants
       - wrong stress / reading (dictionary) -> dictionary phonemes, respellings
       - heteronyms read the wrong way       -> dictionary phonemes for those occurrences only
       - an r that is not in the spelling    -> r-free variants (final ə -> ɐ, ɑː -> ɒ)
  3. try every candidate acoustically (trial.py: several takes in the real sentences, both recognizers,
     against fresh takes of the current reading) and keep the ones that do better (or, for stress and
     reading fixes the recognizers can't hear, that do no worse)
  4. apply them (rebuild the script, re-voice only changed segments), measure, decide; revert on a veto
  5. for words still misheard, voice their segments several more times and keep the best take;
     measure, decide; revert on a veto

Fixes are written to work/pronunciations.json, which you can edit by hand at any time.
"""
# Type hints are stored as strings and not evaluated when the module loads.
from __future__ import annotations

import json  # reads the docmodel and the overrides file, writes the trial plan and the report
import re  # regular expressions for phoneme symbols and word shapes
from pathlib import Path  # Path objects for the run, trial and report files

# package helpers: configure the modules, find the run directory, measure, decide, read results and history
from . import configure, decide, latest, measure, run_dir, status

# ARPAbet vowel (stress digit removed) -> the vowel symbol of the voice's espeak "en" (British) phoneme set.
# AA becomes ɒ as in "lot", OW becomes əʊ and ER becomes ɜː: the symbols the voice was trained on, not American IPA.
V = {"AA": "ɒ", "AE": "a", "AH": "ʌ", "AO": "ɔː", "AW": "aʊ", "AY": "aɪ", "EH": "ɛ", "ER": "ɜː", "EY": "eɪ",
     "IH": "ɪ", "IY": "iː", "OW": "əʊ", "OY": "ɔɪ", "UH": "ʊ", "UW": "uː"}
# ARPAbet consonant -> espeak "en" symbol. R maps to ɹ, but arpabet_to_voice keeps it only before a vowel; G uses the
# IPA letter ɡ (U+0261), as espeak writes it.
C = {"B": "b", "CH": "tʃ", "D": "d", "DH": "ð", "F": "f", "G": "ɡ", "HH": "h", "JH": "dʒ", "K": "k", "L": "l",
     "M": "m", "N": "n", "NG": "ŋ", "P": "p", "R": "ɹ", "S": "s", "SH": "ʃ", "T": "t", "TH": "θ", "V": "v",
     "W": "w", "Y": "j", "Z": "z", "ZH": "ʒ"}


# Convert one CMU ARPAbet pronunciation such as "B OW1 AE2 Z" into Piper raw phonemes in the voice's symbols
# ("bˈəʊˌaz").
# Stress marks (ˈ primary, ˌ secondary) go directly before their vowel. The result is wrapped in [[...]] by callers,
# which tells Piper to read phonemes instead of text. Returns a str; pure function.
def arpabet_to_voice(arpa: str) -> str:
    """CMU ARPAbet -> phonemes in the voice's own (espeak "en") symbol set, stress mark before the vowel.

    The voice was trained on British espeak symbols, so American symbols (ɚ, oʊ, ɑ) are mapped to their
    British-set equivalents; r is kept only before a vowel, as espeak "en" writes it."""
    ph = arpa.split()  # phones separated by spaces; vowels carry a stress digit 0, 1 or 2
    out = []  # output phoneme symbols collected in order
    for k, p in enumerate(ph):  # k is needed to peek at the following phone
        # split "AE2" into base "AE" and stress "2"; consonants have an empty stress
        base, st = re.fullmatch(r"([A-Z]+)([012]?)", p).groups()
        # base of the following phone without its digit, or "" at the end of the word
        nxt = re.sub(r"\d", "", ph[k + 1]) if k + 1 < len(ph) else ""
        if base in V:  # a vowel phone (it carries a stress digit)
            v = V[base]  # its default British vowel symbol from the table
            if base == "AH" and st == "0":  # unstressed AH is the reduced vowel schwa
                v = "ə"  # schwa, the reduced central vowel
            elif base == "ER" and st == "0":  # unstressed ER (as in "keeper") is a plain schwa in non-rhotic espeak
                v = "ə"  # plain schwa without any r colouring
            # word-final unstressed IY ("happy") is written as short i by espeak
            elif base == "IY" and st == "0" and k == len(ph) - 1:
                v = "i"  # short happy-tense i vowel symbol
            # AA before R ("star") is the long ɑː of British "star", not the ɒ of "lot"
            elif base == "AA" and nxt == "R":
                v = "ɑː"  # long open back vowel
            # stress mark for 1 or 2 (none for 0) followed by the vowel
            out.append({"1": "ˈ", "2": "ˌ"}.get(st, "") + v)
        elif base == "R":  # the r consonant phone, handled separately below
            if nxt in V:  # espeak "en" writes an r only when a vowel follows (non-rhotic accent)
                out.append("ɹ")  # keep it, written as the approximant symbol ɹ
        else:  # any other consonant phone, looked up directly
            out.append(C[base])  # its symbol from the table (KeyError for an unknown phone)
    return "".join(out)  # one string without separators, as Piper expects


# Variants of a phoneme string without the vowels this American-trained voice tends to colour with an r.
# A final ə becomes ɐ ("Ana" heard as "Arna") and ɑː not followed by ɹ becomes ɒ ("drama" heard as "drummer").
# Returns up to three distinct variants (each change alone, then both), never the input itself.
def r_variants(ph: str) -> list[str]:
    """Variants of the voice's phonemes without the vowels it tends to r-colour."""
    outs = []  # distinct variant strings, kept in creation order
    a = re.sub(r"ə$", "ɐ", ph)  # final schwa replaced by near-open ɐ, which the voice does not r-colour
    b = re.sub(r"ɑː(?!ɹ)", "ɒ", ph)  # every long ɑː that is not before an r becomes the short ɒ
    for v in (a, b, re.sub(r"ɑː(?!ɹ)", "ɒ", a)):  # each change alone, then both changes together
        if v != ph and v not in outs:  # skip variants identical to the input or already listed
            outs.append(v)  # keep this distinct variant for the acoustic trial
    return outs  # list of str, possibly empty


# Map from a word as the recognizers' scoring normalizes it (lowercase letters, no apostrophes) to the spelling used as
# a key in pronunciations.json "overrides" (lowercase with a straight apostrophe, for example "i'll").
# project supplies the docmodel path; mods are the configured audit modules (for trial.word_key). Only reads files.
def _spelled(project, mods) -> dict:
    """normalized word (as the recognizers' scoring writes it) -> spelled override key (lowercase, apostrophes)."""
    dm = json.loads(project.p("docmodel").read_text(encoding="utf-8"))  # parsed book with every token and block
    out = {}  # normalized key -> override spelling
    for t in dm["tokens"]:  # every word token in the book, in order
        # reference spoken form (before overrides), else the current spoken form
        ref = t.get("spoken_ref") or t.get("spoken") or ""
        # a single word of letters, apostrophes and hyphens, optionally followed by punctuation
        m = re.fullmatch(r"([A-Za-zÀ-ÿ'’-]+)[.,;:!?]*", ref)
        if m:  # only plain words can be overridden; numbers and symbols are skipped
            # first spelling seen wins; curly apostrophes are straightened as in the docmodel overrides
            out.setdefault(mods["trial"].word_key(m.group(1)), m.group(1).lower().replace("’", "'"))
    return out  # normalized word -> override key


# Propose candidate fixes from the latest measurement. project is the Project, lat the dict returned by latest(), mods
# the configured modules. Returns (words, tokens): words maps a normalized word to {"spelled", "kind", "why",
# "variants"} with kind "heard", "dictionary" or "r"; tokens maps a token id (str) to one heteronym fix.
# Calls espeak through the candidates module but writes nothing.
def proposals(project, lat: dict, mods) -> tuple[dict, dict]:
    from emblem_rsi.pron import candidates as cand  # respelling generator and scorer (espeak based)
    from emblem_rsi.pron import lexicon as lx  # dictionary probe helpers, here verb_reading for heteronyms
    spelled = _spelled(project, mods)  # normalized word -> override spelling
    # dictionary probe output, or an empty structure when the probe did not run
    lex = lat.get("lexicon") or {"rows": [], "flagged": [], "heteronyms": []}
    rows = {r["word"]: r for r in lex["rows"]}  # lexicon rows by word: espeak phonemes, CMU variants, distances
    words: dict = {}  # proposals so far, keyed by normalized word

    # Record a proposal. key is the normalized word, kind the problem type, why a reason text and variants the candidate
    # spellings or [[phoneme]] strings. Several probes may propose fixes for one word; reasons and variants are merged.
    def add(key, kind, why, variants):
        sp = spelled.get(key) or key  # spelling to use in pronunciations.json, defaulting to the key itself
        # create the entry on first sight; the first kind seen is kept
        e = words.setdefault(key, {"spelled": sp, "kind": kind, "why": [], "variants": []})
        if kind == "heard":  # a word both recognizers mishear is the strongest evidence
            e["kind"] = "heard"  # so "heard" replaces a dictionary or r kind
        e["why"].append(why)  # collect every reason text for the report
        for v in variants:  # add each candidate variant once, keeping order
            if v and v not in e["variants"]:  # skip empty values and duplicates
                e["variants"].append(v)  # keep the probe's order, best candidates first

    # Up to two CMU dictionary pronunciations of a word as [[phoneme]] overrides in the voice's own symbols. word is a
    # normalized or lexicon spelling; returns a list of str.
    def cmu_raw(word):
        # lexicon row for the word, also tried without apostrophes
        r = rows.get(word) or rows.get(word.replace("'", ""))
        # first two CMU variants, converted and wrapped for Piper; [] when the word is not in the dictionary
        return [f"[[{arpabet_to_voice(a)}]]" for a in (r or {}).get("cmu_arpabet", [])[:2]]

    # The phonemes espeak currently gives the voice for this word, taken from the lexicon probe ("" when unknown). word
    # is looked up as given and without apostrophes; returns a str.
    def espeak_now(word):
        r = rows.get(word) or rows.get(word.replace("'", ""))  # same lookup, with or without apostrophes in the key
        return (r or {}).get("espeak_en", "")  # espeak "en" phoneme string, or empty

    # words both recognizers mishear
    # each confirmed word: both recognizers got it wrong in most of its occurrences
    for e in (lat.get("asr") or {}).get("confirmed", []):
        w = e["word"]  # normalized word as the recognizers score it
        sp = spelled.get(w, w)  # its override spelling, or the word itself
        vs = cmu_raw(sp)  # start with the CMU dictionary phonemes
        try:  # respellings are an optional extra on top of phonemes
            # propose spellings and rank them by espeak distance to CMU, only when the lexicon has a row for the word
            ranked = cand.score(sp, cand.propose(sp, rows.get(sp)), rows.get(sp)) if rows.get(sp) else []
            vs += [x["respell"] for x in ranked[:2]]  # the two best-ranked respellings by espeak distance
        except Exception:  # noqa: BLE001 - candidates are optional
            pass  # candidates needs espeak; without it the word still gets the dictionary phonemes
        cur = espeak_now(sp)  # current espeak phonemes of the word
        if cur and "r" not in sp:  # the spelling has no r, so an r heard in the voice would be spurious
            vs += [f"[[{v}]]" for v in r_variants(cur)]  # add the r-free variants as raw phonemes
        # at most five candidates; the reason names up to two things the first recognizer heard instead
        add(w, "heard", f"misheard as {', '.join(list(e.get('ctc_heard', {}))[:2])}", vs[:5])
    # dictionary: wrong stress or reading
    # flagged dictionary rows: distance or stress mismatch against CMU, plus dropped apostrophes
    for r in lex.get("flagged", []):
        # apostrophe drops are fixed in the text; review rows (names, unknown words) are left to a person
        if r.get("kind") == "display_mismatch" or r.get("review"):
            continue  # leave those rows out of the automatic fixes
        sp = r["word"]  # the word exactly as the lexicon spells it
        key = mods["trial"].word_key(sp)  # normalized key used by the trial and the scoring
        vs = cmu_raw(sp)  # dictionary phonemes come first in the candidate list
        try:  # a respelling is optional here as well (needs espeak)
            # best respelling, accepting candidates that only correct the stress
            best = cand.best_fixes([r], allow_stress_fix=True).get(sp)
            if best:  # a better respelling was found by the scorer
                vs.append(best["respell"])  # add it after the phoneme variants
        except Exception:  # noqa: BLE001
            pass  # espeak unavailable: keep the phoneme variants only
        # at most three candidates, with the lexicon's reason (distance or stress)
        add(key, "dictionary", r.get("reason") or "dictionary", vs[:3])
    # an r that is not in the spelling
    # words whose third formant dips although their spelling has no r
    for r in (lat.get("rcolour") or {}).get("flagged", []):
        # require two or more low-F3 occurrences in at least 60% of the occurrences, to ignore one-off noise
        if r["low"] < 2 or r["rate"] < 0.6:
            continue  # evidence too weak to act on; skip the word
        sp = spelled.get(r["word"], r["word"])  # override spelling for the r-coloured word
        cur = espeak_now(sp)  # current espeak phonemes of that spelling
        if cur:  # only words espeak can phonemize get r-free variants
            # reason: how many occurrences dipped and the median F3 in Hz
            add(mods["trial"].word_key(sp), "r", f"r-coloured in {r['low']}/{r['count']} (F3 {r['f3_median']:.0f} Hz)",
                [f"[[{v}]]" for v in r_variants(cur)])  # the r-free variants as raw phonemes
    # heteronyms read the wrong way: per occurrence
    tokens = {}  # token id (as str) -> fix for one heteronym occurrence
    for h in lex.get("heteronyms", []):  # each heteronym found by the dictionary probe
        for o in h["occurrences"]:  # each occurrence record: token, roles and sentence
            if not o.get("suspect"):  # only occurrences read the wrong way for their sentence
                continue  # correctly read ones are left alone
            # CMU variants whose reading (verb, or noun and adjective) matches the role the sentence wants
            want = [a for a in h["cmu_arpabet"] if (lx.verb_reading(h["word"], a) is True) == (o["expected_role"] == "verb")]
            if want:  # at least one CMU variant has the wanted reading
                # override for this token only, using the first matching variant
                tokens[str(o["token"])] = {"word": h["word"], "variant": f"[[{arpabet_to_voice(want[0])}]]",
                                           # why it was proposed: read as one role, the sentence wants the other
                                           "why": f"read as {o['reading_role']}, sentence wants {o['expected_role']}",
                                           "sentence": o["sentence"]}  # the sentence, for the report and manual review
    words = {k: v for k, v in words.items() if v["variants"]}  # drop words for which no candidate could be produced
    return words, tokens  # (word proposals, token proposals)


# Pick the winning variant per word from the acoustic trial summary (the output of trial.summarize).
# words is the first result of proposals(); summary maps word -> variant -> rates, plus a "_best" entry.
# Returns {word: {"spelled", "variant", "why", "ok_before", "ok_after"}} for the words that got a fix.
def choose(words: dict, summary: dict) -> dict:
    """Pick a winning variant per word from the trial summary."""
    from emblem_rsi.pron.trial import CURRENT  # label of the control: fresh takes of the current reading
    out = {}  # accepted fixes keyed by normalized word
    for key, w in words.items():  # each proposed word with its candidate variants
        s = summary.get(key)  # trial results for this word, keyed by variant
        if not s:  # not tried, for example because no occurrence was found
            continue  # nothing to choose from; skip the word
        base = s.get(CURRENT, {})  # results of the current reading (the control)
        best = s.get("_best", {})  # best variant and the trial's verdict for it
        if w["kind"] in ("heard", "r"):  # misheard words and r-colour words are judged by the recognizers
            # clear gain: recognition 25 points better with little collateral damage (trial.summarize rules)
            if best.get("verdict") == "adopt":
                # adopt the best variant, recording recognition rates before and after
                out[key] = {"spelled": w["spelled"], "variant": best["variant"], "why": w["why"],
                            # ok_after is the adopted variant's recognition rate
                            "ok_before": base.get("ok_rate"), "ok_after": s[best["variant"]]["ok_rate"]}
            elif w["kind"] == "r":  # no clear recognition gain, but this is an r-colour fix
                # r-colour fixes: adopt when recognition does not drop and the r is gone (F3 back up)
                for v in w["variants"]:  # try the r-free variants in proposal order
                    r = s.get(v)  # trial results for this r-free variant
                    # Recognition may drop by at most 5 points, the variant's median F3 must exceed 2300 Hz (no r) and
                    # the current reading's median F3 must be under 2200 Hz (it really had an r); a missing F3 value
                    # fails these tests.
                    if r and r["ok_rate"] >= (base.get("ok_rate") or 0) - 0.05 and (r.get("f3_median") or 0) > 2300 \
                            and (base.get("f3_median") or 9e9) < 2200:  # both tests: the r was present, now it is gone
                        # accept this r-free variant as the fix
                        out[key] = {"spelled": w["spelled"], "variant": v, "why": w["why"],
                                    # recognition rates before and after, for the report
                                    "ok_before": base.get("ok_rate"), "ok_after": r["ok_rate"]}
                        break  # the first acceptable variant wins
        # Dictionary (stress or reading) fixes: the recognizers barely hear stress, so "no worse" is enough.
        else:   # dictionary fixes: the recognizers forgive stress, so require "no worse"
            for v in w["variants"]:  # variants in proposal order, CMU phonemes first
                r = s.get(v)  # trial results for this dictionary variant
                # accept when recognition drops by no more than 10 points
                if r and r["ok_rate"] >= (base.get("ok_rate") or 0) - 0.1:
                    # record the chosen dictionary variant
                    out[key] = {"spelled": w["spelled"], "variant": v, "why": w["why"],
                                # recognition rates before and after the change
                                "ok_before": base.get("ok_rate"), "ok_after": r["ok_rate"]}
                    break  # stop at the first acceptable variant
    return out  # word -> chosen fix dictionary for autofix()


# The folder for a trial of `plan` in the run directory `d`: the newest trial-NN folder when its plan.json holds the
# same plan (a trial that was stopped part way, by a restart or by running out of memory: trial.synthesize skips the
# takes already on disk, so it carries on where it stopped), otherwise the next number. Returns the Path; creates
# nothing.
def trial_dir(d: Path, plan: dict) -> Path:
    prev = sorted(d.glob("trial-*"))  # earlier trial folders, oldest first (the numbers are zero-padded)
    if prev and (prev[-1] / "plan.json").exists():  # the newest trial wrote its plan
        if json.loads((prev[-1] / "plan.json").read_text(encoding="utf-8")) == plan:  # and it is this very plan
            return prev[-1]  # resume it
    return d / f"trial-{len(prev) + 1:02d}"  # a fresh folder: trial-01, trial-02 ...


# Frees the speech recognizers that a measurement or a trial loaded into this process (the asr module keeps them for
# reuse). The two of them take more than a gigabyte, and the voice workers started next take about as much each: on a
# machine limited to 6 GB the loop was stopped by the system half way through a 160-minute book's trial. `mods` is
# configure()'s module dict. The next measurement loads them again, which takes a few seconds.
def release(mods) -> None:
    import gc  # the garbage collector, so the recognizers' memory is returned now rather than at some later point
    mods["asr"]._RECOGNIZERS.clear()  # drop the cached recognizers; nothing else holds them
    gc.collect()  # free them, and the native memory of their inference sessions with them


# One automatic round of the pronunciation loop for a project (the module docstring lists the steps).
# takes: trial takes per candidate and occurrence; retake_takes: extra takes per segment for words still misheard; log
# receives progress lines. Returns a report dict, also saved as work/rsi/pron/autofix-last.json.
# Side effects: runs the voice and both recognizers, edits work/pronunciations.json, rebuilds the script and re-voices
# segments; whatever a veto rejects is restored from the snapshots.
def autofix(project, takes: int = 3, retake_takes: int = 4, log=print) -> dict:
    if isinstance(takes, bool) or not isinstance(takes, int) or takes < 1:
        raise ValueError("takes must be an integer >= 1")
    if isinstance(retake_takes, bool) or not isinstance(retake_takes, int) or retake_takes < 0:
        raise ValueError("retake_takes must be an integer >= 0")
    limits = project.cfg.get("audit") or {}
    maximum_words = limits.get("max_fix_words")
    maximum_occurrences = limits.get("max_occurrences_per_word", 4)
    if maximum_words is not None and (isinstance(maximum_words, bool)
            or not isinstance(maximum_words, int) or maximum_words < 1):
        raise ValueError("audit.max_fix_words must be null or an integer >= 1")
    if isinstance(maximum_occurrences, bool) or not isinstance(maximum_occurrences, int) or maximum_occurrences < 1:
        raise ValueError("audit.max_occurrences_per_word must be an integer >= 1")
    mods = configure(project)  # audit modules configured for this project's paths
    d = run_dir(project)  # run directory work/rsi/pron, sealed on first use
    report = {"loops": []}  # loop entries decided in this round
    if not status(project):  # no history yet: the project was never measured
        measure(project, "baseline", log=log)  # baseline measurement of the narration as it stands
        # its decision; the first measurement that passes the gates is promoted
        report["loops"].append(decide(project, "baseline"))
    lat = latest(project)  # newest measurement and probe outputs
    words, tokens = proposals(project, lat, mods)  # candidate fixes per word and per heteronym occurrence
    deferred = {}
    if maximum_words is not None and len(words) > maximum_words:
        selected = list(words)[:maximum_words]
        deferred = {k: v for k, v in words.items() if k not in selected}
        words = {k: words[k] for k in selected}
    report["budget"] = {"takes": takes, "retakes": retake_takes, "max_fix_words": maximum_words,
                        "max_occurrences_per_word": maximum_occurrences}
    report["deferred_words"] = deferred
    # log how many words and occurrences will be tried
    log(f"autofix: {len(words)} words and {len(tokens)} single occurrences to try")
    chosen = {}  # accepted word fixes, empty until a trial runs
    if words:  # there are word candidates to try acoustically
        # trial plan: at most four occurrences per word, and the candidate variants of each word
        plan = {"max_occurrences": maximum_occurrences, "words": {k: w["variants"] for k, w in words.items()}}
        tdir = trial_dir(d, plan)  # a new trial-NN folder, or the newest one when it was interrupted on the same plan
        tdir.mkdir(parents=True, exist_ok=True)  # create the trial folder and its parents
        # keep the plan beside the results for later inspection
        (tdir / "plan.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
        release(mods)  # the measurement's recognizers make room for the trial's voice workers
        # voice each candidate (and the current reading) in its real sentences and score both recognizers
        res = mods["trial"].run(plan, tdir, takes, workers=max(1, int(project.cfg["voice"].get("workers", 2))),
                                request_path=project.p("request"),
                                # the project's script, docmodel and narration folder
                                docmodel_path=project.p("docmodel"), speech_dir=project.p("speech"), log=log)
        chosen = choose(words, res["summary"])  # pick the winning variant for each word
    ov_path = project.p("pronunciations")  # path of work/pronunciations.json in the project
    # current overrides, or a new empty structure
    ov = json.loads(ov_path.read_text(encoding="utf-8")) if ov_path.exists() else {}
    ov.setdefault("overrides", {})  # word overrides: spelling -> respelling or [[phonemes]]
    ov.setdefault("tokens", {})  # token overrides: token id -> respelling, for single occurrences
    for key, c in chosen.items():  # each accepted word fix from the trial
        ov["overrides"][c["spelled"]] = c["variant"]  # overwrite any earlier override for that spelling
    for tid, t in tokens.items():  # each heteronym occurrence fix (applied without an acoustic trial)
        ov["tokens"].setdefault(tid, t["variant"])  # setdefault keeps a fix someone already wrote for that token
    # record everything proposed and chosen
    report.update({"proposed_words": words, "proposed_tokens": tokens, "chosen": chosen})
    if chosen or tokens:  # something to apply: word fixes or token fixes
        release(mods)  # the trial's recognizers make room for the narration workers that re-voice segments
        # snapshot, write the overrides, rebuild the script, re-voice changed segments
        mods["cli"].apply(d, ov, project.root, log=log)
        measure(project, "autofix: pronunciation fixes", log=log)  # measure the narration with the fixes in place
        # promote or veto, naming how many fixes went in
        e = decide(project, f"autofix: {len(chosen)} word fixes, {len(tokens)} single-occurrence fixes")
        report["loops"].append(e)  # keep the decision entry in the report
        if e["decision"] == "veto":  # the fixes made things worse, or changed nothing
            mods["cli"].revert(d, project.root, log=log)  # restore overrides, script and audio from the snapshot
            log("autofix: fixes vetoed and reverted: " + "; ".join(e["reasons"]))  # log the veto reasons for the user
    # retakes for words still misheard
    lat = latest(project)  # newest measurement; after a veto above this is still the vetoed one
    conf = (lat.get("asr") or {}).get("confirmed", [])  # words both recognizers still mishear
    if conf and retake_takes > 0:  # zero explicitly disables the expensive retake branch
        # every segment containing one of them, sorted and without repeats
        segs = sorted({s for e in conf for s in e["segs"]})
        release(mods)  # the last measurement's recognizers make room for the retake voices
        # voice those segments retake_takes more times and keep the take both recognizers hear best
        mods["cli"].retake(d, segs, [e["word"] for e in conf], retake_takes, project.root, log=log)
        measure(project, "autofix: best-of takes", log=log)  # measure with the best takes in place
        # retake_takes new takes plus the current one
        e = decide(project, f"best of {retake_takes + 1} takes for {len(segs)} segments")
        report["loops"].append(e)  # record the retake decision in the report
        if e["decision"] == "veto":  # the retakes made things worse
            mods["cli"].revert(d, project.root, log=log)  # restore the replaced WAVs and timing records
    # save the report; default=str turns Path values into text
    (d / "autofix-last.json").write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return report  # the report, returned to the autofix stage
