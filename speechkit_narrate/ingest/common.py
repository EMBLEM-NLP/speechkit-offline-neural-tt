# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Small shared helpers for the extraction stage: colours, dictionary lookups, tokens."""
# Postpone evaluation of annotations so "int | None" works as a hint on every supported Python 3 version.
from __future__ import annotations

import colorsys  # standard library RGB/HLS conversions, used by hsl() and hue_sat()
import functools  # lru_cache memoises the dictionary and frequency lookups
import re  # regular expressions for token splitting and key normalisation

# ---------------------------------------------------------------- colours

# Convert a colour as PyMuPDF reports it into one 24-bit integer 0xRRGGBB, the form used everywhere in extraction.
# c is an int already packed as 0xRRGGBB, or a sequence of 0..1 floats with 1 (grey), 3 (RGB) or 4 (CMYK) items.
# Returns the packed int, or None when the colour is absent or has an unexpected number of components.
# Integers make colours cheap to compare, count in a Counter and use as dict keys.
def rgb_int(c) -> int | None:
    """PyMuPDF colour (int 0xRRGGBB, or a tuple of 0..1 floats) -> int, None when absent."""
    if c is None:  # no colour at all (for example a path with no fill)
        return None  # keep "absent" distinct from black (0)
    if isinstance(c, int):  # span colours from get_text() already arrive packed as an int
        return c  # already packed as 0xRRGGBB, so it is returned unchanged
    c = tuple(c)  # drawing colours arrive as tuples or lists; normalise to a tuple
    if len(c) == 1:  # one component means a grey level
        c = (c[0], c[0], c[0])  # repeat it into equal R, G and B
    # Four components are CMYK; convert with the simple formula channel = (1 - ink) * (1 - black).
    if len(c) == 4:                       # CMYK
        C, M, Y, K = c  # cyan, magenta, yellow and black, each 0..1
        # approximate RGB fractions; good enough for theme colours, not colour-managed
        c = ((1 - C) * (1 - K), (1 - M) * (1 - K), (1 - Y) * (1 - K))
    if len(c) != 3:  # any other component count (for example 2 or 5) is not a colour we understand
        return None  # treat it as absent rather than guess
    # scale each 0..1 fraction to 0..255 and clamp, guarding against values slightly outside the range
    r, g, b = (max(0, min(255, round(v * 255))) for v in c)
    return (r << 16) | (g << 8) | b  # pack red into bits 16-23, green into 8-15 and blue into 0-7


# Format a packed colour as a CSS hex string such as "#2a241c" for theme.json; None passes through unchanged.
def hexc(c: int | None) -> str | None:
    # 06x pads to six lowercase hex digits, so dark blues keep their leading zeros
    return None if c is None else f"#{c:06x}"


# Unpack a 0xRRGGBB integer into its (red, green, blue) channels, each 0..255.
def rgb(c: int) -> tuple[int, int, int]:
    return (c >> 16) & 255, (c >> 8) & 255, c & 255  # shift each byte down and mask off the other channels


# Perceived brightness of a packed colour on a 0 (black) .. 255 (white) scale.
# The extractor uses it to tell dark ink from light fills and watermarks; None counts as black (0.0).
def lum(c: int | None) -> float:
    """Perceived luminance 0..255 (Rec. 601, like most binarisers)."""
    if c is None:  # no colour was recorded for this span or drawing
        return 0.0  # treat as black ink, the usual default for text
    r, g, b = rgb(c)  # unpack the 24-bit colour into red, green and blue values from 0 to 255
    return 0.299 * r + 0.587 * g + 0.114 * b  # Rec. 601 luma weights: green looks brightest to the eye, blue darkest


# Euclidean distance between two packed colours in RGB space (0 for identical, about 441 for black versus white).
# Used to decide whether two colours are "the same" (a few units apart) or clearly different (40 or more).
def cdist(a: int | None, b: int | None) -> float:
    if a is None or b is None:  # a missing colour cannot match anything
        return 999.0  # a distance larger than any real one, so comparisons treat it as far away
    # square root of the summed squared channel differences
    return sum((x - y) ** 2 for x, y in zip(rgb(a), rgb(b))) ** 0.5


