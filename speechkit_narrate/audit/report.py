# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""The list of mispronounced words, before and after: work/rsi/pron/report/Pronunciation_Audit.html

Built entirely from the loop's own records (first and last measurement, pronunciations.json, autofix and
retake results), so it is the same for every book. Categories:
  heard       both recognizers misheard the word in most of its occurrences
  r           an r that is not in the spelling (F3 dip)
  context     a heteronym read the wrong way for its sentence
  dictionary  wrong stress or reading against the CMU dictionary, or a dropped apostrophe
"""
# Type hints stay unevaluated strings, so "Path | None" style hints never run at import.
from __future__ import annotations

import html  # escapes words, sentences and the title for HTML
import json  # reads measurements and probe outputs; serializes the page data
import re  # removes the trailing zeros of a percentage with the same pattern the page's script uses
from collections import Counter, defaultdict  # Counter tallies chapters; defaultdict groups heteronym occurrences
from decimal import ROUND_HALF_UP, Decimal  # exact rounding of a percentage, the way JavaScript's toFixed rounds
from pathlib import Path  # Path objects for the run folder, templates and output

from . import configure, status  # configure the audit modules, and read the loop history

# the page template shipped with the package: speechkit_narrate/audit/template/report.html
TEMPLATE = Path(__file__).with_name("template") / "report.html"

# ---- The plain copy of the page, for viewers that run no scripts (the iPhone and iPad Files preview, for one). The
# page's script builds the intro, the tiles, the word list, the loop cards and the credits line from the embedded data.
# Without a script they would stay empty, so report.py writes the same content into the template's marks as plain HTML,
# and the script writes it again over that when it runs. The functions below follow the script line by line; the
# tests compare what a browser shows with scripts off and on.

# Category and status names as the script shows them (CAT and ST in the template); the plain copy must use the same.
CAT = {"heard": "Heard wrong", "r": "Added r", "context": "Wrong reading in context",
       "dictionary": "Stress / dictionary", "kept": "Checked, kept"}  # category key -> the label under the word
ST = {"fixed": "Fixed", "better": "Improved", "listen": "Listen", "kept": "Kept"}  # status key -> the badge text
# The header row of the word list. The page's style sheet hides it on narrow screens, where each cell labels itself.
HEAD = ('<div class="row head" role="row"><div>Word</div><div>What was wrong</div><div>Cause</div><div>Fix</div>'
        '<div>Result</div><div>Status</div></div>')
# The credits line at the bottom of the page, the same text as the script writes.
CREDITS = ("Voice: Piper en_US-ljspeech-high via speechkit-offline-neural-tt. Recognizers: sherpa-onnx NeMo CTC "
           "conformer (small) and Parakeet TDT 0.6B v2, both offline. Dictionary: CMUdict. "
           "Formants: Praat (parselmouth).")


# A value as the script prints it inside a template string. A whole number stored as a float loses its ".0" (12.0
# prints as 12) and None prints as null. Numbers that JavaScript writes with an exponent do not occur in this data.
def _js(v) -> str:
    if isinstance(v, float) and v.is_integer():  # JSON may hold 12.0 where the script sees the number 12
        return str(int(v))  # printed without the decimal point
    return "null" if v is None else str(v)  # null for a missing value, else the usual text


# A fraction as a percentage with up to two decimals and no trailing zeros, as the script's pct() writes it: 0.0258
# gives "2.58%" and 0.5 gives "50%". Decimal rounds a half upward, as JavaScript's toFixed does; Python's own
# formatting rounds a half to the even digit, which differs for exact binary values such as 0.125. x may be None.
def _pct(x) -> str:
    n = Decimal(100 * (x or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)  # the exact 100 * x, to two decimals
    return re.sub(r"\.?0+$", "", str(n)) + "%"  # trailing zeros and a bare point removed, then the percent sign


# A whole number with thousands separators, as the script's fmt() writes it (15441 gives "15,441"); None gives "-".
def _fmt(n) -> str:
    return "-" if n is None else f"{n:,}"  # the comma format matches toLocaleString("en-US") for whole numbers


# One row of the word list as HTML, character for character what list() in the template's script draws. The values
# are inserted as they are, because build() has already escaped them where that was needed. r is one report row.
def _row(r: dict) -> str:
    where = " &middot; " + "; ".join(r["where"][:2]) if r["where"] else ""  # the first two chapters, when there are any
    ipa = f' <span class="ipa">/{r["fix_ipa"]}/</span>' if r.get("fix_ipa") else ""  # the fix's phonemes, if it has any
    note = f'<div class="note">{r["note"]}</div>' if r.get("note") else ""  # the extra note under the cause, if any
    times = f'{_js(r["n"])} time{"" if r["n"] == 1 else "s"}'  # "1 time", "2 times": plural unless the count is one
    # six cells, each on its own line and indented like the script's template string
    return (f'<div class="row" role="row">\n'
            # cell 1: the word, how often it occurs and in which chapters
            f'    <div><div class="word">{r["word"]}</div><div class="sub">{times}{where}</div>'
            f'<span class="cat">{CAT[r["cat"]]}</span></div>\n'  # the category label, which closes cell 1
            f'    <div data-l="What was wrong"><p>{r["problem"]}</p></div>\n'  # cell 2: what was wrong with the word
            # cell 3: the cause, with the extra note when there is one
            f'    <div data-l="Cause"><p>{r["cause"]}</p>{note}</div>\n'
            # cell 4: the fix, with its phonemes when it has any
            f'    <div data-l="Fix"><p>{r["fix"]}{ipa}</p></div>\n'
            # cell 5: what was heard before the fix
            f'    <div data-l="Result" class="ba"><span class="b">{r["before"]}</span>'
            f'<span class="a">{r["after"]}</span></div>\n'  # and what is heard after it, which closes cell 5
            # cell 6: the status badge, and then the end of the row
            f'    <div class="st"><span class="badge s-{r["status"]}">{ST[r["status"]]}</span></div></div>')


# One loop card as HTML, character for character what the script draws: the loop number with promote or veto, the
# change made, four measurements and, for a veto, its reasons as a bullet list. lp is one entry of the loop history.
def _loop(lp: dict) -> str:
    m = lp["rows"]  # the loop's measurements by name
    # the reasons as a bullet list, or nothing for a promoted loop
    reasons = "<ul>" + "".join(f"<li>{x}</li>" for x in lp["reasons"]) + "</ul>" if lp["reasons"] else ""
    # the card's three lines are indented like the script's template string
    return (f'<div class="loop"><div class="n">{_js(lp["loop"])}'
            # the decision (promoted or vetoed) beside the loop number
            f'<small class="{lp["decision"]}">{lp["decision"]}</small></div>\n'
            f'  <div><div><b>{lp.get("change") or ""}</b></div>\n'  # the change that was tried
            f'  <div class="kv"><span>score <b>{_js(lp["score"])}</b></span>'  # the loop's score
            f'<span>WER <b>{_pct(m.get("asr_wer"))}</b></span>'  # the recognizers' word error rate
            # how many words both recognizers misheard
            f'<span>misheard words <b>{_js(m.get("asr_confirmed_words"))}</b></span>'
            # how many words the dictionary and the context checks flagged
            f'<span>dictionary + context flags <b>{_js(m.get("lexicon_flagged_words"))}</b></span></div>\n'
            f'  {reasons}</div></div>')  # the reasons list, empty for a promoted loop, and the end of the card


# The plain-HTML content of every container the script fills, by the name of its mark: the intro paragraph, the six
# summary tiles, the word list (or "No words match." when the report has no rows), the loop cards and the credits line.
# D is the page data that build() collects; it is the same object the script reads.
def static_copy(D: dict) -> dict:
    rows = D["rows"]  # every report row
    wrong = [r for r in rows if r["status"] != "kept"]  # the rows that were wrong; a "kept" row was checked and left
    occ = sum(r["n"] for r in wrong)  # the places where those words were wrong
    fixed = sum(r["n"] for r in rows if r["status"] in ("fixed", "better"))  # places that a fix or a retake improved
    first, last = D["first"], D["last"]  # the first and the last measurement's figures
    # the intro: totals of words, different words and segments, then how many words were wrong and in how many places
    lede = (f"All {_fmt(D['words'])} spoken words ({_fmt(D['unique'])} different words, "
            # the segment count, which ends the first sentence
            f"{_fmt(D['segments'])} narration segments) were heard back and checked. "
            # how many words were wrong and in how many places, then the start of the last sentence
            f"{len(wrong)} words or readings were wrong in {occ} places. The fixes "
            "were tried, adopted when they did better, re-voiced and measured again.")
    # the tiles as (big value, caption); an arrow runs from the first measurement to the last
    tiles = [(_fmt(D["words"]), "spoken words tracked"),
             (occ, "mispronounced occurrences found"),  # tile 2: the places where a word was mispronounced
             (fixed, "occurrences fixed"),  # tile 3: the places that a fix or a retake improved
             # tile 4: words both recognizers misheard, first measurement to last
             (f"{_js(first.get('asr_confirmed_words'))} → {_js(last.get('asr_confirmed_words'))}",
              "words both recognizers misheard"),
             # tile 5: wrong readings found in context, first measurement to last
             (f"{_js(D['het_first'])} → {_js(D['het_last'])}", "wrong readings in context (auto-detected)"),
             # tile 6: the word error rate, first measurement to last
             (f"{_pct(first.get('asr_wer'))} → {_pct(last.get('asr_wer'))}", "recognizer word error rate")]
    return {"LEDE": lede,  # the intro paragraph; the other keys are the containers named in the docstring above
            # the summary tiles, each a big value over its caption
            "STATS": "".join(f'<div class="stat"><b>{b}</b><span>{s}</span></div>' for b, s in tiles),
            # the header row, then one row per word, or the empty-list message
            "LIST": HEAD + ("".join(_row(r) for r in rows) or '<div class="empty">No words match.</div>'),
            "LOOPS": "".join(_loop(lp) for lp in D["loops"]),  # the loop cards, one for each loop in the history
            "FOOT": CREDITS}  # the credits line


# The report page for the page data. The template gets the data as JSON for the script, the plain copy of each container
# the script fills, and the book title with its HTML escaped. Returns the fragment text, without doctype, head or body.
def render_page(data: dict, title: str) -> str:
    # ASCII-only JSON; "</" is escaped so the data can never end the script element early
    blob = json.dumps(data, ensure_ascii=True).replace("</", "<\\/")
    page = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", blob)  # the data goes into its placeholder
    for name, text in static_copy(data).items():  # each container's plain copy
        page = page.replace(f"<!--__STATIC_{name}__-->", text)  # takes the place of the container's mark
    # page title and eyebrow text, escaped
    return page.replace("__TITLE_HTML__", html.escape(title)).replace("__TITLE__", html.escape(title))


# The fragment as a standalone page that a browser opens from disk: a doctype, charset and viewport meta, then the
# fragment's head material (title, links, styles) inside head and its visible part inside body.
def standalone_page(page: str) -> str:
    # where the visible page starts; everything before it (title, links, style) belongs in the head
    i = page.index('<div class="wrap">')
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'  # doctype and charset come first
            # viewport meta, so phones lay the page out at device width
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
            # the head part, then the visible part wrapped in body and closing html tags
            + page[:i] + "</head>\n<body>\n" + page[i:] + "\n</body>\n</html>\n")


# Parse a JSON file, or return None when it does not exist (a probe that never ran). p is a str or Path.
def _load(p):
    # UTF-8 read, so non-ASCII phonemes and quotes survive on Windows too
    return json.loads(Path(p).read_text(encoding="utf-8")) if Path(p).exists() else None


# Build the mispronounced-words page for a project: one row per problem word with its cause, fix and before/after
# result, the loop history and summary numbers. name selects the audit run (default "pron"); log gets one line.
# Writes work/rsi/<name>/report/Pronunciation_Audit.html (standalone page) and Pronunciation_Audit.fragment.html (the
# body for hosting as an artifact). Returns the standalone page's Path; RuntimeError when nothing was measured.
def build(project, name: str = "pron", log=print) -> Path:
    mods = configure(project)  # modules configured for this project (trial.word_key is needed)
    d = project.p("rsi") / name  # run directory, used as is (report building never seals it)
    ms = sorted(d.glob("measure-*.json"))  # every measurement file, oldest first by its zero-padded name
    if not ms:  # no measurement files exist yet, so nothing to report
        # the report stage catches this and records the stage as skipped
        raise RuntimeError("no audit measurements yet")
    first, last = ms[0], ms[-1]  # first measurement = before (the baseline), last = after
    # recognizer probe outputs for the first and last measurement
    A = {k: _load(d / m.stem / "asr" / "asr_measure.json") for k, m in (("first", first), ("last", last))}
    # dictionary probe outputs for the first and last measurement
    L = {k: _load(d / m.stem / "lexicon" / "lexicon_measure.json") for k, m in (("first", first), ("last", last))}
    # r-colour probe outputs for the first and last measurement
    R = {k: _load(d / m.stem / "rcolour.json") for k, m in (("first", first), ("last", last))}
    # the two measurement files themselves; their metrics feed the summary tiles
    mf, ml = json.loads(first.read_text(encoding="utf-8")), json.loads(last.read_text(encoding="utf-8"))
    ov = _load(project.p("pronunciations")) or {}  # current overrides file, or an empty dict
    words_ov, tokens_ov = ov.get("overrides", {}), ov.get("tokens", {})  # word overrides and single-token overrides
    word_key = mods["trial"].word_key  # normalizer shared with the trial and the recognizer scoring
    # overrides keyed by normalized word, so fixes can be looked up for scored words
    ov_by_key = {word_key(k): v for k, v in words_ov.items()}
    retaken = {}  # segment id -> chosen take label, for segments a retake replaced
    # every retake round's result file in order; nothing when there were no retakes
    for p in sorted((d / "retakes").glob("*/result.json")) if (d / "retakes").exists() else []:
        # each segment's chosen take in that round
        for sid, c in json.loads(p.read_text(encoding="utf-8"))["chosen"].items():
            if c["take"] != "current":  # a new take replaced the narration's own
                retaken[sid] = c["take"]  # remember which take label replaced this segment
    # book model, for chapter names and the distinct-word count
    dm = json.loads(project.p("docmodel").read_text(encoding="utf-8"))
    blocks, tokens = dm["blocks"], dm["tokens"]  # display blocks and word tokens
    chapter_of, cur = {}, None  # token id -> chapter title, and the chapter seen most recently while walking
    for t in tokens:  # word tokens in reading order across the book
        b = blocks[t["block"]]  # the display block this token belongs to
        if b["kind"] in ("h1", "cover"):  # an h1 (or the cover, for the front matter) starts a new chapter name
            cur = b.get("text")  # the heading text names the tokens that follow
        chapter_of[t["i"]] = cur  # token index -> current chapter title

    # The one or two chapters holding most of the given token ids, most frequent first (for the "where" line). tids is a
    # list of token ids; returns a list of at most two chapter titles.
    def where(tids):
        # count chapter names; ids may be strings (JSON keys), so they are converted to int
        c = Counter(chapter_of.get(int(t)) for t in tids)
        return [k for k, _ in c.most_common(2) if k]  # top two names, leaving out None (tokens before any chapter)

    # How the override of a normalized word reads in the report, as (text, IPA or None). A raw [[phonemes]] override
    # gives ("phonemes", the phoneme string); a respelling gives the escaped spelling in quotes; no override ("", None).
    def fix_text(key):
        v = ov_by_key.get(key)  # override value stored for the normalized word
        if not v:  # no override was applied to this word
            return "", None  # empty fix text and no phonemes to show
        # strip [[ and ]] for display; a respelling is HTML-escaped inside curly quotes
        return ("phonemes", v[2:-2]) if v.startswith("[[") else (f"spelled &ldquo;{html.escape(v)}&rdquo;", None)

    # Before or after text for a misheard word from one recognizer measurement `a`: how many occurrences each recognizer
    # heard right, plus the most common wrong hearing. w is the normalized word. Returns HTML text or "-".
    def heard_txt(a, w):
        if not a:  # the recognizer probe did not run for this measurement
            return "-"  # nothing to show without recognizer data
        c = a["per_word"].get(w)  # first recognizer's figures for the word (count, ok, heard variants)
        k = a["per_word_confirm"].get(w)  # confirming recognizer's figures, when the word was rechecked
        if not c:  # the word was not scored in this measurement
            return "-"  # word missing from this measurement: show a dash
        top = [h for h in c["heard"] if h != w][:1]  # most frequent hearing that differs from the word itself
        s = f"{c['ok']}/{c['count']}"  # correct occurrences out of all, first recognizer
        if k:  # the confirming recognizer scored it too
            s += f" and {k['ok']}/{k['count']}"  # append the confirming recognizer's ok/count pair
        # name the common mishearing only when something was actually wrong
        return s + " heard right" + (f" (&ldquo;{html.escape(top[0])}&rdquo;)" if top and c["ok"] < c["count"] else "")

    rows, seen = [], set()  # report rows, and normalized words already listed (each word appears once)
    # words still confirmed as misheard in the last measurement
    last_conf = {e["word"] for e in (A["last"] or {}).get("confirmed", [])}
    # Category "heard": words both recognizers misheard in the first measurement.
    for e in (A["first"] or {}).get("confirmed", []):
        w = e["word"]  # normalized word from the recognizer scoring
        seen.add(w)  # mark this heard word as listed
        # token ids (JSON keys, so strings) of its occurrences in the first measurement
        tids = [t for t, o in A["first"]["occurrences"].items() if w in o["expected"].split()]
        ftxt, fipa = fix_text(w)  # override applied to this misheard word, if any
        segs = set(e["segs"])  # narration segments where the word occurs
        # no override, but one of its segments was replaced by a better take
        if not ftxt and any(s in retaken for s in segs):
            ftxt = "new take, picked by both recognizers"  # credit the retake as this word's fix
        # one row: category, word, occurrence count, chapters
        rows.append({"cat": "heard", "word": w, "n": e["count"], "where": where(tids),
                     # what was wrong, and up to two things the recognizer heard instead
                     "problem": "Heard wrong by both recognizers", "cause": "heard as " + ", ".join(
                         # each hearing escaped and in curly quotes
                         f"&ldquo;{html.escape(h)}&rdquo;" for h in list(e.get("ctc_heard", {}))[:2]),
                     # fix text and phonemes; recognition counts in the first measurement
                     "fix": ftxt or "none yet", "fix_ipa": fipa, "before": heard_txt(A["first"], w),
                     "after": heard_txt(A["last"], w),  # recognition counts in the last measurement
                     # still misheard: "listen"; otherwise "fixed" when a fix exists, else "better"
                     "status": "listen" if w in last_conf else ("fixed" if ftxt else "better"), "note": ""})
    # words the dictionary probe still flags in the last measurement
    last_flag = {r["word"] for r in (L["last"] or {}).get("flagged", [])}
    # lexicon rows of the last measurement, by word
    rows_last = {r["word"]: r for r in (L["last"] or {}).get("rows", [])}
    # Category "dictionary": wrong stress or reading against CMU, or a dropped apostrophe, in the first measurement.
    for r in (L["first"] or {}).get("flagged", []):
        w = r["word"]  # the word exactly as the lexicon writes it
        if word_key(w) in seen:  # already listed under another category
            continue  # skip duplicates so each word gets one row
        seen.add(word_key(w))  # mark this dictionary word as listed
        ftxt, fipa = fix_text(word_key(w))  # override applied to the flagged dictionary word
        after = rows_last.get(w, {}).get("espeak_en", "")  # what espeak gives the voice now
        # row basics; the flagged row's tokens give the chapter names
        rows.append({"cat": "dictionary", "word": w, "n": r["count"], "where": where(r.get("tokens", [])),
                     # a display mismatch means the spoken text changed the word (a dropped apostrophe)
                     "problem": "Wrong stress or reading" if r.get("kind") != "display_mismatch" else "Wrong word spoken",
                     # the probe's reason; fix text, else "fixed in the text" when no longer flagged, else "kept"
                     "cause": html.escape(r.get("reason") or ""), "fix": ftxt or ("fixed in the text" if w not in last_flag else "kept"),
                     # phonemes before, shown between slashes in an IPA span
                     "fix_ipa": fipa, "before": f'<span class="ipa">/{html.escape(r.get("espeak_en", ""))}/</span>',
                     # phonemes after, or "-" when the word no longer occurs
                     "after": f'<span class="ipa">/{html.escape(after)}/</span>' if after else "-",
                     # no longer flagged in the last measurement means fixed
                     "status": "fixed" if w not in last_flag else "kept",
                     # the first two CMU dictionary readings for comparison
                     "note": "Dictionary: " + ", ".join(html.escape(x) for x in r.get("cmu_ipa", [])[:2])})
    # (word, token) pairs still read the wrong way in the last measurement
    het_last = {(h["word"], o["token"]) for h in (L["last"] or {}).get("heteronyms", []) for o in h["occurrences"] if o["suspect"]}
    groups = defaultdict(list)  # heteronym -> its wrongly read occurrences in the first measurement
    # Category "context": heteronyms read the wrong way for their sentence (for example "close" read as the verb).
    for h in (L["first"] or {}).get("heteronyms", []):
        for o in h["occurrences"]:  # each recorded occurrence of this heteronym word in the book
            if o["suspect"]:  # read with the wrong role for its sentence
                groups[h["word"]].append(o)  # group the suspect occurrences by word
    for w, occ in groups.items():  # one report row per heteronym word
        if word_key(w) in seen:  # already listed under another category: each word appears once
            continue
        seen.add(word_key(w))
        # occurrences no longer suspect in the last measurement
        fixed = sum(1 for o in occ if (w, o["token"]) not in het_last)
        # token override of the first occurrence only; the others are assumed to share it
        v = tokens_ov.get(str(occ[0]["token"]))
        # occurrence count and the chapters where they occur
        rows.append({"cat": "context", "word": w, "n": len(occ), "where": where([o["token"] for o in occ]),
                     "problem": "Wrong reading for the sentence",
                     # example: the first occurrence's two roles and its sentence, cut to 90 characters and escaped
                     "cause": f"read as {occ[0]['reading_role']}, the sentence needs the {occ[0]['expected_role']}: "
                              # continuation of the cause text with the quoted sentence
                              f"&ldquo;{html.escape(occ[0]['sentence'][:90])}&rdquo;",
                     # fix description and its phonemes, or "none yet"
                     "fix": "phonemes for these occurrences only" if v else "none yet", "fix_ipa": v[2:-2] if v else None,
                     # wrong readings before and after
                     "before": f"{len(occ)} wrong", "after": f"{len(occ) - fixed} wrong",
                     # all fixed, some fixed ("better"), or none ("listen")
                     "status": "fixed" if fixed == len(occ) else ("better" if fixed else "listen"), "note": ""})
    # r-colour rows of the last measurement, by word
    rc_last = {r["word"]: r for r in (R["last"] or {}).get("flagged", [])}
    # Category "r": an r heard where the spelling has none (third formant dip) in the first measurement.
    for r in (R["first"] or {}).get("flagged", []):
        # require at least two low occurrences, and skip words listed already
        if r["low"] < 2 or word_key(r["word"]) in seen:
            continue  # leave this word out of the r-colour category
        seen.add(word_key(r["word"]))  # mark this r-coloured word as listed
        ftxt, fipa = fix_text(word_key(r["word"]))  # override applied to the r-coloured word, if any
        after = rc_last.get(r["word"])  # its r-colour row in the last measurement, or None when no longer flagged
        # row basics: category, word, count, chapters
        rows.append({"cat": "r", "word": r["word"], "n": r["count"], "where": where(r["tokens"]),
                     # problem label, and the median F3 in Hz as the cause
                     "problem": "An r that is not in the spelling", "cause": f"F3 {r['f3_median']:.0f} Hz in "
                     # how many occurrences dipped; fix text and phonemes
                     f"{r['low']} of {r['count']}", "fix": ftxt or "none yet", "fix_ipa": fipa,
                     # F3 before, and after (or "no r" when the dip is gone)
                     "before": f"F3 {r['f3_median']:.0f} Hz", "after": f"F3 {after['f3_median']:.0f} Hz" if after else "no r",
                     # fixed when no longer flagged; otherwise better with a fix, else listen
                     "status": "fixed" if not after else ("better" if ftxt else "listen"), "note": ""})
    # display order of the categories; "kept" appears although no row uses it as a category
    order = {"heard": 0, "r": 1, "context": 2, "dictionary": 3, "kept": 4}
    # by category, most occurrences first, then alphabetically
    rows.sort(key=lambda r: (order[r["cat"]], -r["n"], r["word"]))
    # loop history for the page, one dict per decision
    loops = [{"loop": h["loop"], "decision": h["decision"], "score": h["score"], "change": h.get("change"),
              # only each metric's value from the decision rows
              "reasons": h["reasons"], "rows": {k: v["value"] for k, v in h["rows"].items()}} for h in status(project, name)]
    # everything the page script needs: rows, loops, first and last metrics
    data = {"rows": rows, "loops": loops, "first": mf["metrics"], "last": ml["metrics"],
            # spoken words in total, and distinct display words (lowercased, attached punctuation included)
            "words": mf["inputs"]["words_total"], "unique": len({t["disp"].lower() for t in tokens}),
            "segments": mf["inputs"]["segments"],  # narration segments measured in the first run
            # heteronym suspects in the first measurement
            "het_first": (L["first"] or {}).get("totals", {}).get("heteronym_suspects", 0),
            # heteronym suspects in the last measurement
            "het_last": (L["last"] or {}).get("totals", {}).get("heteronym_suspects", 0)}
    # the template with the data for the script, the plain copy for viewers without scripts, and the escaped title
    page = render_page(data, project.title)
    out = d / "report"  # report folder inside the run directory
    out.mkdir(parents=True, exist_ok=True)  # create the report folder if needed
    frag = out / "Pronunciation_Audit.fragment.html"       # for embedding in another page
    frag.write_text(page, encoding="utf-8")  # the template text as is, with no doctype, head or body tags
    dst = out / "Pronunciation_Audit.html"  # the standalone page, opened directly in a browser (unlike the fragment)
    dst.write_text(standalone_page(page), encoding="utf-8")  # write the standalone page as UTF-8
    log(f"audit report: {len(rows)} rows -> {dst}")  # one log line with the row count and the path
    return dst  # path of the standalone page for the caller
