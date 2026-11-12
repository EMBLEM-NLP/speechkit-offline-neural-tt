# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""`emblem-rsi code ...`: the audit loop applied to explaining code with comments.

    code seal    --dir RUN --repo ROOT [--rubric R] [--test CMD]... [--frozen FILE:1,2,3]...
                                          snapshot every code file as the baseline and seal the rubric
    code show    --dir RUN FILE [--from N --to M] [--todo]
                                          baseline lines with what each one needs (R = needs a comment,
                                          c = already explained, ^ = may take a comment above, > = may take one at
                                          the end)
    code check   --dir RUN FILE...        apply the maps in memory: map errors, lines still unexplained, whether the
                                          code is unchanged and still parses (writes nothing)
    code apply   --dir RUN [FILE...]      write baseline + maps into the repository (snapshot first)
    code measure --dir RUN [--note T] [--no-tests]
    code decide  --dir RUN [--change T]   promote or veto the latest measurement (rules in loop.decide)
    code revert  --dir RUN                put back the files the last apply replaced
    code status  --dir RUN                rubric, coverage and loop history
    code report  --dir RUN --out FILE     per-file coverage of the latest measurement as JSON

A map lives at RUN/maps/<file>.cmt (and <file>.2.cmt, ...): one entry per line, 'N^ text' for a comment line
above baseline line N, 'N> text' for a comment at the end of line N. See emblem_rsi/code/cmap.py.
"""
# Annotations stay unevaluated strings, so `X | None` style hints never fail on older Python 3 versions.
from __future__ import annotations

import json  # manifest, measurement and report files are JSON documents
import sys  # sys.stderr receives the error messages of the commands
from pathlib import Path  # run directory, baseline and output paths as Path objects

from .. import loop  # the generic loop: sealed rubric, decisions and the decision log
from . import cmap, langs  # cmap is imported but not used in this module; langs analyses lines for `show`
from . import measure as cm  # seal, measure, check, apply and revert, under the short name cm


# Print the result of measure.check for people. res is {path: check record}. For each file it prints one summary line
# (status word, coverage, entry count, identity, parse state, long lines) followed by errors, forbidden text,
# placeholder comments and uncovered lines, each list capped. Returns True only when every file is done.
def _print_check(res: dict) -> bool:
    clean = True  # stays True only while every file so far is complete
    for r, v in res.items():  # each checked file together with its result record
        # explained fraction; a file with nothing required counts as complete
        pct = v["covered"] / v["required"] if v["required"] else 1.0
        # Done means: no map errors, the file parses, the code is unchanged, nothing is uncovered and there is no
        # forbidden text or placeholder comment. Long lines are reported but do not block completion.
        done = not v["errors"] and v["ok"] and v["identity_ok"] and not v["uncovered"] and not v["forbidden"] \
            and not v["placeholders"]  # placeholder comments also block completion
        clean &= done  # one incomplete file makes the whole check fail
        # Summary line: status word, path, explained/required lines with the percentage, entry count, identity and parse
        # state.
        print(f"{'ok  ' if done else 'TODO'} {r}: {v['covered']}/{v['required']} lines explained ({pct:.1%}), "
              # entry count and whether the code identity still matches
              f"{v['entries']} map entries, code {'unchanged' if v['identity_ok'] else 'CHANGED'}, "
              # parse state, with up to 200 characters of the parser message
              f"{'parses' if v['ok'] else 'DOES NOT PARSE: ' + v['error'][:200]}"
              # mentions over-long lines only when the count is non-zero
              + (f", {v['long_lines']} lines made longer than 120 chars" if v["long_lines"] else ""))
        for e in v["errors"][:40]:  # show at most forty map or placement errors
            print(f"   error: {e}")  # each error indented under its file's summary line
        if len(v["errors"]) > 40:  # more than forty errors were found for this file
            print(f"   ... {len(v['errors']) - 40} more errors")  # say how many further errors were not shown
        for f in v["forbidden"][:10]:  # at most ten forbidden-text hits per file
            print(f"   forbidden text in a comment: {f}")  # each hit names the forbidden string that was found
        for p in v["placeholders"][:10]:  # at most ten placeholder comments per file
            print(f"   placeholder comment: {p}")  # the first sixty characters of each placeholder
        for n, t in v["uncovered"][:80]:  # up to eighty uncovered lines, with their baseline text
            print(f"   line {n:>5} needs a comment: {t}")  # right-aligned line number followed by its stripped code
        if len(v["uncovered"]) > 80:  # more uncovered lines exist beyond the first eighty
            print(f"   ... {len(v['uncovered']) - 80} more lines")  # report how many uncovered lines were left out
    return clean  # the exit code of `check` depends on this value


# Dispatch one `emblem-rsi code` subcommand. a is the argparse namespace: code_cmd names the subcommand, dir the run
# directory, plus that subcommand's own options. Returns the process exit code: 0 for success, 1 for an incomplete check
# or a vetoed round, 2 for an error. Side effects depend on the subcommand (seal and apply write files, measure runs the
# test suites).
def main_cmd(a) -> int:
    d = Path(a.dir)  # every subcommand works inside the run directory
    try:  # expected loop and file errors become a short message and exit code 2
        if a.code_cmd == "seal":  # seal: snapshot the repository and seal the rubric
            frozen = {}  # frozen lines by file, parsed from the --frozen options
            for spec in a.frozen or []:  # each option looks like FILE:1,2,3
                f, _, nums = spec.rpartition(":")  # split at the last colon so Windows drive letters survive
                # comma-separated line numbers, ignoring empty pieces
                frozen[f] = [int(x) for x in nums.split(",") if x]
            # Each --test is either a JSON list of arguments or a plain command line split on spaces. Splitting ignores
            # quoting, so commands with quoted arguments must use the JSON form.
            tests = [json.loads(t) if t.strip().startswith("[") else t.split() for t in (a.test or [])]
            # seal with the optional rubric, tests and frozen lines
            cm.seal_run(d, Path(a.repo), Path(a.rubric) if a.rubric else None, tests, frozen)
            return 0  # success: the run is sealed and ready
        if a.code_cmd == "show":  # show: print baseline lines with what each one needs
            man = cm.load_manifest(d)  # the manifest decides which files belong to the run
            if a.file not in {e["path"] for e in man["files"]}:  # the requested file must be one of the sealed files
                # refuse an unknown file path with a clear error
                raise loop.LoopError(f"{a.file} is not in the sealed manifest")
            text = (d / "baseline" / a.file).read_text(encoding="utf-8")  # the sealed baseline text of that file
            an = langs.analyze(a.file, text, man.get("frozen"))  # analyse it with the same frozen lines as the loop
            _, _, miss = an.coverage()  # uncovered required line numbers in the baseline
            miss = set(miss)  # a set for fast membership tests in the loop below
            lo, hi = a.from_ or 1, a.to or len(an.lines)  # requested line range, defaulting to the whole file
            # header line explaining the flag letters
            print(f";; {a.file} ({an.lang}) baseline lines {lo}-{hi}. Flags: R needs a comment, c already explained, "
                  "^ comment may go above, > comment may go at the end; '-' exempt (reason in brackets)")
            for ln in an.lines[lo - 1:hi]:  # the selected slice of lines (1-based, inclusive range)
                if a.todo and ln.n not in miss:  # when only unexplained lines were requested, skip the rest
                    continue  # skip lines that are already explained or exempt
                # Three flag characters: R (needs a comment), c (required but already explained) or - (exempt); then ^
                # when a comment may go above and > when one may go at the end.
                flag = ("R" if ln.n in miss else "c" if ln.required else "-") + ("^" if ln.above else " ") + \
                    (">" if ln.trail else " ")  # the end-comment flag character, or a space
                # exemption reason in brackets for exempt non-blank lines
                why = f"  [{ln.why_exempt}]" if not ln.required and ln.why_exempt and not ln.blank else ""
                print(f"{ln.n:>5} {flag} | {ln.text}{why}")  # line number, flags, the line itself and any reason
            return 0  # show always succeeds once the file is found
        if a.code_cmd == "check":  # check: apply maps in memory and report the outcome
            return 0 if _print_check(cm.check(d, a.files)) else 1  # exit code 0 when every file is done, 1 otherwise
        if a.code_cmd == "apply":  # apply: write baseline plus maps into the repository
            cm.apply(d, a.files or None)  # no files named means every file that has a map
            return 0  # apply succeeded; failures raise LoopError instead
        if a.code_cmd == "measure":  # measure: analyse files, run tests, write a measurement
            # the note labels the measurement; tests can be skipped
            out = cm.measure(d, a.note or "", tests=not a.no_tests)
            print(f"wrote {out}")  # report where the measurement file was written
            return 0  # measuring itself never fails the command
        if a.code_cmd == "decide":  # decide: judge the latest measurement against the rubric
            sealed = loop.load_sealed(d)  # the sealed rubric, verified against its stored hash
            # newest measurement by file name; IndexError when there is none
            mpath = sorted(d.glob("measure-*.json"))[-1]
            # apply the loop's fixed rules against the history
            entry = loop.decide(sealed, json.loads(mpath.read_text(encoding="utf-8")), loop.read_log(d))
            entry["change"] = a.change or ""  # describe the change that this round made
            entry["measurement"] = mpath.name  # remember which measurement file was judged
            loop.append_log(d, entry)  # append the decision to the loop's log file
            # Summary: loop number, PROMOTE or VETO, score, gate state and the change description.
            print(f"loop {entry['loop']}: {entry['decision'].upper()}  score {entry['score']}  "
                  # gate state and the change description in brackets
                  f"gates {'ok' if entry['gates_ok'] else 'FAILED'}  ({entry['change']})")
            for name, row in entry["rows"].items():  # one row per rubric metric with its value and requirement
                # pass or fail, metric name, value and the threshold needed
                print(f"  {'ok  ' if row['pass'] else 'FAIL'} {name:<22} {str(row['value']):<10} need {row['need']}")
            for r in entry["reasons"]:  # every reason that led the loop to a veto
                print(f"  veto reason: {r}")  # printed under the summary, one per line
            return 0 if entry["decision"] == "promote" else 1  # exit code 0 for promote and 1 for veto
        if a.code_cmd == "revert":  # revert: undo the most recent apply
            cm.revert(d)  # restore files from the newest snapshot folder
            return 0  # success once the snapshot was restored
        if a.code_cmd == "status":  # status: rubric, size of the run and loop history
            s = loop.load_sealed(d)  # the sealed rubric with its name, time and hash
            man = cm.load_manifest(d)  # the manifest with files and required line counts
            # rubric name, seal time and the first sixteen hash characters
            print(f"rubric {s['rubric'].get('name', '(unnamed)')!r} sealed {s['sealed_at']} sha256 {s['sha256'][:16]}…; "
                  # number of files and required lines at the seal
                  f"{len(man['files'])} files, {sum(e['required'] for e in man['files'])} required lines at the seal")
            for h in loop.read_log(d):  # one output line per logged round of the loop
                cov = h["rows"].get("coverage", {}).get("value")  # coverage value recorded for that round, if present
                # round number, decision, score, coverage and timestamp
                print(f"  loop {h['loop']:>2}  {h['decision']:<7} score {h['score']}  coverage {cov}  {h['at']}  "
                      # the change description and any veto reasons
                      f"{h.get('change', '')}" + (f"  ({'; '.join(h['reasons'])})" if h["reasons"] else ""))
            return 0  # status only reads files, so it always succeeds
        if a.code_cmd == "report":  # report: export per-file coverage of the latest measurement
            mpath = sorted(d.glob("measure-*.json"))[-1]  # the newest measurement file by its zero-padded name
            m = json.loads(mpath.read_text(encoding="utf-8"))  # the measurement contents parsed into a dictionary
            log = loop.read_log(d)  # the decision log, to include the latest decision
            # measurement name, time, metrics and totals
            out = {"measurement": mpath.name, "measured_at": m["measured_at"], "metrics": m["metrics"],
                   # the latest decision, or None before the first one
                   "totals": m["totals"], "decision": (log[-1]["decision"] if log else None),
                   # per-file subset: language, counts, identity, parse, comments
                   "files": {r: {k: f[k] for k in ("lang", "required", "covered", "identity_ok", "ok",
                                                   # only fields present in that measurement's file record
                                                   "comments_added") if k in f} for r, f in m["files"].items()}}
            # write the report as indented UTF-8 JSON
            Path(a.out).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
            print(f"wrote {a.out}")  # tell the user where the report was written
            return 0  # report written without problems
    # Expected failures (loop rules, missing tools, unreadable files, bad JSON, no measurement yet) become one line on
    # stderr instead of a traceback.
    except (loop.LoopError, RuntimeError, OSError, json.JSONDecodeError, IndexError) as e:
        print(f"emblem-rsi: {e}", file=sys.stderr)  # prefixed with the program name, written to standard error
        return 2  # exit code 2 marks an error rather than a verdict
    return 2  # an unknown subcommand also counts as an error


# Add the `code` command and its subcommands to the main emblem-rsi parser. sub is the subparsers object of the
# top-level argparse parser (emblem_rsi/cli.py calls this inside a try block). Returns nothing; every subcommand is
# dispatched to main_cmd.
def register(sub) -> None:
    # the `code` command with a one-line summary for help
    p = sub.add_parser("code", help="code-comment audit: seal, show, check, apply, measure, decide, revert, status")
    cs = p.add_subparsers(dest="code_cmd", required=True)  # a subcommand is mandatory; its name lands in code_cmd

    # Give a subcommand parser the shared --dir option and route it to main_cmd. x is the subcommand's parser; it is
    # returned so that more options can be added.
    def with_dir(x):
        # run directory option shared by every subcommand
        x.add_argument("--dir", required=True, help="run directory (sealed rubric, baseline, maps, measurements, log)")
        x.set_defaults(fn=main_cmd)  # every subcommand is handled by main_cmd
        return x  # hand the parser back for further options
    s = with_dir(cs.add_parser("seal"))  # seal subcommand with the shared run directory option
    s.add_argument("--repo", required=True)  # repository root to audit, required for sealing
    s.add_argument("--rubric")  # optional rubric file instead of the default one
    # repeatable test command option, collected into a list
    s.add_argument("--test", action="append", help="a test command (JSON list or a plain command line)")
    # repeatable FILE:lines option for frozen lines
    s.add_argument("--frozen", action="append", help="FILE:1,2,3 lines that must stay exactly as they are")
    s = with_dir(cs.add_parser("show"))  # show subcommand: list baseline lines with their flags
    s.add_argument("file")  # the repository-relative file to show
    s.add_argument("--from", dest="from_", type=int)  # first line of the range; 'from' is a Python keyword, hence from_
    s.add_argument("--to", type=int)  # last line of the range to show
    # only the lines that still need a comment
    s.add_argument("--todo", action="store_true", help="only lines that still need a comment")
    s = with_dir(cs.add_parser("check"))  # check subcommand: dry run of the comment maps
    s.add_argument("files", nargs="+")  # one or more files to check in a single run
    s = with_dir(cs.add_parser("apply"))  # apply subcommand: write maps into the repository
    s.add_argument("files", nargs="*")  # zero or more files; none means every mapped file
    s = with_dir(cs.add_parser("measure"))  # measure subcommand: write a new measurement file
    s.add_argument("--note")  # free-text note stored in the measurement
    s.add_argument("--no-tests", action="store_true")  # skip running the test commands entirely
    s = with_dir(cs.add_parser("decide"))  # decide subcommand: promote or veto the latest measurement
    s.add_argument("--change")  # description of the change being judged
    with_dir(cs.add_parser("revert"))  # revert subcommand needs only the run directory
    with_dir(cs.add_parser("status"))  # status subcommand takes no options beyond the run directory
    s = with_dir(cs.add_parser("report"))  # report subcommand: export per-file coverage
    s.add_argument("--out", required=True)  # output file path for the JSON report
