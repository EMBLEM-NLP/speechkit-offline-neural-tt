# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Fix candidates: text respellings that steer espeak (voice "en", as narrated) toward the CMU pronunciation.

Purely textual/phonemic: every candidate is phonemized with the same espeak helper and scored with the
same coarse distance to CMU as the lexicon probe. The acoustic re-check belongs to the ASR probe.

    python3 -I emblem_rsi/pron/candidates.py [--lexicon runs/pron-probe/lexicon/lexicon_measure.json]
"""
# keeps annotations such as dict | None as plain strings, so older Pythons accept them
from __future__ import annotations

import argparse  # command-line parsing for the standalone script
import json  # reads lexicon_measure.json and writes fix_candidates.json
import re  # regular expressions, used to restore a capital I in contractions
import sys  # sys.path tweak for script mode and the exit code
from pathlib import Path  # filesystem paths that work on Windows and Linux alike

# Run as a plain script (python3 -I candidates.py) there is no parent package, so relative imports fail.
# In that case the repository root is put on sys.path and the package is imported by its full name.
if __package__ in (None, ""):
    # parents[2] of emblem_rsi/pron/candidates.py is the repository root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from emblem_rsi.pron import lexicon as lx  # noqa: E402
else:  # imported as part of the package (python -m, or from speechkit_narrate/audit/autofix.py)
    from . import lexicon as lx  # the lexicon probe supplies espeak access, CMU lookups and the distance measure

SCHEMA = "emblem-rsi/pron-fix-candidates/1"  # schema tag written into the output JSON so readers can check its format
# Tie-break order when two candidates are equally close to CMU: restoring the display form is the most
# natural change, then splitting into two real words, then a hyphen, and a full phonetic respelling last.
KIND_RANK = {"display": 0, "compound": 1, "hyphenate": 2, "respell": 3}

# ---------------------------------------------------------------- ARPAbet -> English-ish spelling

# Two-consonant clusters English allows at the start of a syllable (ARPAbet symbols), e.g. "pr" in "pray".
# syllabify() uses them to decide where a consonant cluster between two vowels is cut.
_ONSETS2 = {("P", "R"), ("P", "L"), ("B", "R"), ("B", "L"), ("T", "R"), ("D", "R"), ("K", "R"), ("K", "L"),
            # g and f followed by r or l, th and sh followed by r (thr, shr), then s-clusters
            ("G", "R"), ("G", "L"), ("F", "R"), ("F", "L"), ("TH", "R"), ("SH", "R"), ("S", "P"), ("S", "T"),
            # more s-clusters and the consonant + w onsets (tw, dw, kw)
            ("S", "K"), ("S", "M"), ("S", "N"), ("S", "L"), ("S", "W"), ("T", "W"), ("D", "W"), ("K", "W"),
            # gw and the consonant + y onsets heard in words like "pure", "cube" and "huge"
            ("G", "W"), ("P", "Y"), ("B", "Y"), ("K", "Y"), ("F", "Y"), ("M", "Y"), ("V", "Y"), ("HH", "Y")}
# legal three-consonant onsets, all starting with s (spr, str, skr, spl, skw, sky)
_ONSETS3 = {("S", "P", "R"), ("S", "T", "R"), ("S", "K", "R"), ("S", "P", "L"), ("S", "K", "W"), ("S", "K", "Y")}
# Plain English letters for each ARPAbet consonant. DH and TH both become "th" because spelling cannot
# tell the voiced and voiceless sounds apart.
_CONS = {"B": "b", "CH": "ch", "D": "d", "DH": "th", "F": "f", "G": "g", "HH": "h", "JH": "j", "K": "k",
         "L": "l", "M": "m", "N": "n", "NG": "ng", "P": "p", "R": "r", "S": "s", "SH": "sh", "T": "t",
         "TH": "th", "V": "v", "W": "w", "Y": "y", "Z": "z", "ZH": "zh"}
# vowel -> (open syllable, closed syllable) per style
# Vowel spellings per style. "simple" uses the patterns readers expect (o in a closed syllable, igh for
# the long i before a consonant); "explicit" uses unambiguous digraphs such as "ih", "uh" and "eye".
_VOW = {
    # simple: AA is "ah" when open, "o" when a consonant closes the syllable; stressed AH is "uh"/"u"
    "simple": {"AA": ("ah", "o"), "AE": ("a", "a"), "AH1": ("uh", "u"), "AH0": ("a", "a"), "AO": ("aw", "aw"),
               # simple: AY is "y" open and "igh" closed (as in "light"); EH is "eh"/"e"
               "AW": ("ow", "ow"), "AY": ("y", "igh"), "EH": ("eh", "e"), "ER": ("er", "er"), "EY": ("ay", "ay"),
               # simple: short and long i, the long o, and both u sounds written "oo"
               "IH": ("i", "i"), "IY": ("ee", "ee"), "OW": ("oh", "oh"), "OY": ("oy", "oy"), "UH": ("oo", "oo"),
               "UW": ("oo", "oo")},  # simple: UW is "oo" in both positions
    # explicit: the same vowel never changes spelling between open and closed syllables
    "explicit": {"AA": ("ah", "ah"), "AE": ("a", "a"), "AH1": ("uh", "uh"), "AH0": ("uh", "uh"), "AO": ("aw", "aw"),
                 # explicit: AY is always "eye", ER is "ur"
                 "AW": ("ow", "ow"), "AY": ("eye", "eye"), "EH": ("eh", "eh"), "ER": ("ur", "ur"), "EY": ("ay", "ay"),
                 # explicit: IH is "ih" to keep espeak from reading a long i; UH is a bare "u"
                 "IH": ("ih", "ih"), "IY": ("ee", "ee"), "OW": ("oh", "oh"), "OY": ("oy", "oy"), "UH": ("u", "u"),
                 "UW": ("oo", "oo")},  # explicit: UW stays "oo"
}
# Vowel + R pairs that English spells as one unit ("car", "for", "air", "deer", "poor", "fire"); the
# speller emits these and skips the R so it is not written twice.
_R_COLOR = {"AA": "ar", "AO": "or", "EH": "air", "IH": "eer", "IY": "eer", "UH": "oor", "UW": "oor", "AY": "ire"}


# Splits one ARPAbet pronunciation into syllables using the maximal-onset rule: each consonant cluster
# between two vowels gives the following syllable the longest legal onset. phones is a list of ARPAbet
# symbols where vowels end in a stress digit (e.g. ["P", "AA1", "S", "CH", "ER0"]). Returns a list of
# syllables, each a list of symbols. Used by respellings() to build hyphenated candidates.
def syllabify(phones: list[str]) -> list[list[str]]:
    """Maximal-onset syllabification of an ARPAbet pronunciation."""
    # positions of the vowels (CMU vowels always carry a 0, 1 or 2 stress digit)
    vi = [k for k, p in enumerate(phones) if p[-1].isdigit()]
    if not vi:  # no vowel at all, for example a bare consonant
        return [phones]  # treat the whole pronunciation as one syllable
    cuts = []  # phone indexes where a new syllable begins
    for a, b in zip(vi, vi[1:]):  # walk each pair of neighbouring vowels
        cl = phones[a + 1:b]  # the consonants sitting between those two vowels
        n = 0  # how many of them belong to the next syllable's onset
        for size in (3, 2, 1):  # try the longest possible onset first
            if len(cl) >= size:  # only sizes that fit in the cluster
                tail = tuple(cl[-size:])  # the last `size` consonants of the cluster
                # A three- or two-consonant tail must be a listed onset; any single consonant may begin a syllable
                # except NG, which never starts an English syllable.
                if (size == 3 and tail in _ONSETS3) or (size == 2 and tail in _ONSETS2) or \
                        (size == 1 and tail[0] != "NG"):  # NG stays in the coda, e.g. "sing-er" not "si-nger"
                    n = size  # the onset size found
                    break  # stop at the first (longest) legal onset
        # cut just before the onset; with no consonants between the vowels the cut falls on the second vowel
        cuts.append(b - n)
    sylls, start = [], 0  # output syllables and the index where the current one starts
    for c in cuts:  # each cut ends one syllable
        sylls.append(phones[start:c]); start = c  # slice off the syllable and move the start forward
    sylls.append(phones[start:])  # the rest after the last cut is the final syllable
    return sylls  # list of syllables in order


# Writes one syllable as English-looking letters for espeak to read. syl is the syllable's ARPAbet
# symbols, style is "simple" or "explicit" (a key of _VOW). Returns the spelled syllable string.
# Pure function; respellings() calls it once per syllable and per style.
def _spell_syllable(syl: list[str], style: str) -> str:
    out, k = [], 0  # letter chunks produced so far, and the index of the symbol being spelled
    # index of the syllable's vowel; 0 means the syllable starts with its vowel
    vk = next((i for i, p in enumerate(syl) if p[-1].isdigit()), None)
    while k < len(syl):  # walk the symbols; k may jump by two when a vowel + R pair is spelled as one unit
        p = syl[k]  # the current ARPAbet symbol
        if p[-1].isdigit():  # a trailing stress digit marks a vowel
            base, st = p[:-1], p[-1]  # vowel name without the digit, and the digit itself as a string
            # the following symbol, if any, to detect an r-coloured vowel
            nxt = syl[k + 1] if k + 1 < len(syl) else None
            if nxt == "R" and base in _R_COLOR:  # vowel followed by R with a known combined spelling
                out.append(_R_COLOR[base]); k += 2  # write the r-coloured spelling and skip the R as well
                continue  # the R is consumed, so go straight to the next symbol
            # closed syllable: at least one consonant follows the vowel inside this syllable
            closed = any(not q[-1].isdigit() for q in syl[k + 1:])
            # AH is split by stress (AH1 strut vowel vs AH0 schwa); other vowels use their bare name
            key = base + ("1" if st != "0" else "0") if base == "AH" else base
            if key not in _VOW[style]:  # a vowel missing from the table (none expected)
                key = "AH1"  # falls back to the stressed AH spelling
            v = _VOW[style][key][1 if closed else 0]  # pick the closed-syllable or open-syllable spelling
            if base == "AY" and vk == 0 and not closed:  # AY alone at the start of an open syllable
                v = "eye"  # "eye" is read as the long i more reliably than a bare "y"
            out.append(v)  # add the vowel letters
        else:  # otherwise the symbol is a consonant
            c = _CONS[p]  # the plain letters for this consonant, e.g. JH becomes j and NG becomes ng
            # A g before a vowel spelled with e, i or y would be read soft (as in "gem"), so a hard g is written "gh".
            # The check uses the simple-style open spelling of the next symbol's vowel name whatever the style.
            if c == "g" and k + 1 < len(syl) and _VOW["simple"].get(syl[k + 1][:-1], ("",))[0][:1] in ("e", "i", "y"):
                c = "gh"  # "gh" keeps the g hard, as in "ghetto"
            # "ch" right after a single short vowel letter
            if c == "ch" and out and out[-1] in ("a", "e", "i", "o", "u"):
                c = "tch"  # nitch, not nich
            out.append(c)  # add the consonant letters
        k += 1  # move to the following symbol (a vowel + R pair has already jumped by two)
    return "".join(out)  # the syllable as one string


# Generates spelled alternatives for one CMU pronunciation. arpabet is a space-separated ARPAbet string
# such as "B OW1 AE2 Z". Returns a de-duplicated list of strings: for each style the joined spelling, a
# "sstch" variant when needed, the syllables joined by hyphens, and the syllables grouped by stress.
# propose() and heteronym_fixes() feed these to espeak to see which one comes out closest to CMU.
def respellings(arpabet: str) -> list[str]:
    """Phonetic respellings of one CMU variant: joined, hyphenated by syllable, grouped by stressed syllable."""
    phones = arpabet.split()  # ARPAbet string to a list of symbols
    sylls = syllabify(phones)  # syllables under the maximal-onset rule
    # stress digit of each syllable's vowel ("0" for a syllable without one)
    stress = [next((p[-1] for p in s if p[-1].isdigit()), "0") for s in sylls]
    out = []  # candidate strings in the order produced
    for style in ("simple", "explicit"):  # build candidates once with familiar spellings and once with unambiguous ones
        # each syllable spelled in this style, kept apart for the hyphenated forms
        sp = [_spell_syllable(s, style) for s in sylls]
        joined = "".join(sp)  # all syllables run together as one word
        out.append(joined)  # keep the plain joined form
        if "sch" in joined:  # posture: "posscher" reads ʃ; "posstchur" keeps s + tʃ
            # replace "sch" with "sstch" so espeak keeps the s and the tch sound separate
            out.append(joined.replace("sch", "sstch"))
        if len(sp) > 1:  # hyphenated forms only make sense with two or more syllables
            out.append("-".join(sp))  # one hyphen between every syllable, e.g. "bo-az"
            groups = []  # groups of syllables, each holding at most one stressed syllable
            for s, st in zip(sp, stress):  # walk syllables with their stress digit
                # a stressed syllable when the current group already has a stressed one
                if st in "12" and groups and any(x in "12" for x in groups[-1][1]):
                    groups.append(([s], [st]))  # opens a new group, so a hyphen lands before it
                elif not groups:  # the very first syllable
                    groups.append(([s], [st]))  # always opens the first group
                else:  # unstressed syllables, and a first stressed one in a group of unstressed ones
                    groups[-1][0].append(s); groups[-1][1].append(st)  # join the current group
            # hyphens only between stress groups, e.g. "alter-nation"
            out.append("-".join("".join(g[0]) for g in groups))
    return list(dict.fromkeys(out))  # dict.fromkeys drops duplicates and keeps the first-seen order


# ---------------------------------------------------------------- propose / score

# Lists every candidate text for one word: a restored apostrophe, a split into two dictionary words, a
# hyphen at every position for long words, and phonetic respellings of each CMU variant. word is the
# word as it appears in the lexicon rows (lowercase, may contain an apostrophe); row is that word's lexicon
# row (cmu_arpabet, spoken_as) or None. Returns [{"text", "kind"}] with kind a key of KIND_RANK.
def propose(word: str, row: dict | None = None) -> list[dict]:
    """Candidate spellings {text, kind} for a word; row (a lexicon row) supplies CMU variants and display forms."""
    row = row or {}  # with no row, every lookup below simply returns nothing
    w = word.lower()  # the word in lowercase
    letters = w.replace("'", "")  # its letters only, which is how the spoken text spelled it
    out = []  # candidates collected in order

    # Adds one candidate unless it is empty, identical to what the narration already says, or a duplicate.
    # text is the candidate string and kind its category.
    def add(text, kind):
        # skip blanks, the current spoken form, and texts already in the list
        if text and text.lower() != (row.get("spoken_as") or letters) and text not in {c["text"] for c in out}:
            out.append({"text": text, "kind": kind})  # keep the candidate with its kind

    if row.get("spoken_as") or "'" in w:  # the spoken text dropped an apostrophe: restore it
        # offer the apostrophe form; a leading "i'" becomes "I'" so espeak reads the pronoun
        add(re.sub(r"^i'", "I'", w), "display")
    d = lx.cmu()  # the full CMU pronouncing dictionary (cached)
    for i in range(2, len(letters) - 1):  # every split that leaves at least two letters on each side
        a, b = letters[:i], letters[i:]  # the two halves of the word
        # both halves are dictionary words (and the second has two or more letters)
        if a in d and b in d and len(b) >= 2:
            add(f"{a} {b}", "compound")  # as two separate words, e.g. "door keeper"
            add(f"{a}-{b}", "compound")  # and as a hyphenated compound
    if len(letters) >= 5:  # blind hyphenation is only tried for words of five letters or more
        for i in range(2, len(letters) - 1):  # every split point that keeps two letters on each side
            # hyphen at that point, which changes how espeak divides and stresses the word
            add(f"{letters[:i]}-{letters[i:]}", "hyphenate")
    # CMU variants from the row, or looked up directly when no row was given
    arpas = row.get("cmu_arpabet") or [" ".join(v) for v in lx.cmu_lookup(w)[1]]
    for a in arpas:  # every CMU pronunciation of the word
        for r in respellings(a):  # each spelled form of that variant
            add(r, "respell")  # add it as a phonetic respelling
    return out  # list of {text, kind} dictionaries


# Returns the CMU pronunciations to aim for, as IPA strings. word is the word to look up and row is an
# optional lexicon row whose cmu_ipa list is used as-is when present. An empty list means no target.
def _targets(word, row):
    if row and row.get("cmu_ipa"):  # the lexicon row already carries IPA for every variant
        return row["cmu_ipa"]  # IPA already stored by the lexicon probe, so no conversion is needed
    # otherwise convert each dictionary variant from ARPAbet to IPA
    return [lx.arpabet_to_ipa(v) for v in lx.cmu_lookup(word)[1]]


# Phonemizes each candidate with espeak voice "en" and keeps only those that bring the word closer to
# CMU. word is the word, candidates a list of {text, kind} dicts or plain strings, row its lexicon row,
# allow_stress_fix also accepts equal-distance candidates that repair stress, targets overrides the CMU
# IPA list, and before is the current phoneme string (looked up when None). Returns ranked dicts with
# the respelling, its phonemes, the distances before and after, the stress flags and the rule that kept it.
# Runs the espeak helper subprocess for any text not already cached.
def score(word: str, candidates: list, row: dict | None = None, allow_stress_fix: bool = False,
          # explicit targets and current phonemes, used for heteronyms in context
          targets: list[str] | None = None, before: str | None = None) -> list[dict]:
    """Rank candidates by (distance to CMU, stress mismatch, naturalness, length).

    Only candidates strictly closer to CMU than the original survive. A candidate never breaks a
    primary stress that was right, and when the original stress was wrong the candidate must fix it
    (otherwise it does not address what was flagged). With allow_stress_fix, a candidate that fixes a
    wrong primary stress without increasing the distance is also kept (rule "stress").
    """
    # explicit targets win; otherwise CMU variants for the word
    targets = targets if targets is not None else _targets(word, row)
    if not targets:  # nothing to compare with
        return []  # so no candidate can be judged
    # plain strings are treated as respellings
    cands = [c if isinstance(c, dict) else {"text": c, "kind": "respell"} for c in candidates]
    if before is None:  # no current phonemes supplied by the caller
        before = (row or {}).get("espeak_en")  # take them from the lexicon row (espeak "en" of the word as narrated)
    # texts to phonemize: every candidate, plus the word itself when its current phonemes are still unknown
    need = [c["text"] for c in cands] + ([] if before is not None else [word])
    ph = dict(zip(need, lx.espeak(need)[lx.NARRATION_VOICE]))  # one helper call (or cache hits); text -> phoneme string
    if before is None:  # still unknown after the row lookup
        before = ph[word]  # use espeak's reading of the bare word
    b = lx.compare(before, targets)  # distance and stress of the current reading against the targets
    out = []  # candidates that pass the rules, with their phonemes and distances
    for c in cands:  # check every candidate against the current reading
        e = ph[c["text"]]  # its phonemes from espeak "en"
        if not e:  # espeak returned nothing, e.g. the candidate was only punctuation
            continue  # an empty reading cannot be compared, so the candidate is skipped
        r = lx.compare(e, targets)  # distance and stress of the candidate against the same targets
        if not r["stress_ok"]:  # a candidate must place the primary stress on a CMU-stressed syllable
            continue  # otherwise it is rejected, even when it is closer in sound
        if r["distance"] < b["distance"]:  # strictly closer than the current reading
            rule = "distance"  # kept by the distance rule
        # optional: current stress is wrong, candidate fixes it, and it is no farther away
        elif allow_stress_fix and not b["stress_ok"] and r["stress_ok"] and r["distance"] <= b["distance"]:
            rule = "stress"  # kept by the stress rule
        else:  # neither closer to CMU nor an allowed stress repair
            continue  # the candidate is discarded and never appears in the ranking
        # record the candidate with its kind and phonemes
        out.append({"respell": c["text"], "kind": c["kind"], "espeak_en": e, "distance_before": b["distance"],
                    # distance after the change, and the stress check before it
                    "distance_after": r["distance"], "stress_ok_before": b["stress_ok"],
                    # stress check after the change and which rule admitted it
                    "stress_ok_after": r["stress_ok"], "rule": rule})
    # closest first, then right stress, then most natural kind, then shortest text
    out.sort(key=lambda x: (x["distance_after"], not x["stress_ok_after"], KIND_RANK[x["kind"]], len(x["respell"])))
    return out  # ranked survivors with the best fix first; empty when nothing beats the current reading


# Finds the single best fix for each flagged lexicon row. flagged_rows are rows from lexicon_measure.json
# (flagged, review or display mismatches); max_words limits how many rows are tried (None means all);
# allow_stress_fix is passed to score(). Returns {word: summary of the top candidate plus up to five
# alternatives}. Words with no surviving candidate are left out. Used by main() and speechkit-voice-tts-offline-pipeline's autofix.
def best_fixes(flagged_rows: list, max_words=None, allow_stress_fix: bool = False) -> dict:
    """{word: {"respell", "distance_before", "distance_after", ...}} for flagged words that have a better candidate."""
    rows = flagged_rows[:max_words] if max_words else flagged_rows  # optionally only the first max_words rows
    props = {r["word"]: propose(r["word"], r) for r in rows}  # candidate lists per word
    lx.espeak([c["text"] for p in props.values() for c in p])  # one helper call for everything
    out = {}  # word -> chosen fix
    for r in rows:  # score each row's own candidates
        # espeak results now come from the warm cache
        ranked = score(r["word"], props[r["word"]], r, allow_stress_fix=allow_stress_fix)
        if ranked:  # at least one candidate survived
            top = ranked[0]  # the best ranked candidate
            # its text and the distance it starts from
            out[r["word"]] = {"respell": top["respell"], "distance_before": top["distance_before"],
                              # the distance it reaches and whether the stress was right before
                              "distance_after": top["distance_after"], "stress_ok_before": top["stress_ok_before"],
                              # stress after, the admitting rule and the candidate kind
                              "stress_ok_after": top["stress_ok_after"], "rule": top["rule"], "kind": top["kind"],
                              # phonemes before and after, for a human to compare
                              "espeak_before": r["espeak_en"], "espeak_after": top["espeak_en"],
                              # the CMU variants aimed at and the token ids where the word occurs
                              "cmu": r["cmu_arpabet"], "tokens": r.get("tokens", []),
                              # the next five ranked candidates, for a human to choose from
                              "alternatives": [x["respell"] for x in ranked[1:6]]}
    return out  # fixes keyed by word


# Proposes respellings for heteronym occurrences that espeak read with the wrong meaning. heteronyms is the
# "heteronyms" list from lexicon_measure.json; only occurrences marked suspect are handled. Each is
# scored against the CMU variants of the expected reading, starting from its in-context phonemes.
# Returns {word: {"expected_role", "target", "occurrences": [token, seg, expected_role, target, respell or None,
# distances]}}; the top-level role and target are the first suspect occurrence's.
def heteronym_fixes(heteronyms: list) -> dict:
    """For heteronym occurrences flagged as suspect, a respelling that forces the expected reading."""
    jobs = []  # (heteronym, occurrence, wanted ARPAbet variants) to score
    for h in heteronyms:  # each heteronym entry from the lexicon measurement
        for o in h["occurrences"]:  # every occurrence of it in the book
            if not o.get("suspect"):  # context and reading agree
                continue  # an occurrence read with the expected meaning needs no respelling
            # Keep the CMU variants whose verb reading matches the expected role. verb_reading() returns None for an
            # unknown variant, which counts as "not a verb" here.
            want = [a for a in h["cmu_arpabet"]
                    # True == True for an expected verb, False == False for an expected noun or adjective
                    if (lx.verb_reading(h["word"], a) is True) == (o["expected_role"] == "verb")]
            if want:  # at least one variant has the wanted reading
                jobs.append((h, o, want))  # queue the occurrence with the CMU variants it should sound like
    # (word, wanted variants) -> candidate list. Keyed by the reading as well as the word, because two
    # suspect occurrences of one word can need opposite readings (one wants the verb, another the noun);
    # each occurrence must be scored against candidates built from its own wanted variants.
    props = {}
    for h, _, want in jobs:  # build the candidate lists, one per word and wanted reading
        key = (h["word"], tuple(want))  # the word together with the CMU variants it should sound like
        if key not in props:  # not built yet for this reading
            cands = []  # candidates for this word and reading
            for a in want:  # each wanted CMU variant
                for r in respellings(a):  # each spelled form of that wanted variant
                    cands.append({"text": r, "kind": "respell"})  # added as a phonetic respelling candidate
            w = h["word"]  # the heteronym's spelling, cut below at every split point
            for i in range(2, len(w) - 1):  # every split with at least two letters on each side
                # hyphenation can shift espeak's stress to the other reading
                cands.append({"text": f"{w[:i]}-{w[i:]}", "kind": "hyphenate"})
            props[key] = list({c["text"]: c for c in cands}.values())  # drop duplicate texts, keep the order
    # phonemize all of them in one helper call to warm the cache
    lx.espeak([c["text"] for p in props.values() for c in p])
    out = {}  # word -> fix summary
    for h, o, want in jobs:  # score each suspect occurrence
        # targets are this occurrence's wanted readings only, with candidates built for those readings
        ranked = score(h["word"], props[(h["word"], tuple(want))], targets=[lx.arpabet_to_ipa(a) for a in want],
                       # start from the in-context phonemes and accept stress-only repairs
                       before=o["espeak_in_context"], allow_stress_fix=True)
        # one entry per word; its top-level role and target are those of its first suspect occurrence, and each
        # occurrence also records its own, since later occurrences may need the other reading
        ent = out.setdefault(h["word"], {"expected_role": o["expected_role"], "target": want, "occurrences": []})
        ent["occurrences"].append({  # add this occurrence's result
            # where the occurrence is and what espeak said there
            "token": o["token"], "seg": o["seg"], "sentence": o["sentence"], "espeak_in_context": o["espeak_in_context"],
            "expected_role": o["expected_role"], "target": want,  # the reading this occurrence needs
            "respell": ranked[0]["respell"] if ranked else None,  # best respelling, or None when nothing helped
            # distance of the in-context reading to the wanted variants
            "distance_before": ranked[0]["distance_before"] if ranked else None,
            "distance_after": ranked[0]["distance_after"] if ranked else None,  # distance after the respelling
            "espeak_after": ranked[0]["espeak_en"] if ranked else None})  # phonemes of the respelling
    return out  # fixes keyed by word


# Command-line entry point: reads a lexicon measurement and writes fix_candidates.json next to it. argv is
# the argument list (None means sys.argv). Side effects: runs the espeak helper subprocess, writes the
# output JSON and prints a summary. Returns 0.
def main(argv=None) -> int:
    # the first line of the module docstring becomes the --help description
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # lexicon measurement produced by lexicon.py
    ap.add_argument("--lexicon", default=str(lx.OUTDIR / "lexicon_measure.json"))
    ap.add_argument("--out", default=str(lx.OUTDIR / "fix_candidates.json"))  # where to write the candidates
    a = ap.parse_args(argv)  # parse argv, or the real command line when it is None
    lex = json.loads(Path(a.lexicon).read_text(encoding="utf-8"))  # load the lexicon measurement
    flagged = lex["flagged"]  # rows flagged by distance, stress or a dropped apostrophe
    strict = best_fixes(flagged)  # fixes that must reduce the distance
    with_stress = best_fixes(flagged, allow_stress_fix=True)  # second pass that also accepts stress-only repairs
    stress_only = {w: v for w, v in with_stress.items() if w not in strict}  # words fixed only by the looser rule
    het = heteronym_fixes(lex["heteronyms"])  # respellings for heteronyms read with the wrong meaning
    # review rows (proper names) that have a reason, a CMU entry and are not out of the dictionary
    review_issues = [r for r in lex["review"] if r.get("reason") and not r["oov"] and r.get("cmu_ipa")]
    review_fix = best_fixes(review_issues, allow_stress_fix=True)  # fixes for those, stress repairs allowed
    res = {  # the fix_candidates.json document written below
        "schema": SCHEMA,  # the emblem-rsi/pron-fix-candidates/1 format tag
        # which lexicon file and espeak voice the candidates were built from
        "source": {"lexicon": str(Path(a.lexicon).resolve()), "voice_espeak": lx.NARRATION_VOICE},
        "rule": "a candidate is kept only if its espeak 'en' phonemes are strictly closer to CMU than the original "
                "(same coarse distance as the lexicon probe) and it does not break a correct primary stress; "
                "stress_fixes additionally allow equal distance when a wrong primary stress becomes right",
        # how many flagged rows, how many have a strict fix, how many only a stress fix
        "totals": {"flagged": len(flagged), "with_better_candidate": len(strict), "stress_only_fixes": len(stress_only),
                   # flagged rows left without any candidate
                   "no_candidate": len(flagged) - len(strict) - len(stress_only),
                   # heteronym occurrences for which some respelling worked
                   "heteronym_occurrences_fixed": sum(1 for v in het.values() for o in v["occurrences"] if o["respell"])},
        "fixes": strict,  # word -> strict fix
        "stress_fixes": stress_only,  # word -> stress-only fix
        # flagged words that no rule could fix
        "no_candidate": [r["word"] for r in flagged if r["word"] not in with_stress],
        "heteronym_fixes": het,  # heteronym fixes by word
        "review_fixes": review_fix,  # fixes for review rows
    }
    out = lx.write_json(res, Path(a.out))  # atomic write through a temporary file
    t = res["totals"]  # totals for the console summary
    print(f"wrote {out}")  # say where the file went
    # first part of the summary: flagged rows and how many got a strict fix
    print(f"  flagged {t['flagged']}: better candidate {t['with_better_candidate']}, stress-only fix "
          # continuation of the totals line
          f"{t['stress_only_fixes']}, none {t['no_candidate']}; heteronym occurrences with a fix "
          f"{t['heteronym_occurrences_fixed']}")  # end of the totals line
    for w, v in list(strict.items()) + list(stress_only.items()):  # every chosen fix, strict ones first
        # word, chosen text and the distance change
        print(f"  {w:<14} -> {v['respell']:<16} d {v['distance_before']:.2f} -> {v['distance_after']:.2f}  "
              # stress flags and phonemes before and after
              f"stress {v['stress_ok_before']}->{v['stress_ok_after']}  {v['espeak_before']} -> {v['espeak_after']}")
    for w, v in het.items():  # each heteronym with fixes
        for o in v["occurrences"]:  # every suspect occurrence of that heteronym, including those left without a fix
            # role, word, segment, respelling and the phoneme change
            print(f"  [{o['expected_role']}] {w:<10} {o['seg']} -> {o['respell']}  {o['espeak_in_context']} -> {o['espeak_after']}")
    return 0  # exit code 0; failures raise exceptions instead


if __name__ == "__main__":  # run only as a script
    sys.exit(main())  # exit with main()'s return code
