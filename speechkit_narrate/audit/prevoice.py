# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Fix before voicing: per-token fixes for heteronyms that espeak would read in the wrong role (the prevoice stage).

Opt-in with project.json {"prevoice": {"enabled": true}}. It runs after the script and before narration:
  1. the script is built in memory with work/pronunciations.json applied and none of this stage's own fixes
  2. every sentence piece holding a heteronym is phonemized whole with espeak "en", as narrate will voice it, and each
     occurrence's share is matched to its CMU variants (emblem_rsi.pron.heteronyms)
  3. the reading its sentence wants is decided under prevoice.rules (default "v1+tagger": the v1 rules, confirmed by
     spaCy's tagger when it is installed, else the rules alone)
  4. an occurrence espeak would read the other way gets a per-token fix: [[the wanted CMU variant in the voice's own
     symbols]], the same form the pronunciation loop writes into pronunciations.json "tokens"
  5. the word audit runs over these fixes and pronunciations.json's word overrides; its findings are warnings
  6. the script is rebuilt, so request.json, the input narrate fingerprints, carries the fixes to the voice
Priority: work/pronunciations.json always wins. A token it fixes, or a word it fixes everywhere, is never touched, and
in the script its fixes are applied first and this stage's last.
Files (work/prevoice/): fixes.json (read by the script stage), decisions.json (every occurrence considered: the
reading espeak gives, the reading wanted, the decision and why) and word_audit.json.
Turned off again, the stage runs once more: it deletes fixes.json and rebuilds the script without them.
"""
# Store type hints as unevaluated strings, so `str | None` works on every supported Python.
from __future__ import annotations

import hashlib  # SHA-256 of book.md, stored with the fixes so a stale file is never applied
from functools import partial  # binds the runtime python into the espeak adapter
from pathlib import Path  # the runtime interpreter and the output files

from .. import env  # where the speechkit runtime and the voice config are
from ..project import ProjectError  # a wrong setting is reported as a project problem
from ..text import docmodel  # the script builder, which applies the fixes this stage writes

DIR = "work/prevoice"  # this stage's folder, relative to the project
FIXES = docmodel.PREVOICE_FIXES  # the per-token fixes the script stage applies (lowest priority)
DECISIONS = f"{DIR}/decisions.json"  # every occurrence considered, with the decision and its reason
AUDIT = f"{DIR}/word_audit.json"  # the word audit of the fixes and the word overrides


# True when project.json turns the stage on (prevoice.enabled). project is the Project.
def enabled(project) -> bool:
    return bool((project.cfg.get("prevoice") or {}).get("enabled"))  # off unless the project says otherwise


# The stage's satisfied hook: a reason to skip it, or None when it has to run. It must run when it is on, and once more
# after it was turned off while its fixes file still exists (to take the fixes out of the script). Read-only.
def off_reason(project) -> str | None:
    if enabled(project):  # on: the planner's fingerprints decide whether it is due
        return None  # never skipped by this hook
    if (project.root / FIXES).exists():  # turned off, but the script still carries its fixes
        return None  # run once to remove them
    return "off (project.json prevoice.enabled is false)"  # the default: nothing to do, and nothing changes


# Writes one dict as indented UTF-8 JSON at a project-relative path, atomically (a .tmp file, then a rename).
def _write(project, rel: str, obj: dict) -> Path:
    from emblem_rsi.pron import lexicon  # its write_json is the toolkit's atomic UTF-8 writer
    return lexicon.write_json(obj, project.root / rel)  # the parent folder is created when needed


# Runs the stage for one project. log receives progress lines. phonemize(texts, voices) -> {voice: [phonemes]}
# defaults to Piper's espeak bridge in the speechkit runtime; tagger is "auto" (spaCy when the rule set uses it and
# it is installed), None (rules only) or an object with tags() and role() like emblem_rsi.pron.tagger; symbols is
# the voice's phoneme set (default: read from the profile's voice config). Returns a summary dict. Side effects:
# writes work/prevoice/*.json and rebuilds work/docmodel.json and work/request.json; off, it only removes its fixes.
def run(project, log=print, phonemize=None, tagger="auto", symbols=None) -> dict:
    fixes_path = project.root / FIXES  # where the script stage looks for the fixes
    if not enabled(project):  # off (run anyway because it was forced, or to clean up)
        if fixes_path.exists():  # fixes from when it was on
            fixes_path.unlink()  # the script stage would ignore them anyway; deleting them makes "off" visible
            docmodel.build(project)  # request.json without them, so narrate re-voices those segments plainly
            log("prevoice: off; its fixes were taken out of the script")  # say what changed
            return {"enabled": False, "removed": True}  # the stage did one thing
        log("prevoice: off (project.json prevoice.enabled)")  # nothing to do
        return {"enabled": False}  # no file was touched
    # imported only when the stage is on: the heteronym engine, the espeak adapter, the tagger and the word audit
    from emblem_rsi.pron import heteronyms as het, lexicon, wordaudit
    from emblem_rsi.pron import tagger as postagger  # the optional spaCy adapter
    from .autofix import arpabet_to_voice  # the loop's ARPAbet -> voice-symbol converter, so fixes match the loop's
    mode = (project.cfg.get("prevoice") or {}).get("rules") or het.DEFAULT_MODE  # the rule set to judge with
    if mode not in het.MODES:  # a typo must not silently fall back to another rule set
        raise ProjectError(f"project.json prevoice.rules {mode!r} is not one of {', '.join(het.MODES)}")  # list them
    if phonemize is None:  # the real phonemizer: espeak "en" through the runtime venv's helper
        runtime = env.runtime_python()  # the speechkit runtime's python, which has Piper's espeak bridge
        if not Path(runtime).exists():  # no voice runtime on this machine
            # the stage is Linux-only, like narrate; on Windows the registry delegates it to WSL
            raise RuntimeError(f"prevoice needs the speechkit runtime's espeak ({runtime} not found)")
        phonemize = partial(lexicon.espeak, runtime=runtime)  # one helper call per batch of texts
    auto = tagger == "auto"  # look for spaCy here, rather than use what the caller passed
    if auto:  # the tagger is used only by tagger rule sets, and only when spaCy loads
        tagger = postagger if "tagger" in mode and postagger.available() else None  # None: rules only
    if "tagger" not in mode:  # rules-only sets never ask the tagger
        tag_info = "not used"  # recorded so the decisions file is self-explanatory
    elif tagger is not None:  # a tagger is in use
        tag_info = getattr(tagger, "describe", lambda: "supplied by the caller")()  # spaCy version and model
    elif auto:  # a tagger rule set on a machine where spaCy or its model is missing
        tag_info = postagger.describe()  # "not installed (rules only)"
    else:  # the caller asked for rules only
        tag_info = "none given (rules only)"  # the rule part of the set decides alone
    if symbols is None:  # the voice's own phoneme set, from the profile's config
        symbols = wordaudit.voice_symbols(env.voice_config(project.cfg["voice"]["profile"]))  # phoneme_id_map keys

    _, flat, segs, _ = docmodel.model(project, prevoice=False)  # the script as hand and loop fixes make it
    words_ov, tokens_ov = docmodel.load_overrides(project.p("pronunciations"))  # the fixes that always win
    pcs = het.pieces(flat, [(s["id"], s["tokens"]) for s in segs])  # sentence pieces as narrate voices them
    occ = het.read(pcs, het.find(pcs), phonemize=phonemize)  # every heteronym, with espeak's reading in context
    het.judge(pcs, occ, mode, tagger)  # the reading each sentence wants, and why

    for o in occ:  # first pass: decide every occurrence, in narration order
        if o["token"] in tokens_ov:  # a hand or loop fix for this very token
            o["decision"] = ("keep", "pronunciations.json fixes this token, and its fixes win")  # never touched
        elif o["word"] in words_ov:  # the word is fixed everywhere by pronunciations.json
            o["decision"] = ("keep", "pronunciations.json fixes this word everywhere, and its fixes win")  # same rule
        elif not (o["single"] and o["plain"]):  # hyphenated, quoted or otherwise not one plain word
            # apply_override only replaces a token that is one plain word
            o["decision"] = ("skip", "the token is not one plain word, so a per-token fix cannot replace it")
        else:  # free to judge
            o["decision"] = het.decide(o)[:2]  # fix, keep or skip, and why
    # the phonemes of every wanted reading, in one espeak call: espeak's own (from a carrier phrase) or the CMU variant
    made = het.fix_phonemes({(o["word"], o["expected"]) for o in occ if o["decision"][0] == "fix"},
                            phonemize=phonemize, to_voice=arpabet_to_voice)  # the loop's ARPAbet converter
    fixes, rows = {}, []  # token id -> fix; one decision record per occurrence
    for o in occ:  # second pass: the fixes and the decision records
        (action, reason), say, how = o["decision"], None, None  # the decision, and its fix when one is made
        if action == "fix":  # espeak would read it the other way
            ph, how = made[(o["word"], o["expected"])]  # the wanted reading's phonemes, and where they came from
            missing = sorted({c for c in (ph or "") if c not in symbols})  # symbols the voice has no id for
            if not ph or missing:  # no phonemes, or phonemes the voice could not say
                # the reason names the symbols the voice has no id for, or why no phonemes were found
                action, reason = "skip", (f"the fix [[{ph}]] uses symbols the voice lacks: {' '.join(missing)}"
                                          if ph else how)  # why no fix was made after all
            else:  # a fix the voice can say
                say = f"[[{ph}]]"  # Piper raw phonemes, the form the loop writes into pronunciations.json
                fixes[str(o["token"])] = {"word": o["word"], "say": say}  # JSON object keys are strings
        # the record for the decisions file: where, what espeak says, what is wanted, and the outcome
        rows.append({"token": o["token"], "seg": o["seg"], "word": o["word"], "display": flat[o["token"]]["disp"],
                     "sentence": pcs[o["piece"]]["ref"],  # the words a listener should hear
                     # espeak's phonemes in context, the CMU variant they are nearest to, and its reading
                     "in_context": o["in_context"], "nearest": o["nearest"], "reading": o["reading"],
                     # the rule's verdict with its reason, and the tagger's tag with its reading
                     "rule": o["rule"], "rule_reason": o["rule_reason"], "tag": o["tag"], "tag_role": o["tag_role"],
                     "expected": o["expected"], "decision": action, "reason": reason,  # the outcome and why
                     "fix": say, "fix_from": how if say else None})  # the phonemes given to the voice, and their source

    book_sha = hashlib.sha256(project.p("book").read_bytes()).hexdigest()  # ties the fixes to this book.md
    # the fixes file the script stage reads, then the script rebuilt with them
    _write(project, FIXES, {"schema": "speechkit_narrate/prevoice-fixes/1", "book_sha256": book_sha, "rules": mode,
                            "about": "Per-token fixes made before narration by the prevoice stage (lowest priority: "
                                     "work/pronunciations.json always wins). [[...]] is Piper raw phoneme input.",
                            "tokens": fixes})  # token id -> {"word", "say"}
    script = docmodel.build(project)  # request.json now carries the fixes, so narrate's fingerprint sees them
    # the word audit: pronunciations.json's word overrides, then each distinct fix this stage made
    pairs = list(words_ov.items()) + sorted({(f["word"], f["say"]) for f in fixes.values()})
    audit = wordaudit.audit(pairs, symbols, phonemize=phonemize)  # rows only; this stage writes them below
    for i, r in enumerate(audit):  # the overrides come first, in the order the pairs were given
        r["source"] = "pronunciations.json" if i < len(words_ov) else "prevoice"  # where each audited fix comes from
    _write(project, AUDIT, {"schema": "speechkit_narrate/prevoice-word-audit/1", "rows": audit})  # work/prevoice only
    warns = wordaudit.warnings(audit)  # word-audit findings: fixes with missing symbols, fixes that change nothing
    dropped = [r for r in rows if "symbols the voice lacks" in r["reason"]]  # fixes the voice could not have said
    warns += [f"token {r['token']} ({r['word']}): {r['reason']}" for r in dropped]  # reported with the others
    counts = {a: sum(1 for r in rows if r["decision"] == a) for a in ("fix", "keep", "skip")}  # decisions by kind
    kept = sum(1 for r in rows if r["reason"].startswith("pronunciations.json"))  # left to the hand and loop fixes
    totals = {"occurrences": len(rows), **counts, "kept_by_pronunciations": kept}  # the decisions file's counts
    # every occurrence with its decision, and what produced them
    _write(project, DECISIONS, {"schema": "speechkit_narrate/prevoice-decisions/1", "rules": mode, "tagger": tag_info,
                                "book_sha256": book_sha, "totals": totals,  # the book it was made for, and counts
                                "warnings": warns, "occurrences": rows})  # in narration order
    for w in warns:  # warnings go to the log too
        log(f"prevoice: warning: {w}")  # one line each
    # one summary line
    log(f"prevoice: {len(rows)} heteronym occurrences, {counts['fix']} fixed before voicing ({mode}, tagger "
        f"{tag_info}), {kept} left to pronunciations.json")  # the rule set and tagger that decided
    return {"enabled": True, "rules": mode, "tagger": tag_info, "occurrences": len(rows),  # what was judged, and how
            "fixes": counts["fix"], "kept": counts["keep"], "skipped": counts["skip"],  # the decisions by kind
            "kept_by_pronunciations": kept, "warnings": len(warns),  # left to pronunciations.json; warnings
            "script": script}  # the script counts include the fixes applied
