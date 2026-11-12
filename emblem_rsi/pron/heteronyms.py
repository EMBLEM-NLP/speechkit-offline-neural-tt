# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Heteronym roles before voicing: the reading espeak will give each occurrence, and the reading its sentence wants.

speechkit-narrate's prevoice stage uses this to fix heteronyms before narration; evaluate() scores the rule sets against the
pronunciation loop's own token fixes.

    pieces(tokens, segments)   the script's sentence pieces as the narration voices them (inventory.piece_words)
    find(pieces)               every heteronym occurrence, at its own token position
    read(pieces, occurrences)  espeak's reading of each occurrence in its sentence (one helper call)
    judge(pieces, occurrences, mode, tagger)   the reading the sentence wants under a rule set, and why
    decide(occurrence)         fix, keep or skip, and the CMU variant a fix should say
    fix_phonemes(wanted)       the phonemes a fix gives the voice: espeak's own for that reading, else the CMU variant
    evaluate(...)              caught / missed / false alarms against the loop's token fixes

Rule sets (mode):
    v1                     lexicon.explain_role version 1, the lexicon probe's own rules
    v2                     version 2: v1 plus a clause-start imperative rule and a subject-then-determiner rule
    v1+tagger, v2+tagger   the rule decides and the part-of-speech tagger must agree (rule alone without spaCy)
    tagger                 the tagger alone (it also tells the past "read" from the present one)
    v1-then-tagger, v2-then-tagger   the rule where it has an opinion, the tagger elsewhere

    python -m emblem_rsi.pron.heteronyms PROJECT [PROJECT ...] [--modes v1,v2,v2+tagger] [--json]
