# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Optional part-of-speech tagger for heteronym roles: spaCy's small English model, when it is installed.

Nothing here is required. Without spaCy, or without its en_core_web_sm model, available() is False, tags() gives
None for every word, and callers fall back to the rule sets in lexicon.explain_role() (rules only). The model is
loaded once per process, on first use, never at import.

    pip install spacy
    python -m spacy download en_core_web_sm

Tag to role: VB* is the verb reading, NN* and JJ* the noun or adjective reading, anything else no opinion. For
"read", VBD and VBN are the past reading (red) and every other tag the present one (reed).
"""
# Store type hints as unevaluated strings, so `str | None` works on every supported Python.
from __future__ import annotations

from functools import lru_cache  # loads the model once per process and keeps it

MODEL = "en_core_web_sm"  # the spaCy pipeline used: small, offline, English


# Loads spaCy's English pipeline the first time it is needed and keeps it for the rest of the process. Returns the
# pipeline, or None when spaCy is not installed (ImportError) or its model is missing (OSError). The cache also keeps
# a None, so a machine without spaCy pays for the failed import once.
@lru_cache(maxsize=1)
def _load():  # the cache makes this run once per process
    try:  # spaCy is an optional dependency
        import spacy  # imported here, never at module import, so the toolkit works without it
    except ImportError:  # not installed: rules only
        return None  # remembered by the cache
    try:  # the package can be there without its downloaded model
        return spacy.load(MODEL)  # the full small pipeline, the way the earlier prototype loaded it
    except OSError:  # spacy.load raises OSError for a model that is not installed
        return None  # treated exactly like a missing spaCy


# True when the tagger can be used here (spaCy and its model both load). Loads the model on the first call.
def available() -> bool:
    return _load() is not None  # the cached pipeline, or None


# A one-line description of the tagger for logs and decision files, e.g. "spaCy 3.8.18 en_core_web_sm 3.8.0", or
# "not installed (rules only)" when it cannot be loaded.
def describe() -> str:
    nlp = _load()  # the cached pipeline, or None
    if nlp is None:  # nothing to describe
        return "not installed (rules only)"  # the fallback every caller uses
    import spacy  # already imported by _load, so this only binds the name for its version
    return f"spaCy {spacy.__version__} {MODEL} {nlp.meta.get('version', '')}".strip()  # library, model, model version


# Penn Treebank tags for several words of one text. text is a sentence piece (reference words joined by spaces) and
# spans its words as (start, end) character offsets into it. Each word gets the tag of the first spaCy token that
# overlaps it (spaCy splits "don't" into two tokens), or None when no token overlaps or the tagger is unavailable.
# Returns a list as long as spans.
def tags(text: str, spans) -> list:
    nlp = _load()  # the cached pipeline, or None
    if nlp is None:  # rules only: no tag for any word
        return [None] * len(spans)  # same length, so callers can zip without checks
    doc = nlp(text)  # tag the whole piece once, so every word is tagged in its sentence
    out = []  # one tag per span, in order
    for start, end in spans:  # each word's place in the text
        # the first spaCy token whose characters overlap the word's characters
        hit = next((t for t in doc if t.idx < end and t.idx + len(t.text) > start), None)
        out.append(hit.tag_ if hit is not None else None)  # its fine-grained tag (VB, VBD, NN, JJ, ...)
    return out  # tags in span order


# The heteronym role a Penn tag stands for. tag is a Penn Treebank tag or None, word the heteronym. Returns "verb",
# "noun-adj", "past" or "present" (for "read" only), or None when the tag says nothing about the reading.
def role(tag, word: str):
    if not tag:  # no tag (no tagger, or nothing overlapped the word)
        return None  # no opinion
    if word == "read":  # the tense heteronym: red for the past and the participle, reed otherwise
        return "past" if tag in ("VBD", "VBN") else "present"  # VBD past tense, VBN past participle
    if tag.startswith("VB"):  # VB, VBD, VBG, VBN, VBP, VBZ: some form of the verb
        return "verb"  # the verb reading
    if tag.startswith(("NN", "JJ")):  # nouns and adjectives (NN, NNS, NNP, JJ, JJR, JJS)
        return "noun-adj"  # the noun or adjective reading
    return None  # adverbs, prepositions and the rest decide nothing
