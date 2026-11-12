# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""The built-in pipeline stages, declared for the registry (see registry.py for what each field means).

Order of definition is the run order among stages that do not depend on each other.
"""
# Postpones evaluation of type annotations (PEP 563) so every annotation is kept as a string.
# The toolkit puts this line at the top of every module for consistency and so newer syntax such as `X | None` works on
# older Pythons.
from __future__ import annotations

import json  # used to read report.json and theme.json and to log compact summaries
import shutil  # copy2 copies the draft to book.md while keeping the file's timestamps

# The `stage` decorator only attaches a metadata dict to each function below (attribute __narrate_stage__).
# registry.load() later scans this module, turns each tagged function into a Stage record and orders them.
from .registry import stage

# The `satisfied` hook of the extract stage: the planner calls it before deciding whether extract must run.
# project is the open Project. Returns a reason string (the stage is then skipped with that reason) or None.
# Once work/book.md exists it is the hand-edited text, so extracting again would only rewrite a draft nobody reads.
def _book_exists(project):
    # deleting book.md is the documented way to make extraction happen again
    return "work/book.md exists (edit it; delete it to extract again)" if project.p("book").exists() else None


def _voice_inputs(project):
    from .voice.narrate import cache_inputs
    return cache_inputs(project)


def _voice_outputs(project):
    from .voice.narrate import validate_outputs
    return validate_outputs(project)


def _export_outputs(project):
    from .audio import manifest_issues
    return manifest_issues(project)


def _export_inputs(project):
    from .audio import audit_provenance
    return audit_provenance(project)


# Stage 1, extract: reads the source .txt, .md or SpeechKit request .json into work/draft.md and a review report.
# The decorator declares what the planner needs: the files it reads and writes, the settings it depends on, its options.
# The function receives the Project and the run Ctx; it returns the summary dict from extract.run, which the run log
# stores.
# Side effects: writes work/draft.md and work/extract/report.json.
# "@input" is the file named by project.json "input"; outputs are PATHS keys
@stage("extract", dependencies=["ingest"], inputs=["@input"], outputs=["draft", "extract_report"], settings=["input", "ingest", "text_fixes"],
       satisfied=_book_exists,  # skip entirely once book.md exists (see _book_exists above)
       description="the .txt, .md or request .json -> work/draft.md and a review report")
# extract(project, ctx) -> dict with draft path, block/word counts, OCR pages, suspect count and problems
def extract(project, ctx):
    from . import ingest  # the text and request readers, imported only when the stage runs
    r = ingest.run(project)  # writes work/draft.md and work/extract/report.json
    # the summary is JSON-encoded and cut to 600 characters so a long problem list cannot flood the log
    ctx.log(f"extract: {json.dumps(r, ensure_ascii=False)[:600]}")
    return r  # the extract summary becomes this stage's recorded result


# Stage 2, check: creates work/book.md from the draft the first time, validates it and lists words to review.
# On that first creation it returns a "stop" so a full run pauses for a human review, unless the caller passed
# accept_draft.
# Returns counts of blocks per kind, the validation problems, the number of review words and whether book.md was just
# made.
# book.md is both input and output, so editing it by hand changes the fingerprint and re-runs check
@stage("check", dependencies=["text/markup.py"], needs=["extract"], inputs=["book", "draft"], outputs=["book"],
       # the description explains the one-time review pause
       description="create work/book.md from the draft (once), validate it, list words to review; stops a full run "
                   "the first time so the draft can be reviewed, unless accept_draft")
def check(project, ctx):  # check(project, ctx) -> dict; side effect: may create work/book.md
    # ProjectError is the toolkit's "a project file or setting is wrong" exception, shown as a usage error
    from .project import ProjectError
    from .text import markup  # markup parses the simple Markdown dialect used by book.md
    created = False  # remembers whether this run made book.md, which decides the review stop below
    # book.md is created only once; after that it belongs to the user and is never overwritten
    if not project.p("book").exists():
        if not project.p("draft").exists():  # without a draft there is nothing to copy from
            # tells the user which stage to run first instead of failing later with a missing-file error
            raise ProjectError("no work/book.md and no work/draft.md: run `extract` first")
        # book.md starts as an exact copy of the draft, timestamps included
        shutil.copy2(project.p("draft"), project.p("book"))
        created = True  # flag the creation for the result and for the stop decision
        # point the user at the file to read before any audio is generated from it
        ctx.log("check: work/book.md created from the draft - review it (and the report) before narrating")
    text = project.p("book").read_text(encoding="utf-8")  # the text every later stage works from
    # list of human-readable problems (empty file, no chapter heading, table rows without a header, ...)
    probs = markup.validate(text, project.title)
    # list of block dicts; parse puts a synthetic "cover" block first, so the count includes it
    blocks = markup.parse(text, project.title)
    kinds = {}  # tally of how many blocks there are of each kind (h1, h2, p, bullet, th, ...)
    for b in blocks:  # walk every parsed block
        kinds[b["kind"]] = kinds.get(b["kind"], 0) + 1  # count this block's kind, starting at 0 when the kind is new
    # one summary line, e.g. "check: 212 blocks {'cover': 1, 'h1': 9, 'p': 180, ...}"
    ctx.log(f"check: {len(blocks)} blocks {kinds}")
    for pr in probs:  # each validation problem gets its own log line
        ctx.log(f"check: {pr}")  # problems are reported, not fatal: the user decides whether to fix them
    sus = []  # stays empty when there is no extraction report
    rep = project.p("extract_report")  # work/extract/report.json written by the extract stage
    if rep.exists():  # a text project brought in another way may have no report
        # the words the extractor flagged for a human to look at (odd names, glued words, OCR doubts)
        sus = json.loads(rep.read_text(encoding="utf-8")).get("suspects") or []
        if sus:  # only mention them when there are some
            # say how many there are and where the full list lives
            ctx.log(f"check: {len(sus)} words to review (work/extract/report.json), e.g. "
                    # show the first 12 as examples; each suspect is a dict, its "word" field is printed
                    + ", ".join(str(s.get('word', s)) for s in sus[:12]))
    # the stage result, stored in the run record and returned to the CLI/MCP caller
    out = {"blocks": len(blocks), "kinds": kinds, "problems": probs, "review_words": len(sus), "created": created}
    # pause only on the very run that created book.md, and only if the caller did not pre-accept the draft
    if created and not ctx.accept_draft:
        # registry.run sees "stop" and halts the pipeline after recording this stage as done
        out["stop"] = "review work/book.md (and work/extract/report.json), then run again; or pass accept_draft"
    return out  # return the summary (with or without the stop request)


# Stage 3, script: builds the narration script from book.md.
# docmodel.json holds the blocks and display tokens; request.json holds the segments the voice engine speaks.
# Re-runs when book.md, the pronunciation overrides or any of the listed settings change; returns the counts from
# docmodel.build.
# pronunciations is work/pronunciations.json, written by autofix
@stage("script", dependencies=["text", "chapters.py"], needs=["check"], inputs=["book", "pronunciations"], outputs=["docmodel", "request"],
       # metadata and spoken-form rules that change the script's text
       settings=["title", "subtitle", "author", "language", "spoken", "chapters"],
       # shown when listing stages
       description="book.md -> docmodel.json + request.json (spoken forms, pronunciation fixes)")
def script(project, ctx):  # script(project, ctx) -> dict of counts (blocks, tokens, segments, ...)
    from .text import docmodel  # deferred import of the script builder
    r = docmodel.build(project)  # writes work/docmodel.json and work/request.json
    ctx.log(f"script: {r}")  # log the counts as the stage's progress line
    return r  # the counts become the stage result


# The satisfied hook of the prevoice stage: a reason to skip it (it is off, the default), or None when it must run
# (it is on, or it was turned off and its fixes still have to be taken out of the script). project is the Project.
def _prevoice_off(project):
    from .audit import prevoice as pv  # imported only when the planner asks
    return pv.off_reason(project)  # None means the planner's fingerprints decide


# Stage 3b, prevoice (opt-in with project.json prevoice.enabled): heteronyms that espeak would read in the wrong role
# get a per-token fix before narration (lowest priority: work/pronunciations.json always wins), so the pronunciation
# loop only verifies them. It rebuilds the script, so the fixes reach narrate through request.json, the input narrate
# already fingerprints. Returns a summary (occurrences, fixes, warnings); writes work/prevoice/*.json.
@stage("prevoice", dependencies=["audit/prevoice.py", "audit/autofix.py", "text", "env.py", "../emblem_rsi/pron"], needs=["script"], inputs=["book", "pronunciations"],  # what the fixes are computed from
       # the fixes the script applies, and every occurrence with its decision
       outputs=["work/prevoice/fixes.json", "work/prevoice/decisions.json"],
       # its own settings, everything that changes the script's words, and the voice (its phoneme set)
       settings=["prevoice", "title", "subtitle", "author", "language", "spoken", "chapters", "voice"],
       platforms=["linux"], refreshes=["script"], satisfied=_prevoice_off,  # espeak lives in the Linux voice runtime
       # the one-line summary shown by `speechkit-narrate stages`, MCP and PowerShell help
       description="opt-in: fix heteronyms espeak would read in the wrong role before narration (prevoice.enabled)")
def prevoice(project, ctx):  # prevoice(project, ctx) -> dict summary; off, it only takes its old fixes out
    from .audit import prevoice as pv  # speechkit_narrate/audit/prevoice.py, imported only when the stage runs
    return pv.run(project, log=ctx.log)  # decide, write the fixes, rebuild the script


# Stage 4, narrate: voices every segment of request.json into WAV files under work/speech.
# It runs only on Linux (the offline neural voice runtime); on Windows the registry delegates it to WSL through
# SPEECHKIT_DELEGATE.
# heavy=True makes the MCP server run it as a background job. Returns voiced and text-only counts plus how many segments
# need a pace review.
# outputs=["speech"] is the work/speech folder; it must exist and be non-empty. It needs prevoice (skipped while off) so
# `run narrate` applies the prevoice fixes first; needs are not fingerprinted, so this re-runs nothing.
@stage("narrate", needs=["script", "prevoice"], inputs=["request"], outputs=["speech"], settings=["voice.profile"],
       dependencies=["voice", "env.py", "../emblem_rsi/pron/_espeak_helper.py"], fingerprint_extra=_voice_inputs, validate=_voice_outputs,
       operational_options=["workers"],
       platforms=["linux"],  # Linux-only, delegated to WSL on Windows
       # workers: how many voice processes to run in parallel; no default, so project.json voice.workers applies
       heavy=True, options={"workers": {"type": "integer", "minimum": 1, "description": "parallel voice workers"}},
       # it resumes after an interruption and re-voices only segments whose text changed
       description="voice every segment with speechkit-offline-neural-tt (resumes; re-voices only what changed)")
# narrate(project, ctx) -> dict; side effects: WAV and .wav.json files in work/speech, worker processes started
def narrate(project, ctx):
    from .voice import narrate as nar  # the voice module is imported only when narration actually runs
    # None for workers lets narrate fall back to the project's voice.workers setting
    r = nar.narrate(project, log=ctx.log, workers=ctx.opt("workers"))
    # status adds totals across the whole book: segments, segments voiced, and minutes of audio
    ctx.log(f"narrate: {r}; {nar.status(project)}")
    return r  # the narration counts are the stage result


# Stage 5, autofix: the pronunciation repair loop (emblem-rsi sealed rubric).
# It measures, proposes respellings for misheard words, tries each in its sentences, applies winners, re-measures, then
# promotes or vetoes.
# refreshes lists script and narrate because autofix rewrites their outputs consistently; their records are refreshed so
# they do not re-run needlessly.
# Returns one summary per loop plus the number of word fixes kept and single-token fixes tried; side effects: pronunciations.json,
# speech files and work/rsi/pron.
# its output is the loop log, one JSON line per measured loop
@stage("autofix", dependencies=["audit", "voice", "text", "env.py", "../emblem_rsi/pron",
                               "../emblem_rsi/loop.py", "../emblem_rsi/rubrics/pronunciation.json"], needs=["narrate"], inputs=["request", "speech"], outputs=["work/rsi/pron/log.jsonl"],
       # Linux-only and long-running, like narrate
       settings=["voice.profile", "audit"], platforms=["linux"], heavy=True, refreshes=["script", "narrate"],
       # each candidate spelling is voiced 3 times so one lucky take does not decide
       options={"takes": {"type": "integer", "default": 3, "description": "takes per candidate fix"},
                # words still misheard after the fixes get 4 extra takes and the best one is kept
                "retakes": {"type": "integer", "default": 4, "description": "best-of-N retakes for words still misheard"}},
       # stage description continues on the next line
       description="pronunciation loop: measure, propose fixes, try them in their sentences, apply winners, "
                   "re-measure, promote or veto (emblem-rsi sealed rubric)")
def autofix(project, ctx):  # autofix(project, ctx) -> dict summary of the loops and fixes
    from .audit import autofix as af  # the loop driver in speechkit_narrate/audit/autofix.py
    from .text import docmodel  # needed to rebuild the docmodel after the overrides change
    # the second ctx.opt argument matters when this function is called directly with a Ctx that names no stage
    r = af.autofix(project, takes=ctx.opt("takes", 3), retake_takes=ctx.opt("retakes", 4), log=ctx.log)
    for e in r["loops"]:  # every measured loop (baseline, fixes, retakes) appears in r["loops"]
        # decision is "promote" or "veto"; change describes what that loop tried
        ctx.log(f"autofix: loop {e['loop']} {e['decision'].upper()} score {e['score']} ({e.get('change')})")
    docmodel.build(project)                     # the docmodel follows the final overrides
    # keep just the four headline fields of each loop so the stored result stays small
    return {"loops": [{k: e.get(k) for k in ("loop", "decision", "score", "change")} for e in r["loops"]],
            # word_fixes counts accepted whole-word respellings; token_fixes counts the single-occurrence fixes that
            # were proposed and tried (the loop's promote or veto above says whether that round was kept)
            "word_fixes": len(r.get("chosen", {})), "token_fixes": len(r.get("proposed_tokens", {}))}


# Stage 6, audit: one measured step of the pronunciation loop without proposing any fixes.
# default=False keeps it out of a plain `run`/`all`; it is run by name after manual edits to see whether they helped.
# Returns the loop number, the promote/veto decision, the score and the pass/value of each rubric row.
# Linux-only and heavy because measuring re-transcribes the narration
@stage("audit", dependencies=["audit", "env.py", "../emblem_rsi/pron", "../emblem_rsi/loop.py",
                             "../emblem_rsi/rubrics/pronunciation.json"], settings=["voice.profile", "audit"], needs=["narrate"], inputs=["request", "speech"], platforms=["linux"], default=False, heavy=True,
       # note: free text recorded in the loop log as the change being judged
       options={"note": {"type": "string", "description": "what changed since the last measurement"}},
       # the summary printed by `speechkit-narrate stages`
       description="one measured step of the pronunciation loop (measure + promote/veto), no fixes")
# audit(project, ctx) -> dict; side effect: a new measurement folder and log entry in work/rsi/pron
def audit(project, ctx):
    from . import audit as au  # speechkit_narrate/audit/__init__.py, imported only when this stage runs
    note = ctx.opt("note") or "measurement"  # without a note the log entry just says "measurement"
    # the measurer's progress lines are discarded (the lambda swallows them); it returns the measurement file path
    m = au.measure(project, note, log=lambda *_: None)
    # judge that measurement against the sealed rubric and the history; appends a log entry
    e = au.decide(project, note, measurement=m)
    # one line with the verdict, e.g. "audit: loop 4 PROMOTE score 0.93"
    ctx.log(f"audit: loop {e['loop']} {e['decision'].upper()} score {e['score']}")
    return {"loop": e["loop"], "decision": e["decision"], "score": e["score"],  # the headline numbers of the decision
            # for every rubric row keep whether it passed and its measured value
            "rows": {k: {"pass": r["pass"], "value": r["value"]} for k, r in e["rows"].items()}}


# Stage 13, report: the HTML page of mispronounced words, before and after the autofix loop.
# It reads the measurements in work/rsi/pron and the pronunciation overrides; when there are no measurements it is
# skipped, not failed.
# Returns the page path, or {"skipped": reason}.
# runs after autofix so the page shows the final state
@stage("report", dependencies=["audit/report.py", "audit/__init__.py", "audit/template/report.html",
                              "../emblem_rsi/pron", "../emblem_rsi/loop.py"], needs=["autofix"], inputs=["work/rsi/pron", "pronunciations"],
       # the trailing "?" marks an optional output, so a skipped report does not count as missing
       outputs=["work/rsi/pron/report/Pronunciation_Audit.html?"],
       description="the mispronounced-words page")  # short description in listings
def report(project, ctx):  # report(project, ctx) -> dict
    from .audit import report as rp  # the report builder in speechkit_narrate/audit/report.py
    try:  # a missing measurement must not stop packaging
        rp.build(project, log=ctx.log)  # writes Pronunciation_Audit.html (and a fragment for hosting)
        # the path of the finished page, as a string
        return {"html": str(project.p("rsi") / "pron" / "report" / "Pronunciation_Audit.html")}
    except RuntimeError as e:  # report.build raises RuntimeError when there are no audit measurements yet
        ctx.log(f"report: skipped ({e})")  # log why the page was not made
        return {"skipped": str(e)}  # a normal result, so the pipeline continues to package


# Stage 8, export: the finished narration in out/narration: one audio file per chapter (wav, or mp3/m4a through
# ffmpeg), the word timings of each chapter, the full script, and a manifest with every file's SHA-256.
# Returns the manifest's summary (chapters, minutes, files); side effects: files in out/narration (stale ones removed).
@stage("export", needs=["narrate"], inputs=["docmodel", "request", "speech", "book", "work/rsi/pron/autofix-last.json"],
       dependencies=["audio.py", "chapters.py", "voice/identity.py", "../emblem_rsi/pron", "../emblem_rsi/loop.py"],
       fingerprint_extra=_export_inputs, validate=_export_outputs,
       outputs=["out/narration/manifest.json"], settings=["export", "chapters", "title", "subtitle", "author", "voice.profile"],
       options={"format": {"type": "string", "enum": ["wav", "mp3", "m4a"],
                           "description": "audio format (default: project.json export.format)"}},
       description="one audio file per chapter with word timings and a SHA-256 manifest (out/narration)")
def export(project, ctx):  # export(project, ctx) -> dict summary
    from . import audio  # chapter assembly; imported only when the stage runs
    return audio.export(project, fmt=ctx.opt("format"), log=ctx.log)