# Build a CSS hex colour from hue, saturation and lightness.
# h is in degrees (any value; it is wrapped to 0..360), s and l are fractions 0..1. style.py uses it to derive
# the highlight, pill and underline colours from the accent's hue.
def hsl(h: float, s: float, l: float) -> str:
    # colorsys takes hue as a 0..1 fraction and expects the order hue, lightness, saturation
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, l, s)
    # format each 0..1 channel as two hex digits
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


# The inverse of hsl(): returns (hue in degrees 0..360, saturation 0..1, lightness 0..1) for a packed colour.
def hue_sat(c: int) -> tuple[float, float, float]:
    r, g, b = (v / 255 for v in rgb(c))  # channels as 0..1 fractions
    h, l, s = colorsys.rgb_to_hls(r, g, b)  # colorsys returns hue, lightness, saturation in that order
    return h * 360, s, l  # reorder to (hue degrees, saturation, lightness) for the caller


# ---------------------------------------------------------------- words

# Quote characters (curly, straight, guillemets, low-9). Neither this nor DASHES is imported anywhere today.
QUOTE_CHARS = "“”‘’\"'«»„"  # double and single, opening and closing, plus « » and „
DASHES = "—–"  # em dash and en dash
# Closed-class English words (articles, prepositions, pronouns, auxiliaries, conjunctions). A glued token
# whose parts are such words ("ofthe", "bya") is almost certainly two words; clean.py's glue splitter relies on it.
FUNCTION_WORDS = set("""a an the of to in on at by for from with as and or but nor so if is it its be am are was were
been has have had do does did not no my your his her our their this that these those there here what who whom which
when where why how than then too very can could will would shall should may might must i me we us you he she they
them him one two all any each both few more most some such only own same just over into onto upon out up down off
about after before again once under until while myself yourself himself herself itself ourselves themselves
yourselves without within through across between against among around behind beyond during since though although
because whether unless toward towards near via every another other many much several either neither""".split())  # words


# The CMU Pronouncing Dictionary word list (about 130,000 lowercase English words), loaded once and cached.
# It is the "is this a real word" reference for known(); an empty set is returned when cmudict is not installed.
@functools.lru_cache(maxsize=1)
def cmu() -> frozenset:  # takes no arguments, so the maxsize=1 cache above computes the word set once
    try:  # cmudict is an optional dependency
        import cmudict  # imported here so the module still loads without it
        # keep only the words; a frozenset gives fast membership tests and can be shared safely
        return frozenset(cmudict.dict().keys())
    # Missing package or unreadable data: carry on without a dictionary.
    except Exception:  # noqa: BLE001 - optional
        return frozenset()  # callers then fall back to word frequencies


# Return a function that gives a word's Zipf frequency from the wordfreq package, or None when it is missing.
# Cached so the import is attempted only once.
@functools.lru_cache(maxsize=1)
def _zipf_fn():  # private helper behind zipf() and have_wordfreq()
    try:  # wordfreq is optional too
        from wordfreq import zipf_frequency  # zipf_frequency(word, lang) returns 0..8 on the Zipf scale
        return lambda w: zipf_frequency(w, "en")  # fixed to English, the language the dictionary checks assume
    # Not installed (or broken install): report that no frequency list is available.
    except Exception:  # noqa: BLE001 - optional dependency
        return None  # None tells zipf() to use the CMUdict fallback


# How common a word is, on the Zipf scale: log10 of occurrences per billion words (about 7 for "the", 3 for an
# ordinary word, 0 for unknown). w is any string; case and curly apostrophes are normalised first.
# Results are cached for up to 200,000 distinct words because the same tokens are scored many times.
@functools.lru_cache(maxsize=200000)
def zipf(w: str) -> float:  # cached per distinct word string
    # When wordfreq is missing, a word in CMUdict scores a neutral 3.5 and anything else 0.
    """Word frequency on the Zipf scale (0 unknown .. 7+ very common). Falls back to CMUdict presence."""
    w = w.lower().replace("’", "'")  # wordfreq lists lowercase words with straight apostrophes
    f = _zipf_fn()  # the cached lookup function, or None
    if f is not None:  # wordfreq imported successfully, so a true frequency lookup is possible
        return f(w)  # the measured Zipf value from wordfreq's English list, 0 when the word is unseen
    # fallback: dictionary presence only, with 3.5 chosen to pass the "common enough" thresholds used elsewhere
    return 3.5 if w in cmu() else 0.0


