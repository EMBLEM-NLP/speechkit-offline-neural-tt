# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Word audit: the voice's own espeak phonemes for each fixed word, against the fix.

For every word -> fix pair (pronunciations.json "overrides", or per-token fixes given as word -> fix) one row:
  word             the word as written in the fixes
  raw              espeak "en" (the narration voice) for the word on its own
  fix              the phonemes the fix asks for: a [[...]] block as written, a respelling through espeak "en"
  how              "raw phonemes" or "respelling '<text>'"
  same             the fix equals the raw reading (the fix changes nothing)
  symbols_missing  fix symbols that have no id in the voice's phoneme map (the voice cannot say them)
  kind             "r-less vowel the voice colours with an r" (the fix removes such a vowel), "stress" (only the
                   stress marks differ) or "letter-to-sound"
  en_us            espeak "en-us" for the word, for reference only

Phonemes come from lexicon.espeak(), the one adapter to Piper's espeak bridge (a helper subprocess in the speechkit
runtime). audit() writes nothing unless it is given an explicit output path.
"""
# Store type hints as unevaluated strings, so `str | None` works on every supported Python.
from __future__ import annotations

import json  # reads the voice config's phoneme map
import re  # recognises [[...]] raw phoneme blocks
from functools import partial  # binds the runtime python into the default phonemizer
from pathlib import Path  # the voice config and the output file

from . import lexicon  # espeak() (the helper adapter) and write_json() (atomic UTF-8 output)

# RP vowels that the US-recorded voice learned mostly before a lost r, so it tends to add one ("Ana" heard as "Arna").
RHOTIC_RISK = ("ɑː", "ɔː", "ɜː", "ɪə", "eə", "ʊə")
RAW = re.compile(r"\[\[(.*)\]\]")  # a Piper raw phoneme block; group 1 is the phoneme text inside
KIND_R = "r-less vowel the voice colours with an r"  # the kind of a fix that removes such a vowel


# The phoneme symbols a Piper voice can say: the keys of the phoneme_id_map in its config JSON (one codepoint each).
# config is the path of the voice's .onnx.json file, read as UTF-8. Returns a set of str.
def voice_symbols(config) -> set:
    cfg = json.loads(Path(config).read_text(encoding="utf-8"))  # UTF-8 explicitly: IPA breaks Windows' default
    return set(cfg["phoneme_id_map"])  # every symbol the voice has an id for


# A phoneme string without stress marks, for telling a stress-only fix from a sound change.
def _unstressed(ph: str) -> str:
    return ph.replace("ˈ", "").replace("ˌ", "")  # primary and secondary stress marks removed


# Audits word -> fix pairs against the voice. fixes maps a word to its fix (a respelling or a [[raw phoneme]] block),
# or is a list of (word, fix) pairs when one word has several fixes (per-token fixes); symbols is the voice's symbol
# set (voice_symbols()), phonemize(texts, voices) returns {voice: [phonemes per text]} (default: lexicon.espeak with
# runtime, one helper call), and out is the only place anything is written (None writes nothing). Returns one row per
# pair, in the order given (see the module docstring for the fields).
def audit(fixes, symbols, phonemize=None, runtime=None, out=None) -> list[dict]:
    """Rows comparing each word's raw espeak phonemes with its fix; written to out only when out is given."""
    # the default adapter: Piper's espeak bridge through the helper subprocess, run by the given runtime python
    phonemize = phonemize or partial(lexicon.espeak, runtime=runtime)
    pairs = list(fixes.items()) if isinstance(fixes, dict) else [tuple(p) for p in fixes]  # (word, fix) pairs
    respell = [f for _, f in pairs if not RAW.fullmatch(f)]  # respellings must be phonemized too
    texts = sorted({w for w, _ in pairs} | set(respell))  # the words and the respellings, each once
    ph = phonemize(texts, (lexicon.NARRATION_VOICE, lexicon.REFERENCE_VOICE)) if texts else {}  # one call for all
    en = dict(zip(texts, ph.get(lexicon.NARRATION_VOICE, []), strict=True))  # text -> phonemes in the narration voice
    us = dict(zip(texts, ph.get(lexicon.REFERENCE_VOICE, []), strict=True))  # text -> phonemes in the reference voice
    rows = []  # one row per pair, in the order given
    for word, fix in pairs:  # each fixed word with its fix
        raw = en.get(word, "")  # what the voice says for the word without the fix
        m = RAW.fullmatch(fix)  # a raw phoneme block?
        tgt, how = (m.group(1), "raw phonemes") if m else (en.get(fix, ""), f"respelling '{fix}'")  # the fix's phonemes
        bad = sorted({c for c in tgt if c not in symbols and not c.isspace()})  # symbols the voice has no id for
        risk = [v for v in RHOTIC_RISK if v in raw] if "r" not in word else []  # r-prone vowels in an r-less word
        # the fix took an r-prone vowel out, only moved the stress, or changed the sounds
        kind = (KIND_R if risk and not any(v in tgt for v in risk)
                else "stress" if _unstressed(raw) == _unstressed(tgt) else "letter-to-sound")  # the other two kinds
        # the row, with the reference voice's reading for comparison
        rows.append({"word": word, "raw": raw, "fix": tgt, "how": how, "same": raw == tgt, "symbols_missing": bad,
                     "kind": kind, "en_us": us.get(word, "")})  # en_us is shown for reference only
    if out is not None:  # an explicit destination was given
        lexicon.write_json(rows, Path(out))  # atomic UTF-8 JSON; nothing is ever written to the current folder
    return rows  # the rows, whether or not they were written


# The audit rows that need a person's attention, as short warning texts: a fix with symbols the voice lacks, or a
# fix that changes nothing. rows come from audit(). Returns a list of str, empty when every fix is fine.
def warnings(rows: list[dict]) -> list[str]:
    out = []  # warning texts in row order
    for r in rows:  # each audited word
        if r["symbols_missing"]:  # the voice has no id for some symbol of the fix
            # the voice would skip or garble those symbols
            out.append(f"fix for '{r['word']}' uses symbols the voice does not have: {' '.join(r['symbols_missing'])}")
        if r["same"]:  # espeak already says exactly what the fix asks for
            out.append(f"fix for '{r['word']}' changes nothing: espeak already says {r['raw']}")  # a no-op fix
    return out  # empty when nothing needs attention
