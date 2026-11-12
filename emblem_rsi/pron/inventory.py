# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Inventory of every spoken word occurrence in the narrated book.

One row per spoken token (the strings the voice was given), with its segment, sentence piece,
chapter/section heading, display text, normalized words and the time span from the segment's
<id>.wav.json. Plus a table of unique normalized words.

    python3 -I emblem_rsi/pron/inventory.py [--karaoke <project root>] [--out runs/pron-probe/lexicon/inventory.json]
"""
from __future__ import annotations  # postponed annotations, so `list[str]` hints work everywhere

import argparse  # command line options when run as a script
import hashlib  # SHA-256 of the input files, recorded so results can be tied to exact inputs
import json  # reads the request, docmodel and timing records, writes the inventory
import re  # splitting tokens into words and spotting numbers and sentence ends
import sys  # sys.exit passes main's status to the shell when run as a script
from pathlib import Path  # file paths that work on Windows and Linux
from typing import NamedTuple  # PieceWord, the record the shared word splitter returns

SCHEMA = "emblem-rsi/pron-inventory/1"  # format tag written into the inventory JSON
REPO = Path(__file__).resolve().parents[2]  # repository root, two folders above this file
# narration project root from EMBLEM_PRON_ROOT, else the current directory (same rule as paths.py)
DEFAULT_KARAOKE = Path(__import__("os").environ.get("EMBLEM_PRON_ROOT") or Path.cwd())
# default output file inside the repository's runs folder
DEFAULT_OUT = REPO / "runs" / "pron-probe" / "lexicon" / "inventory.json"
# block kinds that act as headings (not used below; kept for readers of the inventory)
HEADING_KINDS = ("cover", "h1", "h2")

# splits on whitespace, ASCII hyphen and the Unicode hyphens and dashes U+2010 to U+2015
_SPLIT = re.compile(r"[\s\-‐-―]+")
# a number: digits, optional thousands commas, optional decimals, optional ordinal suffix (any case)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?(?:st|nd|rd|th)?", re.I)
_EDGE = "\"'“”‘’()[]{},.;:!?…«»"  # quotes, brackets and punctuation stripped from the edges of each word


# SHA-256 of a whole file as hex. Parameter p is the path; the file is read into memory in one go.
def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()  # fingerprint recorded in the inventory's source block


# Split one spoken token into its surface words, keeping their original case.
# Parameter spoken is the token text (for example "twenty-one," or "“Hello"). Returns a list of strings.
# Hyphenated forms become separate words, and edge punctuation is removed from each piece.
def split_words(spoken: str) -> list[str]:
    """Surface sub-words of a spoken token: split on spaces/hyphens, edge punctuation stripped, case kept."""
    out = []  # surface words collected in spoken order
    for w in _SPLIT.split(spoken):  # break on spaces and every kind of hyphen or dash
        w = w.strip(_EDGE)  # trim quotes, brackets and punctuation from both ends
        if w:  # ignore pieces that were only punctuation
            out.append(w)  # keep the cleaned word with its original capitalisation
    return out  # surface words in spoken order


# Normalise one surface word into the key used for counting unique words.
# Parameter surface is a word from split_words. Returns lowercase letters and apostrophes, or for a number its
# digits with commas and any ordinal suffix kept ("1,200", "3rd"); returns "" when nothing word-like remains.
def norm_word(surface: str) -> str:
    """Lowercase letters and apostrophes; a number keeps its digits (and commas/ordinal suffix)."""
    s = surface.lower().replace("’", "'")  # lowercase and turn curly apostrophes into straight ones
    # does the whole thing, minus commas or full stops at either end, look like a number?
    m = _NUMBER.fullmatch(s.strip(".,"))
    if m and any(c.isdigit() for c in s):  # only treat it as a number if it really contains a digit
        return m.group(0).rstrip(",.")  # the number text (already stripped at both ends) without trailing punctuation
    # otherwise keep only letters and apostrophes, and drop apostrophes at the edges
    s = "".join(c for c in s if c.isalpha() or c == "'").strip("'")
    return s  # normalised word key, possibly empty for symbols


# Display text of a token with edge punctuation and spaces trimmed. Parameter disp is the docmodel display text.
# Returns the trimmed text, or the original when trimming would leave nothing (a token that is pure punctuation).
def clean_display(disp: str) -> str:
    return disp.strip(_EDGE + " ") or disp  # the `or` fallback keeps tokens such as "…" from becoming empty keys


# Group token positions into sentence pieces, breaking after a token that ends in . ! or ?, as the narration does.
# Parameter tokens is one segment's spoken token list. Returns a list of lists of positions (0-based, in order).
# A final token ending in punctuation does not open an empty trailing piece.
def sentence_pieces(tokens: list[str]) -> list[list[int]]:
    """Token positions per sentence piece, the same split the narration used (token ends in . ! ?)."""
    pieces = [[]]  # start with one empty piece that the first token fills
    for i, t in enumerate(tokens):  # walk the tokens with their positions
        pieces[-1].append(i)  # this token belongs to the current piece
        if re.search(r"[.!?]$", t) and i + 1 < len(tokens):  # sentence end, and more tokens follow
            pieces.append([])  # open a new piece for the next sentence
    return pieces  # list of position lists, one per sentence piece


# Split one spoken token into its words, the single rule that build(), token_words() and piece_words() share.
# ref is what a listener should hear (the docmodel's spoken_ref), sp what the voice was given (an override may have
# replaced the word with a respelling or a [[raw phoneme]] block). Returns (keep, voiced): keep lists (surface, key)
# pairs for the words whose key is not empty, voiced what the voice got for each of them. An overridden token gives
# its whole override to the first word and "" to the rest; voiced keeps one entry even when keep is empty.
def _token_split(ref: str, sp: str) -> tuple[list[tuple[str, str]], list[str]]:
    surf = split_words(ref)  # the reference words a listener should hear
    norms = [norm_word(w) for w in surf]  # normalised keys for counting unique words
    keep = [(s, n) for s, n in zip(surf, norms, strict=True) if n]  # drop pieces that normalise to nothing
    if ref == sp:  # the token was not overridden, so the voice said the reference words themselves
        voiced = [s for s, _ in keep]  # the voiced forms are the surface words
    else:   # overridden token: the voice got `sp` (a respelling or a [[raw phoneme]] block) for the word
        # the override's text without trailing punctuation or surrounding quotes
        core = re.sub(r"[,.;:!?…]+$", "", sp).strip("\"'“”‘’()")
        # attribute the whole respelling to the first word and nothing to the rest
        voiced = [core] + [""] * (len(keep) - 1)
    return keep, voiced  # the same two lists build() used to compute inline


# The words of one spoken token as (surface, key, voiced) triples, in spoken order. ref is the reference text (what a
# listener should hear) and spoken what the voice is given; None means the same text. A word the voice gets nothing
# for (the tail of an overridden token) has voiced "". Pure function.
def token_words(ref: str, spoken: str | None = None) -> list[tuple[str, str, str]]:
    """(surface, key, voiced) for every word of one spoken token; spoken defaults to ref."""
    keep, voiced = _token_split(ref, ref if spoken is None else spoken)  # the shared split rule
    # one triple per word that has a key (an overridden token with no such word still has one voiced entry)
    return [(s, n, v) for (s, n), v in zip(keep, voiced[:len(keep)], strict=True)]


# One word of a sentence piece, as every role check (the lexicon probe, the prevoice stage, the tagger) sees it.
class PieceWord(NamedTuple):
    token: int  # global token id the word belongs to
    sub: int  # position of the word inside its token (a hyphenated token holds several words)
    surface: str  # the word as a listener should hear it: case kept, edge punctuation removed
    norm: str  # its key (norm_word): lowercase, apostrophes kept, numbers keep their digits
    voiced: str  # what the voice is given for it: the word itself, an override's text, or "" for an override's tail
    punct_after: bool  # the token ends with , ; : . ! or ? right after this word: a clause boundary follows
    start: int  # offset of the surface word in the piece's reference text (the reference tokens joined by spaces)
    end: int  # offset just past the surface word in that text


# The shared word splitter: every word of one sentence piece with its token id, its position inside the token, its
# clause-break flag and its place in the reference text. tokens lists (token id, reference text) or (token id,
# reference text, spoken text) per token, in spoken order; the spoken text (what the voice is given) decides the
# voiced forms and the clause breaks, the reference text the words. The reference text of the piece is the reference
# texts joined by single spaces, which is what start and end index. Returns a list of PieceWord. Pure function.
def piece_words(tokens) -> list[PieceWord]:
    """Words of one sentence piece with token ids, clause-break flags and offsets (one splitter for every check)."""
    out = []  # the piece's words in spoken order
    base = 0  # offset of the current token's reference text in the joined piece text
    for item in tokens:  # each token of the piece
        tid, ref = item[0], item[1]  # its global id and reference text
        sp = item[2] if len(item) > 2 and item[2] is not None else ref  # what the voice gets (defaults to ref)
        words = token_words(ref, sp)  # its words, keys and voiced forms
        brk = bool(re.search(r"[,;:.!?]$", sp))  # the token ends a clause (the lexicon probe's rule)
        pos = 0  # search position inside this token's reference text
        for j, (s, n, v) in enumerate(words):  # each word of the token
            at = ref.find(s, pos)  # where the surface word sits in the token (split_words only trims the edges)
            at = pos if at < 0 else at  # never missing in practice; fall back to the search position
            pos = at + len(s)  # the next word is searched after this one
            # the last word of a token that ends in clause punctuation carries the break
            out.append(PieceWord(tid, j, s, n, v, brk and j == len(words) - 1, base + at, base + at + len(s)))
        base += len(ref) + 1  # the next token starts after this one and the joining space
    return out  # one PieceWord per word with a key


# Build the inventory of every spoken word occurrence in a narration project.
# Parameter karaoke_root is the project root holding work/request.json, work/docmodel.json and work/speech/.
# Returns a dict with source hashes, totals, one row per spoken token, the unique-word table and sentence texts.
# Reads files only; the lexicon probe consumes this to decide which words to compare with the CMU dictionary.
def build(karaoke_root) -> dict:
    root = Path(karaoke_root)  # accept either a string path or a Path object
    work = root / "work"  # the narration's working folder holding request, docmodel and speech
    # the request (what the voice was given) and the document model (what the reader sees)
    req_p, doc_p = work / "request.json", work / "docmodel.json"
    req = json.loads(req_p.read_text(encoding="utf-8"))  # segments with spoken tokens and their global token ids
    # tokens with display text, block index and optional spoken_ref, plus the block list
    doc = json.loads(doc_p.read_text(encoding="utf-8"))
    # document tokens (indexed by their id) and blocks (cover, h1, h2, paragraphs, tables)
    dtoks, blocks = doc["tokens"], doc["blocks"]

    # chapter / section for every document token, walking blocks in document order
    head_of = {}  # chapter and section headings in force at each token id
    h1 = h2 = cover = None  # latest h1, h2 and cover text seen so far
    last_block = None  # block of the previous token, so headings are only updated when a new block starts
    for t in dtoks:  # every document token in reading order
        b = t["block"]  # index of the block (paragraph, heading, cell) holding this token
        if b != last_block:  # the token starts a block that has not been examined yet
            kind = blocks[b]["kind"]  # block kind such as cover, h1, h2, paragraph or table
            if kind == "h1":  # a chapter heading block (top-level heading)
                h1, h2 = blocks[b].get("text"), None  # new chapter text, and the section resets
            elif kind == "h2":  # a section heading block inside the current chapter
                h2 = blocks[b].get("text")  # new section text replaces the previous section heading
            elif kind == "cover":  # the cover block, which carries the book title
                cover = blocks[b].get("text")  # its text serves as the heading before any chapter
            last_block = b  # remember that this block's heading has been handled
        # chapter falls back to the section and then the cover; section may be None
        head_of[t["i"]] = (h1 or h2 or cover, h2)

    first_in_cell = set()  # token ids that open a block or a table cell
    for b in blocks:  # examine every block of the document model
        # table blocks list tokens per cell; other blocks are treated as one cell holding all their tokens
        for cell in b.get("cell_tokens") or [b.get("tokens", [])]:
            if cell:  # skip empty table cells that hold no tokens
                first_in_cell.add(cell[0])  # record the cell's first token id as a block opener

    rows, sentences, unique = [], {}, {}  # per-token rows, sentence texts per segment, and the unique-word table
    missing_time = 0  # tokens with no timing in their segment's record
    for seg in req["segments"]:  # every narrated segment listed in the request
        sid = seg["id"]  # segment id, which also names its WAV and timing files
        # the voice's timing record for this segment (the file must exist)
        rec = json.loads((work / "speech" / f"{sid}.wav.json").read_text(encoding="utf-8"))
        # token id -> (start, end) seconds within the segment WAV
        times = {w["token"]: (w.get("start"), w.get("end")) for w in rec.get("words", [])}
        toks, tidx = seg["tokens"], seg["tokenIndex"]  # spoken token strings and their global ids
        pieces = sentence_pieces(toks)  # the segment split into sentence pieces
        sentences[sid] = [" ".join(toks[i] for i in p) for p in pieces]  # the text of each piece, joined with spaces
        where = {}  # position -> (piece index, position within the piece)
        for si, p in enumerate(pieces):  # walk the sentence pieces with their index numbers
            for pos, i in enumerate(p):  # and the token positions inside each piece
                where[i] = (si, pos)  # remember the piece and offset where each position falls
        for i, (sp, ti) in enumerate(zip(toks, tidx)):  # every spoken token with its segment position and global id
            dt = dtoks[ti]  # the matching document token, indexed directly by global id
            start, end = times.get(ti, (None, None))  # timing, or (None, None) when the voice did not record this token
            if start is None:  # no start time means the word cannot be found in the audio
                missing_time += 1  # count the untimed token for the summary totals
            ref = dt.get("spoken_ref", sp) or sp     # what a listener should hear (before pronunciation overrides)
            # (surface word, key) pairs a listener should hear, and what the voice was given for each of them
            keep, voiced = _token_split(ref, sp)
            chapter, section = head_of.get(ti, (None, None))  # chapter and section headings in force at this token
            si, pos = where[i]  # sentence piece and position within it
            # One inventory row describing a spoken token.
            row = {
                # token id, segment, sentence piece and position inside it
                "token": ti, "seg": sid, "sent": si, "pos": pos,
                # headings and the block this token belongs to
                "chapter": chapter, "section": section, "block": dt["block"],
                # block kind and whether the token opens its block or cell
                "block_kind": blocks[dt["block"]]["kind"], "first_in_block": ti in first_in_cell,
                # what the reader sees, what the voice was given, and what a listener should hear
                "display": dt["disp"], "spoken": sp, "ref": ref,
                # surface words, their keys and the forms actually voiced
                "words": [s for s, _ in keep], "norm": [n for _, n in keep], "voiced": voiced,
                "start": start, "end": end,  # seconds within the segment's WAV, or None
            }
            rows.append(row)  # collect the finished row for the inventory
            disp = clean_display(dt["disp"])  # cleaned display text, used for the unique-word table
            for (s, n), vs in zip(keep, voiced):  # each kept word with what was voiced for it
                s = vs or s  # prefer the voiced form (a respelling) as the surface text
                # per-word record, created on first sight
                u = unique.setdefault(n, {"count": 0, "tokens": [], "displays": {}, "surface": {}})
                u["count"] += 1  # count one more occurrence of this unique word
                # record each token id once, even when a word repeats inside a token
                if not u["tokens"] or u["tokens"][-1] != ti:
                    u["tokens"].append(ti)  # remember the token id where the word occurs
                u["displays"][disp] = u["displays"].get(disp, 0) + 1  # how often each display form appears
                u["surface"][s] = u["surface"].get(s, 0) + 1  # how often each voiced surface form appears

    occurrences = sum(len(r["norm"]) for r in rows)  # total word occurrences across all rows
    # The finished inventory: source files with their hashes, notes on the fields, totals, rows, unique words
    # and sentence texts.
    return {
        "schema": SCHEMA,  # format tag identifying the inventory schema version
        # request file path and its SHA-256 fingerprint
        "source": {"request": str(req_p), "request_sha256": _sha(req_p),
                   "docmodel": str(doc_p), "docmodel_sha256": _sha(doc_p),  # document model path and hash
                   "speech_dir": str(work / "speech")},  # folder with the WAVs and timing records
        # Human-readable description of the fields, stored inside the JSON for anyone opening the file.
        # explains rows and how the norm keys were built (from `ref`, what a listener should hear)
        "notes": ("rows: one per spoken token; norm = lowercase letters/apostrophes of each space/hyphen-separated "
                  # explains how numbers and time spans are represented
                  "word of `ref`, the reading before pronunciation overrides (numbers keep their digits); start/end are seconds within that segment's WAV; "
                  # explains the sent and pos sentence-piece fields
                  "sent/pos = sentence piece index within the segment and token position within that piece "
                  # explains how sentence pieces were split from tokens
                  "(pieces split after tokens ending in . ! ? exactly as the narration did); sentences[seg] holds the piece texts."),
        # segment, token, occurrence and unique counts, plus untimed tokens
        "totals": {"segments": len(req["segments"]), "spoken_tokens": len(rows), "occurrences": occurrences,
                   "unique": len(unique), "tokens_without_time": missing_time},  # number of tokens without timing
        "rows": rows,  # one row per spoken token
        "unique": dict(sorted(unique.items())),  # unique words sorted alphabetically
        "sentences": sentences,  # sentence piece texts per segment
    }


# Write the inventory as indented UTF-8 JSON, replacing any old file atomically.
# Parameters: inv is the dict from build(); out is the destination path. Returns the path written.
def write(inv: dict, out: Path) -> Path:
    out = Path(out)  # accept a plain string path from the caller
    out.parent.mkdir(parents=True, exist_ok=True)  # create the output folder if needed
    tmp = out.with_suffix(out.suffix + ".tmp")  # temporary file beside the target, e.g. inventory.json.tmp
    # write everything to the temporary file first
    tmp.write_text(json.dumps(inv, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(out)  # rename over the target, so readers never see a half-written file
    return out  # final path of the written inventory file


# Script entry point: build the inventory for a project and write it to disk.
# Parameter argv is an optional argument list (None means sys.argv). Returns 0. Prints the output path and totals.
def main(argv=None) -> int:
    # the first docstring line becomes the help description
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--karaoke", default=str(DEFAULT_KARAOKE))  # narration project root holding the work folder
    ap.add_argument("--out", default=str(DEFAULT_OUT))  # destination path of the inventory JSON file
    a = ap.parse_args(argv)  # parse the command line into an options namespace
    inv = build(a.karaoke)  # collect every spoken word occurrence
    out = write(inv, Path(a.out))  # save the inventory atomically to the chosen path
    t = inv["totals"]  # totals block used for the printed summary
    print(f"wrote {out}")  # report where the inventory file was written
    # token, segment, occurrence and unique-word counts
    print(f"  {t['spoken_tokens']} spoken tokens in {t['segments']} segments; {t['occurrences']} word occurrences; "
          # plus how many tokens had no timing
          f"{t['unique']} unique words; {t['tokens_without_time']} tokens without timing")
    return 0  # exit status 0 once the inventory has been written


if __name__ == "__main__":  # run main only when executed as a script, not when imported by the lexicon probe
    sys.exit(main())  # exit the interpreter with main's return value