"""
# Store type hints as unevaluated strings, so `str | None` works on every supported Python.
from __future__ import annotations

import argparse  # command line of the evaluation
import json  # project files in, the evaluation out
import re  # the plain-word shape a per-token fix can replace
import sys  # exit code of the command line
from collections import Counter, defaultdict  # words per token; occurrences per token
from pathlib import Path  # project folders and their files

# Started as a plain script there is no parent package, so the repository root is put on sys.path and the modules
# are imported by their full names; inside the package relative imports are used.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # parents[2] is the repository root
    from emblem_rsi.pron import inventory, lexicon  # noqa: E402
    from emblem_rsi.pron import tagger as postagger  # noqa: E402
else:  # normal case: imported as emblem_rsi.pron.heteronyms
    from . import inventory, lexicon  # the shared word splitter; the rules, CMU lookups and espeak adapter
    from . import tagger as postagger  # the optional spaCy adapter (loaded only when a tagger mode is used)

# Every rule set judge() accepts, in the order the evaluation reports them.
MODES = ("v1", "v2", "v1+tagger", "v2+tagger", "tagger", "v1-then-tagger", "v2-then-tagger")
# The default: v1 confirmed by the tagger, the only set with no false alarm on either evaluated book.
DEFAULT_MODE = "v1+tagger"
# The words looked for: the curated heteronym list and every word whose readings verb_reading() can tell apart.
CANDIDATES = frozenset(lexicon.HETERONYMS | lexicon.ROLE_WORDS)
# A token a per-token fix can replace: one plain word and its trailing punctuation (docmodel.apply_override's shape).
PLAIN = re.compile(r"[A-Za-zÀ-ÿ'’-]+[.,;:!?]*")


# The sentence pieces of a script, split exactly as the narration voices them (after a token ending in . ! or ?).
# tokens is the docmodel token list, indexed by token id: "spoken" is what the voice is given, "spoken_ref" what a
# listener should hear. segments lists (segment id, token ids) in narration order. Returns one dict per piece:
# seg, text (the voiced tokens joined, what espeak reads), ref (the reference tokens joined), spoken (token id ->
# voiced token text) and words (inventory.piece_words over the piece).
def pieces(tokens, segments) -> list[dict]:
    out = []  # pieces in narration order
    for seg_id, ids in segments:  # each narration segment
        voiced = [tokens[i]["spoken"] for i in ids]  # what the voice is given, token by token
        for pos in inventory.sentence_pieces(voiced):  # the segment's pieces, as positions into ids
            # (token id, what a listener should hear, what the voice is given): the inventory's reference rule
            items = [(ids[p], tokens[ids[p]].get("spoken_ref", voiced[p]) or voiced[p], voiced[p]) for p in pos]
            out.append({"seg": seg_id, "text": " ".join(it[2] for it in items),  # the text espeak will read
                        "ref": " ".join(it[1] for it in items),  # the words a listener should hear
                        "spoken": {it[0]: it[2] for it in items},  # each token's voiced text, for the plain check
                        "words": inventory.piece_words(items)})  # the shared splitter's words
    return out  # every piece of the script


# Every heteronym occurrence in the pieces, each at its own position (a word used twice in a sentence is judged
# twice). Returns dicts with piece (index), k (word index in the piece), token, sub (word index in the token), word
# (its key), seg, single (the token holds no other word) and plain (the token's voiced text is one plain word).
def find(pcs: list[dict]) -> list[dict]:
    occ = []  # occurrences in narration order
    for pi, p in enumerate(pcs):  # each piece with its index
        per_token = Counter(w.token for w in p["words"])  # how many words each token holds
        for k, w in enumerate(p["words"]):  # each word with its position in the piece
            if w.norm in CANDIDATES:  # a heteronym
                # where it is, what it is, and whether a per-token fix could replace its token
                occ.append({"piece": pi, "k": k, "token": w.token, "sub": w.sub, "word": w.norm, "seg": p["seg"],
                            "single": per_token[w.token] == 1,  # a hyphenated token holds several words
                            "plain": bool(PLAIN.fullmatch(p["spoken"][w.token]))})  # quotes or digits do not fit
    return occ  # the occurrences to read and judge


# espeak's reading of each occurrence in its sentence, the way the lexicon probe measures it after narration: every
# piece holding an occurrence is phonemized whole with the narration voice, its phonemes are split among its words
# (lexicon.context_phonemes) and the occurrence's share is matched to its CMU variants. phonemize(texts, voices)
# returns {voice: [phonemes per text]} (default lexicon.espeak: one helper call). Adds in_context, nearest (the CMU
# variant espeak is closest to) and reading (its role, or None) to every occurrence; returns the list.
def read(pcs: list[dict], occ: list[dict], phonemize=None) -> list[dict]:
    phonemize = phonemize or lexicon.espeak  # Piper's espeak bridge in the speechkit runtime, by default
    need = sorted({o["piece"] for o in occ})  # only pieces that hold an occurrence are phonemized
    # the piece texts and every word's voiced form, each once, for one helper call
    texts = sorted({pcs[i]["text"] for i in need} | {w.voiced for i in need for w in pcs[i]["words"] if w.voiced})
    voice = lexicon.NARRATION_VOICE  # espeak "en", the voice the narration phonemizes with
    en = dict(zip(texts, phonemize(texts, (voice,))[voice], strict=True)) if texts else {}  # text -> phonemes
    ctx = {}  # (token, sub) -> in-context phonemes of that word
    for i in need:  # each piece with an occurrence
        wl = [w for w in pcs[i]["words"] if w.voiced]  # words the voice gets something for (as the probe aligns)
        parts = lexicon.context_phonemes(en[pcs[i]["text"]], [en[w.voiced] for w in wl])  # the piece split by word
        ctx.update({(w.token, w.sub): ph for w, ph in zip(wl, parts, strict=True)})  # remember each word's share
    variants = {}  # word -> its CMU variants as ARPAbet strings, looked up once
    for o in occ:  # read every occurrence
        vs = variants.setdefault(o["word"], [" ".join(v) for v in lexicon.cmu_lookup(o["word"])[1]])  # cmudict order
        cp = ctx.get((o["token"], o["sub"]), "")  # its in-context phonemes ("" when it got none)
        used, role = lexicon.context_reading(o["word"], cp, vs)  # nearest variant (stress first) and its role
        o.update(in_context=cp, nearest=used, reading=role)  # what espeak will say there
    return occ  # the same list, now with readings


# How a rule's verdict and the tagger's combine under a mode. rule/why come from explain_role (or say there is no
# rule), tag/trole from the tagger, use_tag says whether a tagger is in use. Returns (expected role or None, reason).
def _combine(mode: str, rule, why: str, tag, trole, use_tag: bool):
    if mode in ("v1", "v2"):  # rules only
        return rule, why  # the rule decides alone
    if not use_tag:  # a tagger mode on a machine without spaCy: rules only
        if mode == "tagger":  # nothing else to fall back to
            return None, "tagger not installed"  # no opinion at all
        return rule, f"{why}; tagger not installed, rule alone"  # the rule part of the mode
    seen = f"tagger {tag or 'gave no tag'}" + (f" ({trole})" if trole else "")  # what the tagger said, for the reason
    if mode == "tagger":  # the tagger alone
        return trole, seen  # its role, or None for a tag without an opinion
    if mode.endswith("+tagger"):  # the rule decides and the tagger must agree
        if rule is None:  # the rule has no opinion, so there is nothing to confirm
            return None, why  # not judged
        if trole == rule:  # both say the same
            return rule, f"{why}; {seen} agrees"  # confirmed
        return None, f"{why}; {seen} does not confirm it"  # a disagreement is never acted on
    if rule is not None:  # rule-then-tagger: the rule wherever it has an opinion
        return rule, why  # the tagger is not asked
    return trole, f"{why}; {seen}"  # elsewhere the tagger's role (None when it has none)


# The reading each occurrence's sentence wants, under one rule set. mode is one of MODES; tagger is an object with
# tags(text, spans) and role(tag, word) (the emblem_rsi.pron.tagger module), or None for rules only. Adds rule and
# rule_reason (explain_role), tag and tag_role (the tagger), expected and reason (the mode's verdict) to every
# occurrence; returns the list. Raises ValueError for an unknown mode.
def judge(pcs: list[dict], occ: list[dict], mode: str = DEFAULT_MODE, tagger=None) -> list[dict]:
    if mode not in MODES:  # a typo in project.json must not silently pick another rule set
        raise ValueError(f"unknown heteronym rule set {mode!r}; use one of {', '.join(MODES)}")  # list them all
    version = 2 if mode.startswith("v2") else 1  # the rule version the mode builds on (tagger alone: v1, unused)
    use_tag = "tagger" in mode and tagger is not None  # only tagger modes ask the tagger, and only when there is one
    tags = {}  # piece index -> tag per word, for the pieces that hold an occurrence
    if use_tag:  # tag each piece once, every word in its own sentence
        for i in sorted({o["piece"] for o in occ}):  # pieces with an occurrence
            if "tags" not in pcs[i]:  # not tagged yet (evaluate() judges the same pieces under several modes)
                pcs[i]["tags"] = tagger.tags(pcs[i]["ref"], [(w.start, w.end) for w in pcs[i]["words"]])  # tag once
            tags[i] = pcs[i]["tags"]  # the reference words' tags
    for o in occ:  # judge every occurrence
        words = pcs[o["piece"]]["words"]  # its piece's words
        if o["word"] in lexicon.ROLE_WORDS:  # a word whose two readings the rules can tell apart
            # the rule's verdict at this occurrence's own position, with the clause breaks of its piece
            rule, why = lexicon.explain_role([w.norm for w in words], o["k"], o["word"],
                                             [w.punct_after for w in words], version)  # the mode's rule version
        else:  # wind, lead, read and the like: verb_reading() cannot classify their variants
            rule, why = None, "no rule tells this word's readings apart"  # only the tagger may judge "read"
        tag = tags[o["piece"]][o["k"]] if use_tag else None  # the tagger's Penn tag for this word
        trole = tagger.role(tag, o["word"]) if use_tag else None  # the role that tag stands for
        expected, reason = _combine(mode, rule, why, tag, trole, use_tag)  # the mode's verdict
        # everything that went into the verdict, for the decisions file
        o.update(rule=rule, rule_reason=why, tag=tag, tag_role=trole, expected=expected, reason=reason)
    return occ  # the same list, now judged


# The first CMU variant of word that has the given role (variant_role), as an ARPAbet string, or None. The pronunciation
# loop picks its heteronym fixes the same way, so a fix made here is the fix the loop would make.
def target_variant(word: str, role: str):
    for v in lexicon.cmu_lookup(word)[1]:  # the dictionary's variants, in cmudict order
        arpa = " ".join(v)  # as one space-separated string
        if lexicon.variant_role(word, arpa) == role:  # the reading the sentence wants
            return arpa  # the first one wins
    return None  # the dictionary has no variant for that reading


# Short phrases that make espeak give each reading of a heteronym, tried in order: the infinitive or a pronoun subject
# for the verb ("to record", "it increases"), an article for the noun or adjective, "had" for the past "read".
CARRIERS = {"verb": ("to {}", "it {}", "we {}"), "noun-adj": ("the {}", "a {}"), "past": ("had {}",),
            "present": ("to {}",)}  # "to read" is the present reading


# The phonemes a fix should give the voice for each wanted (word, role) pair. Preferred: espeak's own phonemes for that
# reading, taken from the first carrier phrase (CARRIERS) whose reading of the word matches its CMU variant for the
# role; these are what the voice was trained on, and what the pronunciation loop's acoustically checked fixes use.
# Otherwise the first CMU variant for the role converted by to_voice (ARPAbet -> voice symbols), when given. wanted is
# an iterable of (word, role); phonemize(texts, voices) as for read() (one call for every phrase). Returns
# {(word, role): (phonemes or None, how they were made)}.
def fix_phonemes(wanted, phonemize=None, to_voice=None) -> dict:
    phonemize = phonemize or lexicon.espeak  # Piper's espeak bridge in the speechkit runtime, by default
    wanted = sorted(set(wanted))  # each pair once, in a stable order
    phrases = {wr: [c.format(wr[0]) for c in CARRIERS.get(wr[1], ())] for wr in wanted}  # carrier phrases per pair
    said = {p for ps in phrases.values() for p in ps}  # every carrier phrase, once
    texts = sorted(said | {x for p in said for x in p.split()})  # the phrases, and each of their words alone
    voice = lexicon.NARRATION_VOICE  # espeak "en", as the narration phonemizes
    en = dict(zip(texts, phonemize(texts, (voice,))[voice], strict=True)) if texts else {}  # text -> phonemes
    out = {}  # (word, role) -> (phonemes, how)
    for (w, role), ps in phrases.items():  # each wanted reading
        variants = [" ".join(v) for v in lexicon.cmu_lookup(w)[1]]  # the dictionary's readings of the word
        got = None  # the phonemes found for this reading, with how they were made
        for phrase in ps:  # carriers in order of preference
            # the phrase's phonemes split among its words; the heteronym is the last word
            part = lexicon.context_phonemes(en[phrase], [en[x] for x in phrase.split()])[-1]
            if part and lexicon.context_reading(w, part, variants)[1] == role:  # espeak gives the wanted reading here
                got = (part, f"espeak in '{phrase}'")  # its own phonemes for that reading
                break  # the first matching carrier wins
        if got is None and to_voice is not None:  # no carrier made espeak say it: use the dictionary
            target = target_variant(w, role)  # the first CMU variant with that reading
            got = (to_voice(target), f"CMU {target}") if target else None  # in the voice's own symbols
        out[(w, role)] = got or (None, f"no phonemes for the {role} reading")  # nothing usable
    return out  # one entry per wanted pair


# What to do about one judged and read occurrence. Returns (action, reason, target): action "fix" (espeak would give
# the other reading; target is the CMU variant to say), "keep" (espeak already gives the wanted reading) or "skip"
# (no verdict, no reading, or no variant for the wanted reading); target is None unless the action is "fix".
def decide(o: dict):
    if o.get("expected") is None:  # the rule set has no opinion here
        return "skip", f"no expected reading ({o.get('reason')})", None  # nothing to act on
    if o.get("reading") is None:  # espeak's reading could not be matched to a role
        return "skip", "espeak's reading could not be told apart", None  # never fix blind
    if o["expected"] == o["reading"]:  # espeak already says what the sentence wants
        return "keep", f"espeak already gives the {o['reading']} reading ({o['reason']})", None  # nothing to change
    target = target_variant(o["word"], o["expected"])  # the dictionary reading to say instead
    if target is None:  # no dictionary variant has the wanted reading
        return "skip", f"no dictionary variant for the {o['expected']} reading", None  # cannot make a fix
    # a fix: espeak would say the other reading
    why = f"espeak gives the {o['reading']} reading; the sentence wants the {o['expected']} ({o['reason']})"
    return "fix", why, target  # the CMU variant to say instead


# Scores rule sets against the pronunciation loop's own token fixes. measurement is a lexicon_measure.json taken before
# the fixes (its heteronym occurrences carry espeak's in-context reading as nearest_cmu); labels are the token ids the
# loop fixed (pronunciations.json "tokens"); tokens and segments describe the script as for pieces(). Each occurrence is
# judged at its own token position under every mode in modes. Returns {"occurrences", "labels" (fixed tokens that are
# heteronym occurrences), "modes": {mode: {"caught", "missed", "false_alarms" (counts) and the token lists}}}.
def evaluate(measurement: dict, labels, tokens, segments, modes=MODES, tagger=None) -> dict:
    labels = {int(t) for t in labels}  # token ids as ints (JSON object keys are strings)
    pcs = pieces(tokens, segments)  # the script's pieces with the shared splitter's words
    words_of = defaultdict(list)  # token id -> [(sub, key, piece index, word index)]
    for i, p in enumerate(pcs):  # index every word of every piece by its token
        for k, w in enumerate(p["words"]):  # each word with its position in the piece
            words_of[w.token].append((w.sub, w.norm, i, k))  # where the word sits
    occ = []  # one entry per measured occurrence
    for h in measurement.get("heteronyms", []):  # each heteronym the probe found
        for o in h.get("occurrences", []):  # each of its occurrences
            ws = words_of.get(o["token"], [])  # the token's words in the script
            # the heteronym's position inside its token, as the probe looks it up (first match, else the first word)
            hit = next((x for x in ws if x[1] == h["word"]), ws[0] if ws else None)
            if hit is None:  # the token is not in this script (another edition of the book)
                continue  # cannot be judged
            nearest = o.get("nearest_cmu")  # the CMU variant espeak's in-context reading was closest to
            # the measured reading in judge()'s terms (for "read" too), and whether the loop fixed this token
            occ.append({"piece": hit[2], "k": hit[3], "token": o["token"], "sub": hit[0], "word": h["word"],
                        "reading": lexicon.variant_role(h["word"], nearest) if nearest else None,  # None: unmatched
                        "label": o["token"] in labels})  # the loop's own verdict
    out = {"occurrences": len(occ), "labels": len({o["token"] for o in occ if o["label"]}), "modes": {}}  # totals
    for mode in modes:  # score each rule set on the same occurrences
        judge(pcs, occ, mode, tagger)  # sets expected for every occurrence under this mode
        caught, missed, false = [], [], []  # token lists
        for o in occ:  # compare the prediction with the loop's fix
            # the rule set says espeak's reading is the wrong one here
            predicted = o["expected"] is not None and o["reading"] is not None and o["expected"] != o["reading"]
            if predicted and o["label"]:  # predicted and fixed by the loop
                caught.append(o["token"])  # a caught fix
            elif predicted:  # predicted, but the loop left it alone
                false.append(o["token"])  # a false alarm
            elif o["label"]:  # fixed by the loop, not predicted
                missed.append(o["token"])  # a missed fix
        # counts first, then the token lists for inspection
        out["modes"][mode] = {"caught": len(caught), "missed": len(missed), "false_alarms": len(false),
                              "caught_tokens": caught, "missed_tokens": missed, "false_alarm_tokens": false}  # lists
    return out  # one score per mode


# The earliest lexicon measurement of a project, the one taken before the loop's fixes: the first measure-NNN folder
# under work/rsi/pron that holds lexicon/lexicon_measure.json. Returns its Path, or None when there is none.
def baseline_measurement(root) -> Path | None:
    found = sorted((Path(root) / "work" / "rsi" / "pron").glob("measure-*/lexicon/lexicon_measure.json"))  # in order
    return found[0] if found else None  # measure-001 normally


# evaluate() for one speechkit-narrate project folder: its docmodel.json and request.json (the script), pronunciations.json
# (the loop's token fixes are the labels) and its baseline lexicon measurement. Reads files only. Returns the
# evaluation plus "project" and "measurement"; raises FileNotFoundError when a file is missing.
def evaluate_project(root, modes=MODES, tagger=None) -> dict:
    work = Path(root) / "work"  # every input lives in the work folder
    m = baseline_measurement(root)  # the measurement taken before the fixes
    if m is None:  # the loop never measured this project
        raise FileNotFoundError(f"no lexicon measurement under {work / 'rsi' / 'pron'}")  # nothing to score against

    # Reads one JSON file of the project as UTF-8 (Windows' default code page would break on IPA).
    def load(p):
        return json.loads(Path(p).read_text(encoding="utf-8"))  # the parsed file
    dm, req = load(work / "docmodel.json"), load(work / "request.json")  # the script
    labels = load(work / "pronunciations.json").get("tokens", {})  # the loop's token fixes
    segs = [(s["id"], s["tokenIndex"]) for s in req["segments"]]  # (segment id, token ids) per segment
    res = evaluate(load(m), labels, dm["tokens"], segs, modes, tagger)  # score every mode
    return {"project": str(root), "measurement": str(m), **res}  # where the numbers came from, then the numbers


# Command-line entry point: prints caught / missed / false alarms per rule set for each project given. argv is the
# argument list (None means sys.argv). Uses spaCy when it is installed. Returns 0; reads files only.
def main(argv=None) -> int:
    # the command line: project folders, and optionally the rule sets and JSON output
    ap = argparse.ArgumentParser(description="Score heteronym rule sets against the pronunciation loop's token fixes.")
    ap.add_argument("projects", nargs="+")  # speechkit-narrate project folders
    ap.add_argument("--modes", default=",".join(MODES))  # comma-separated rule sets, all by default
    ap.add_argument("--json", action="store_true")  # print the full evaluation as JSON instead of a table
    a = ap.parse_args(argv)  # read the options
    modes = [m.strip() for m in a.modes.split(",") if m.strip()]  # the requested rule sets
    tg = postagger if postagger.available() else None  # the tagger when spaCy and its model are installed
    results = [evaluate_project(p, modes, tg) for p in a.projects]  # one evaluation per project
    if a.json:  # machine-readable output
        print(json.dumps(results, ensure_ascii=False, indent=1))  # everything, token lists included
        return 0  # done
    print(f"tagger: {postagger.describe()}")  # which tagger the tagger modes used
    for r in results:  # one block per project
        print(f"{r['project']}: {r['labels']} token fixes among {r['occurrences']} heteronym occurrences")  # totals
        for mode, s in r["modes"].items():  # one line per rule set
            # caught, missed and false alarms, aligned in columns
            print(f"  {mode:16} caught {s['caught']:3}  missed {s['missed']:3}  false alarms {s['false_alarms']:3}")
    return 0  # findings are printed, never a failure


if __name__ == "__main__":  # only when run as a script or with python -m
    sys.exit(main())  # exit with main()'s return value