# True when real word frequencies are available; the glued-word splitter refuses to guess without them.
def have_wordfreq() -> bool:
    return _zipf_fn() is not None  # the cached function is None when wordfreq could not be imported


# Split a token into (leading punctuation, word, trailing punctuation), for example "“Hello,”" gives
# ("“", "Hello", ",”"). Used throughout extraction to look a word up without its quotes and commas.
def core(tok: str) -> tuple[str, str, str]:
    """Split a token into (leading punctuation, word, trailing punctuation)."""
    # leading non-word characters, the shortest middle, trailing non-word characters; re.S lets "." match newlines
    m = re.match(r"^([^\w]*)(.*?)([^\w]*)$", tok, re.S)
    # the regex always matches (every part may be empty), so the groups are safe to read
    return m.group(1), m.group(2), m.group(3)


# Does a dictionary know this word? word is a bare token (use core() to strip punctuation first).
# Tolerates case, curly apostrophes, possessives ("God's", "parents'") and hyphenated compounds whose parts are
# all known. An empty string counts as known so punctuation-only tokens are never flagged. Cached per word.
@functools.lru_cache(maxsize=200000)
def known(word: str) -> bool:  # recursive for hyphenated parts, so the cache also serves the pieces
    """A word a dictionary knows (CMUdict, possessive/plural tolerant, hyphenated parts)."""
    w = word.lower().replace("’", "'")  # normalise case and apostrophe style to match CMUdict entries
    if not w:  # nothing left after stripping punctuation
        return True  # never report an empty word as unknown
    d = cmu()  # the cached CMUdict word set (possibly empty)
    if w in d:  # the lowercased word appears exactly as written among the CMUdict entries
        return True  # known as written, so the hyphen and possessive checks below are skipped
    if "-" in w:  # hyphenated compound such as "well-known"
        # every non-empty part must be known on its own (recursive call)
        return all(known(p) for p in w.split("-") if p)
    for suf in ("'s", "s'", "'"):  # possessive and plural-possessive endings
        if w.endswith(suf) and w[: -len(suf)] in d:  # strip the ending and look the stem up
            return True  # "Moses'" or "nation's" with a known stem
    if not d:  # no CMUdict installed at all
        return zipf(w) >= 2.0  # fall back to frequency: Zipf 2.0 or more counts as a real word
    return False  # CMUdict is loaded and no form of the word matched, so it is reported as unknown


# Does this token look like a number rather than a word? Covers digit strings with separators ("3:16", "1,000"),
# ordinals ("21st", "1990s") and Roman numerals in one case. Note that [ivxlcdm]+ also matches some ordinary
# words made only of those letters ("mild", "civil", "vivid"), which the spell check then skips.
def is_numberish(word: str) -> bool:
    # fullmatch so the whole token must fit one of the alternatives
    return bool(re.fullmatch(r"[\d.,:/\-–]+|(\d+(st|nd|rd|th|s))|[ivxlcdm]+|[IVXLCDM]+", word))


# Comparison key for spotting repeated lines (running headers, footers): case, spaces and punctuation are ignored
# and every run of digits becomes "#", so "Chapter 3 - Page 41" and "Chapter 4 - Page 57" share one key.
def norm_key(s: str) -> str:
    """Comparison key: lowercase letters/digits only, digits collapsed to '#'."""
    s = re.sub(r"\d+", "#", s.lower())  # collapse digit runs first, on the lowercased text
    return re.sub(r"[^a-z#]", "", s)  # then keep only letters and the "#" placeholders


# Comparison key that keeps word boundaries: lowercase letters, digits and single spaces only.
# structure.py compares title-page lines with the project title using it. Accented letters are dropped too.
def words_key(s: str) -> str:
    # both apostrophe styles end up removed by the character filter; strip() trims the ends
    return re.sub(r"[^a-z0-9 ]", "", s.lower().replace("’", "'")).strip()
