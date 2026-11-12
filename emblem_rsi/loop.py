# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""The emblem-rsi audit loop: seal a rubric, measure a page against it, then promote or veto.

Why it is built this way
  * The rubric is sealed (hashed and timestamped) before any measurement, so a rubric cannot be bent to fit a result.
  * `measure` refuses to run without a sealed rubric and records the rubric hash it used.
  * `decide` re-checks that hash, then applies fixed rules. Anything not measured counts as failed.
  * Every loop is appended to log.jsonl, so the history can be audited later.

This is one reading of "RSI" (recursive self-improvement audit loop). The names and rules are small and explicit
so they can be changed once the intended meaning is confirmed.
"""
from __future__ import annotations  # lets annotations such as `Path | None` work on every supported Python 3

import argparse  # builds the `loop` subcommands and their options
import datetime as dt  # UTC timestamps for seals, measurements and log entries
import hashlib  # SHA-256 hashing of rubrics, pages, signatures and the probe script
import json  # rubrics, measurements and the log are all JSON
import os  # copy of the environment for the node process
import subprocess  # runs node with the browser probe
import sys  # standard error stream for one-line failure messages
from pathlib import Path  # Path objects for the run folder, page, signature and measurement files on any OS

RUBRIC_SCHEMA = "emblem-rsi.rubric/1"  # format tag a rubric file must carry before it can be sealed
SEALED_SCHEMA = "emblem-rsi.sealed/1"  # format tag of the sealed copy (rubric plus hash and time)
MEASURE_SCHEMA = "emblem-rsi.measurement/1"  # format tag stamped onto every measurement the loop accepts
HERE = Path(__file__).resolve().parent  # folder holding this module, used to find bundled rubrics and scripts
DEFAULT_RUBRIC = HERE / "rubrics" / "viscous-letters.json"  # rubric used by `loop seal` when no --rubric is given
# Path to the headless-browser probe script run by node.
MEASURE_JS = HERE / "js" / "measure.mjs"   # the probe; its hash is recorded with every measurement


# The one exception type for broken loop rules: no seal, edited seal, unknown file format, failed probe.
# Command handlers catch it and print the message instead of a traceback.
class LoopError(Exception):  # plain Exception subclass so callers can catch loop problems specifically
    """A rule of the loop was broken. The message says which, in plain words."""


# Current UTC time as an ISO 8601 string with whole seconds, e.g. 2026-10-28T18:20:04+00:00.
# Takes no parameters. Used for seal, measurement and log timestamps; strings in this format sort by time.
def now() -> str:
    # timezone-aware UTC so timestamps from different machines compare correctly
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# Serialise any JSON-compatible object to bytes in one fixed form: keys sorted, no spaces.
# Parameter obj is a dict, list or scalar. Returns UTF-8 bytes. Two equal objects always give identical bytes,
# so their hashes match regardless of key order or the indentation used in the file on disk.
def canonical(obj) -> bytes:
    # sort_keys and compact separators remove every formatting difference before hashing
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


# SHA-256 of a bytes value, returned as 64 lowercase hex characters.
# Parameter b is the data. Used for the rubric seal and for combined fingerprints.
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()  # hexdigest gives the printable form stored in JSON files


# SHA-256 of a whole file's contents. Parameter p is a path (str or Path); returns the hex digest.
# Reads the file in one go, which is fine for pages, signatures and scripts of a few megabytes.
def sha256_file(p: Path) -> str:
    return sha256_bytes(Path(p).read_bytes())  # hash the raw bytes, so any edit, even whitespace, changes the result


# ---------- rubric ----------
# Check that a rubric dictionary has the structure the loop relies on, before it can be sealed.
# Parameter r is the parsed rubric JSON. Returns nothing; raises LoopError naming the first problem found.
# Rules: the right schema, a non-empty name (the CLI prints it after sealing), at least one metric, each with op "min" or "max", a numeric threshold and a positive
# weight, and every guard naming an existing metric. Nothing is written.
def validate_rubric(r: dict) -> None:
    if r.get("schema") != RUBRIC_SCHEMA:  # refuse files that are not emblem-rsi rubrics
        raise LoopError(f"rubric schema must be {RUBRIC_SCHEMA!r}")  # !r shows the expected tag in quotes
    if not isinstance(r.get("name"), str) or not r["name"].strip():  # the seal and status commands print the name
        raise LoopError("rubric needs a non-empty \"name\"")  # refuse before anything is sealed
    m = r.get("metrics")  # the metrics table: name -> {op, value, weight?, gate?}
    if not isinstance(m, dict) or not m:  # a rubric must define at least one metric to measure
        raise LoopError("rubric has no metrics")  # refuse an empty or malformed metrics table
    for k, v in m.items():  # check every metric definition in the table
        # op says whether higher ("min") or lower ("max") is better; value is the pass threshold
        if v.get("op") not in ("min", "max") or not isinstance(v.get("value"), (int, float)):
            # name the offending metric and what it is missing
            raise LoopError(f"metric {k!r} needs op 'min' or 'max' and a numeric value")
        # weight scales the metric's share of the score; it defaults to 1 and must be above zero
        if not isinstance(v.get("weight", 1), (int, float)) or v.get("weight", 1) <= 0:
            # zero or negative weights would distort or invert the score
            raise LoopError(f"metric {k!r} needs a positive weight")
    for g in r.get("guards", []):  # guards are metrics that must not get worse between promoted loops
        if g not in m:  # a guard has to refer to one of the metrics above
            raise LoopError(f"guard {g!r} is not a metric")  # catch typos in guard names before sealing


# Seal a rubric: validate it, then write it with its canonical hash and a timestamp into the run directory.
# Parameters: rubric_path is the rubric JSON file; run_dir is the folder for this run (created if missing).
# Returns the sealed dictionary. Writes RUN/rubric.sealed.json and refuses to overwrite an existing seal,
# so the rules cannot be changed after measurements have been seen.
def seal(rubric_path: Path, run_dir: Path) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)  # make the run folder, including parents, if it does not exist yet
    out = run_dir / "rubric.sealed.json"  # fixed name every other command looks for
    if out.exists():  # one seal per run directory, forever
        # a new rubric needs a new run directory, so the history stays honest
        raise LoopError(f"{out} already exists. A sealed rubric is never overwritten; start a new run directory for a new rubric.")
    rubric = json.loads(Path(rubric_path).read_text(encoding="utf-8"))  # read and parse the rubric file
    validate_rubric(rubric)  # raise LoopError if the rubric is malformed
    # record format tag, UTC time, hash of the canonical rubric bytes and the rubric itself
    sealed = {"schema": SEALED_SCHEMA, "sealed_at": now(), "sha256": sha256_bytes(canonical(rubric)), "rubric": rubric}
    # indented JSON so a person can read the sealed rules
    out.write_text(json.dumps(sealed, indent=2) + "\n", encoding="utf-8")
    return sealed  # callers print the name, hash and time from it


# Load the run's sealed rubric and verify it has not been edited since sealing.
# Parameter run_dir is the run folder. Returns the sealed dict. Raises LoopError when there is no seal yet
# or when the stored hash no longer matches the rubric content. Every measure and decide goes through here.
def load_sealed(run_dir: Path) -> dict:
    p = run_dir / "rubric.sealed.json"  # location of the seal written by seal()
    if not p.exists():  # nothing may be measured or judged without a seal
        # point the user to the command that creates it
        raise LoopError(f"no sealed rubric in {run_dir}. Run `emblem-rsi loop seal` first; nothing may be measured before the rubric is sealed.")
    s = json.loads(p.read_text(encoding="utf-8"))  # parse the sealed rubric file from JSON
    # recompute the canonical hash and compare it with the one stored at sealing time
    if sha256_bytes(canonical(s["rubric"])) != s["sha256"]:
        # any edit to the rules after sealing is caught here
        raise LoopError("the sealed rubric has been edited since it was sealed (hash mismatch)")
    return s  # verified sealed rubric, safe to use for decisions


# ---------- scoring ----------
# Partial credit for one metric, from 0 to 1: full credit on a pass, otherwise how close the value came.
# Parameters: op is "min" or "max", threshold the pass value, value the measured number or None.
# For "min" the credit is value / threshold; for "max" it is threshold / value; unmeasured values earn 0.
def credit(op: str, threshold: float, value) -> float:
    """1.0 when the metric passes, otherwise how close it came (0..1). Unmeasured is 0."""
    if value is None:  # a metric the probe did not report earns nothing, so skipping a measurement never helps
        return 0.0  # zero credit, the same as a total failure
    v = float(value)  # measurements may arrive as int, float or numeric string
    if op == "min":  # "min" metrics: higher values are better
        # Pass gives 1.0; otherwise the ratio, floored at 0; a threshold of zero or below gives 0 for a miss.
        # conditional expression chain: pass, partial ratio, or zero
        return 1.0 if v >= threshold else max(0.0, v / threshold) if threshold > 0 else 0.0
    # lower is better: pass gives 1.0, a miss gives threshold / value, and a non-positive value gives 0
    return 1.0 if v <= threshold else max(0.0, threshold / v) if v > 0 else 0.0


# Whether one metric passes its threshold. Parameters as for credit(); returns a bool, False when unmeasured.
def passes(op: str, threshold: float, value) -> bool:
    # None fails; otherwise compare with >= for "min" metrics and <= for "max" metrics
    return value is not None and (float(value) >= threshold if op == "min" else float(value) <= threshold)


# Score a set of measured metrics against the rubric.
# Parameters: rubric is the rubric dict (not the sealed wrapper); metrics maps metric name to value.
# Returns {"score": weighted mean credit 0..1, "gates_ok", "failed_gates", "rows": per-metric details}.
# A gate is a metric that must pass outright, whatever the overall score is.
def score(rubric: dict, metrics: dict) -> dict:
    # sum of all weights, used to turn weighted credit into a 0..1 score
    total = sum(m.get("weight", 1) for m in rubric["metrics"].values())
    # running weighted credit, names of failed gate metrics, and the per-metric table
    got, failed_gates, rows = 0.0, [], {}
    # walk the rubric's metrics; metrics the page reports but the rubric lacks are ignored
    for name, spec in rubric["metrics"].items():
        val = metrics.get(name)  # None when the probe did not measure this metric
        c = credit(spec["op"], spec["value"], val)  # partial credit between 0 and 1 for this metric
        ok = passes(spec["op"], spec["value"], val)  # strict pass or fail against the metric threshold
        got += c * spec.get("weight", 1)  # weight the credit; heavier metrics move the score more
        # what was measured, what was required (e.g. "min 0.85"), pass flag and rounded credit
        rows[name] = {"value": val, "need": f"{spec['op']} {spec['value']}", "pass": ok, "credit": round(c, 4)}
        if spec.get("gate") and not ok:  # a metric marked as a gate must pass on its own
            failed_gates.append(name)  # remember it; any failed gate vetoes the loop
    # weighted mean credit to 4 decimals, plus the gate summary and the detail rows
    return {"score": round(got / total, 4), "gates_ok": not failed_gates, "failed_gates": failed_gates, "rows": rows}


# ---------- log and decision ----------
# Read the run's history: one decision record per line of RUN/log.jsonl.
# Parameter run_dir is the run folder. Returns a list of dicts, oldest first, or [] when there is no log yet.
def read_log(run_dir: Path) -> list[dict]:
    p = run_dir / "log.jsonl"  # the history file sits next to the seal in the run directory
    # JSON Lines: parse each non-blank line; a missing log is an empty history
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


# Apply the fixed rules to one measurement and return the log entry with decision "promote" or "veto".
# Parameters: rubric_sealed is the verified seal, measurement the probe output dict, history the earlier
# log entries. Pure function: nothing is written. Every broken rule adds a plain-language reason; any reason vetoes.
def decide(rubric_sealed: dict, measurement: dict, history: list[dict]) -> dict:
    """Apply the fixed rules. Returns the log entry (decision is 'promote' or 'veto')."""
    rubric = rubric_sealed["rubric"]  # the rules themselves, without the seal wrapper
    reasons: list[str] = []  # veto reasons collected below; an empty list means promote
    if measurement.get("schema") != MEASURE_SCHEMA:  # only files produced by the loop's measure step are accepted
        raise LoopError("not an emblem-rsi measurement file")  # anything else is a usage error, not a veto
    # the measurement must have been made against this exact sealed rubric
    if measurement.get("rubric_sha256") != rubric_sealed["sha256"]:
        # a stale measurement from another rubric is vetoed
        reasons.append("the measurement was made against a different rubric than the sealed one")
    # ISO timestamps in UTC compare correctly as strings; a missing time sorts first and is vetoed
    if measurement.get("measured_at", "") < rubric_sealed["sealed_at"]:
        # a measurement from before the seal could have influenced the rules
        reasons.append("the measurement is older than the rubric seal")
    sc = score(rubric, measurement.get("metrics", {}))  # score the measured metrics against the rubric
    # only promoted loops set the bar; vetoed ones never count
    promoted = [h for h in history if h["decision"] == "promote"]
    best = max((h["score"] for h in promoted), default=None)  # best score so far, or None for the first promotion
    # the full entry of the best promoted loop, used for the guard comparison
    best_row = max(promoted, key=lambda h: h["score"]) if promoted else None
    if not sc["gates_ok"]:  # any failed gate is a veto, however high the score
        reasons.append("gate failed: " + ", ".join(sc["failed_gates"]))  # list the failed gate names
    # same page and same signature as the most recent promotion means nothing actually changed
    if promoted and measurement.get("page_sha256") == promoted[-1]["page_sha256"] and measurement.get("signature_sha256") == promoted[-1].get("signature_sha256"):
        # re-measuring unchanged inputs must not be counted as progress
        reasons.append("the page and signature are unchanged since the last promoted loop, so there is nothing new to judge")
    # a new promotion must beat the best score by at least the rubric's min_gain
    if best is not None and sc["score"] < best + rubric.get("min_gain", 0):
        # state the score, the required margin and the current best
        reasons.append(f"score {sc['score']} is not at least {rubric.get('min_gain', 0)} above the best promoted score {best}")
    if best_row is not None:  # guards apply only once there is a promoted loop to compare against
        slack = rubric.get("guard_slack", 0.02)  # tolerance before a guard counts as worse; defaults to 0.02
        for g in rubric.get("guards", []):  # check each guard metric listed by the rubric
            # the guard's spec, its value in the best promoted loop and its value now
            spec, old, new = rubric["metrics"][g], best_row["rows"].get(g, {}).get("value"), sc["rows"][g]["value"]
            if old is None or new is None:  # a guard that was not measured on one side cannot be compared
                continue  # skip this guard without counting it as worse
            # worse means below old minus slack (for "min") or above old plus slack (for "max"); slack is relative above
            # 1
            worse = (new < old - slack * max(1.0, abs(old))) if spec["op"] == "min" else (new > old + slack * max(1.0, abs(old)))
            # a guard dropping while the total rises suggests the probe was gamed rather than the page improved
            if worse and sc["score"] > best_row["score"]:
                # name the guard and both values, and say why it is suspicious
                reasons.append(f"guard {g} got worse ({old} -> {new}) while the score rose; that looks like a probe artefact, not a real gain")
            elif worse:  # a guard regression with no score gain is a plain veto
                reasons.append(f"guard {g} got worse ({old} -> {new})")  # report the old and new values
    # The log entry: loop number, time, decision and reasons, scores and per-metric rows, and the hashes of the page,
    # signature, rubric and probe, so any decision can be audited and reproduced later.
    entry = {
        # loop number continues the log; any reason at all means veto
        "loop": len(history) + 1, "at": now(), "decision": "veto" if reasons else "promote", "reasons": reasons,
        # overall score, gate result and metric rows from score()
        "score": sc["score"], "gates_ok": sc["gates_ok"], "rows": sc["rows"],
        # fingerprints of the page and signature that were measured
        "page_sha256": measurement.get("page_sha256"), "signature_sha256": measurement.get("signature_sha256"),
        # rubric and probe fingerprints, plus the best promoted score before this loop
        "rubric_sha256": rubric_sealed["sha256"], "probe_sha256": measurement.get("probe_sha256"), "best_before": best,
    }
    return entry  # the caller appends the entry to the history log


# Append one decision record to RUN/log.jsonl. Parameters: run_dir is the run folder, entry the dict from decide().
# Returns nothing. The log is append-only, so earlier decisions are never rewritten.
def append_log(run_dir: Path, entry: dict) -> None:
    with open(run_dir / "log.jsonl", "a", encoding="utf-8") as f:  # append mode creates the file on first use
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")  # one compact JSON object per line (JSON Lines)


# ---------- measurement (runs the page in a headless browser through node) ----------
# Run the browser probe on a page and store its metrics, stamped with the hashes the loop needs.
# Parameters: page is the HTML file, signature an optional signature JSON, run_dir the run folder, node_modules a
# folder holding puppeteer-core and @sparticuz/chromium (or None). Returns the path of RUN/measure-NNN.json.
# Side effects: starts node (up to 10 minutes) and writes the measurement file. Raises LoopError on any failure.
def measure(page: Path, signature: Path | None, run_dir: Path, node_modules: str | None) -> Path:
    sealed = load_sealed(run_dir)  # refuses to run before the rubric is sealed, and verifies the seal
    if not Path(page).is_file():  # fail early with a clear message for a mistyped page path
        raise LoopError(f"page {page} does not exist")  # the probe would otherwise fail with a less helpful error
    # next measurement number from the count of existing files (a deleted file could make numbers repeat)
    n = len(list(run_dir.glob("measure-*.json"))) + 1
    out = run_dir / f"measure-{n:03d}.json"  # zero-padded so names sort in order up to 999
    env = dict(os.environ)  # pass the current environment through to node
    if node_modules:  # optional folder override for the node packages
        # measure.mjs reads this variable to find puppeteer-core and chromium
        env["EMBLEM_RSI_NODE_MODULES"] = node_modules
    cmd = ["node", str(MEASURE_JS), "--page", str(page), "--out", str(out)]  # node measure.mjs --page PAGE --out OUT
    if signature:  # the signature is optional; without it the signature metrics are recorded as null
        # the probe replays the signature in the page and scores loudness correlation and pitch error
        cmd += ["--signature", str(signature)]
    try:  # missing node is turned into a LoopError below
        # run node, capture its output as UTF-8 text, and stop it after 600 s
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:  # the node executable itself is not available on PATH
        # explain the missing node installation in plain words
        raise LoopError("node is not installed or not on PATH") from e
    except subprocess.TimeoutExpired as e:  # the browser probe hung past its time limit
        # explains the 600-second timeout passed to subprocess.run above
        raise LoopError("the browser measurement took longer than 10 minutes") from e
    if r.returncode != 0 or not out.exists():  # a non-zero exit, or no output file, means the probe failed
        # show the last 1500 characters of stderr (or stdout) so the cause is visible but not overwhelming
        raise LoopError("browser measurement failed:\n" + (r.stderr or r.stdout).strip()[-1500:])
    m = json.loads(out.read_text(encoding="utf-8"))  # read the metrics and details the probe wrote
    # Stamp the measurement: schema, the sealed rubric hash, the time, and fingerprints of the page, the signature
    # and the probe script, so decide() can check what was measured and with which rules.
    m.update({"schema": MEASURE_SCHEMA, "rubric_sha256": sealed["sha256"], "measured_at": now(), "page_sha256": sha256_file(page),
              # signature hash is None when no signature was used; the probe hash catches edits to measure.mjs
              "signature_sha256": sha256_file(signature) if signature else None, "probe_sha256": sha256_file(MEASURE_JS)})
    # rewrite the file with the stamps, indented for reading
    out.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    return out  # path of the finished, stamped measurement file


# ---------- command line ----------
# The run directory given with --dir, as a Path. Parameter a is the argparse namespace.
def _dir(a) -> Path:
    return Path(a.dir)  # every loop subcommand requires --dir


# Handler for `loop seal --dir RUN [--rubric FILE]`: seal a rubric into the run directory.
# Parameter a is the argparse namespace. Returns 0 on success, 2 on any problem; writes rubric.sealed.json.
def cmd_seal(a) -> int:
    try:  # collect the expected failures in one place
        # the given rubric, or the bundled viscous-letters rubric
        s = seal(Path(a.rubric) if a.rubric else DEFAULT_RUBRIC, _dir(a))
    # rule violations, unreadable files and invalid JSON all become a one-line error
    except (LoopError, OSError, json.JSONDecodeError) as e:
        print(f"emblem-rsi: {e}", file=sys.stderr); return 2  # print the error message and exit with status 2
    # name, first 16 hex characters of the hash and the sealing time (validate_rubric guarantees the "name")
    print(f"sealed rubric {s['rubric']['name']!r}  sha256 {s['sha256'][:16]}…  at {s['sealed_at']}")
    return 0  # exit status 0: the run directory is sealed


# Handler for `loop measure --dir RUN --page PAGE [--signature SIG] [--node-modules DIR]`.
# Parameter a is the namespace. Runs the browser probe and prints the measurement path. Returns 0 or 2.
def cmd_measure(a) -> int:
    try:  # only LoopError is translated here; other errors surface as a traceback
        # optional signature path, None when not given
        out = measure(Path(a.page), Path(a.signature) if a.signature else None, _dir(a), a.node_modules)
    except LoopError as e:  # loop rule broken or the probe failed
        # report the failure on standard error and exit with status 2
        print(f"emblem-rsi: {e}", file=sys.stderr); return 2
    print(f"wrote {out}")  # where the new measure-NNN.json was written
    return 0  # exit status 0: the measurement is ready for decide


# Shared by `loop decide` and `loop run`: judge one measurement, log the decision and print a report.
# Parameters: run_dir is the run folder, mpath the measurement JSON. Returns 0 for promote, 1 for veto.
# Side effect: appends the entry to RUN/log.jsonl.
def _decide_and_print(run_dir: Path, mpath: Path) -> int:
    sealed = load_sealed(run_dir)  # verify the seal before judging anything
    # apply the fixed rules using the full history
    entry = decide(sealed, json.loads(mpath.read_text(encoding="utf-8")), read_log(run_dir))
    append_log(run_dir, entry)  # record the decision, whether promote or veto
    # headline: loop number, decision in capitals, score and gate state
    print(f"loop {entry['loop']}: {entry['decision'].upper()}  score {entry['score']}  gates {'ok' if entry['gates_ok'] else 'FAILED'}")
    for name, row in entry["rows"].items():  # print one line per rubric metric
        # pass mark, metric name padded to 22 characters, measured value and requirement
        print(f"  {'ok  ' if row['pass'] else 'FAIL'} {name:<22} {str(row['value']):<10} need {row['need']}")
    for r in entry["reasons"]:  # then list every veto reason recorded by the rules
        print(f"  veto reason: {r}")  # each veto reason on its own indented line
    return 0 if entry["decision"] == "promote" else 1  # exit status 0 only for a promotion, so scripts can branch on it


# Handler for `loop decide --dir RUN [--measurement FILE]`: judge the latest or a given measurement.
# Parameter a is the namespace. Returns 0 promote, 1 veto, 2 error.
def cmd_decide(a) -> int:
    try:  # expected failures are caught below
        d = _dir(a)  # the run folder given with --dir
        # explicit file, or the newest measure-NNN.json by name
        mpath = Path(a.measurement) if a.measurement else sorted(d.glob("measure-*.json"))[-1]
        return _decide_and_print(d, mpath)  # judge the measurement, append the log entry, and print
    # IndexError means the glob found no measurement files
    except (LoopError, IndexError, OSError, json.JSONDecodeError) as e:
        # give a helpful message for that case and the raw message otherwise; exit 2
        print(f"emblem-rsi: {e if not isinstance(e, IndexError) else 'no measurement found; run `loop measure` first'}", file=sys.stderr); return 2


# Handler for `loop run`: measure and decide in one step. Parameter a is the namespace. Returns 0, 1 or 2.
def cmd_run(a) -> int:
    try:  # group the expected errors from both steps
        # run the probe and keep the measurement path
        out = measure(Path(a.page), Path(a.signature) if a.signature else None, _dir(a), a.node_modules)
        return _decide_and_print(_dir(a), out)  # then judge the fresh measurement immediately
    except (LoopError, OSError, json.JSONDecodeError) as e:  # rule broken, file problem or bad JSON
        print(f"emblem-rsi: {e}", file=sys.stderr); return 2  # one-line error message on standard error, then status 2


# Handler for `loop status --dir RUN`: show the sealed rubric and every logged decision.
# Parameter a is the namespace. Returns 0, or 2 when there is no valid seal. Writes nothing.
def cmd_status(a) -> int:
    try:  # the seal must exist and be intact for status
        s = load_sealed(_dir(a))  # verify the seal and load the sealed rubric
    except LoopError as e:  # no seal yet, or it was edited
        print(f"emblem-rsi: {e}", file=sys.stderr); return 2  # report the missing or edited seal and exit 2
    # rubric name (a seal made before names were required has none), sealing time and a hash prefix
    print(f"rubric {s['rubric'].get('name', '(unnamed)')!r} sealed {s['sealed_at']} sha256 {s['sha256'][:16]}…")
    for h in read_log(_dir(a)):  # each logged decision in log order, oldest first
        # loop number, decision padded to 7 characters, score and time, with veto reasons in brackets
        print(f"  loop {h['loop']:>2}  {h['decision']:<7} score {h['score']}  {h['at']}" + (f"  ({'; '.join(h['reasons'])})" if h["reasons"] else ""))
    return 0  # status only reads; it always ends with 0


# Add the `loop` command group to the main parser. Parameter sub is the subparsers object from cli.main.
# Returns nothing; each subcommand stores its handler as fn, which cli.main calls.
def register(sub) -> None:
    # the `loop` command group with its help summary
    p = sub.add_parser("loop", help="sealed-rubric audit loop: seal, measure, decide")
    ls = p.add_subparsers(dest="loop_cmd", required=True)  # loop's own subcommands; choosing one is mandatory

    # Add the options shared by loop subcommands: --dir always, and the page options when page=True.
    def common(x, page=False):
        # the run folder holds the seal, the measurements and the log
        x.add_argument("--dir", required=True, help="run directory (holds the sealed rubric, measurements and log)")
        if page:  # only measure and run need a page to test
            # page HTML (required), optional signature JSON, and the folder holding the node packages
            x.add_argument("--page", required=True); x.add_argument("--signature"); x.add_argument("--node-modules", help="folder holding puppeteer-core and @sparticuz/chromium")

    # `loop seal` takes only the run directory and an optional rubric
    s = ls.add_parser("seal", help="hash and timestamp a rubric before anything is measured"); common(s)
    # rubric file path; the handler falls back to the bundled one
    s.add_argument("--rubric", help="rubric JSON (default: the bundled viscous-letters rubric)"); s.set_defaults(fn=cmd_seal)
    # `loop measure` needs the page options
    m = ls.add_parser("measure", help="run the page in a headless browser and record metrics"); common(m, True); m.set_defaults(fn=cmd_measure)
    # `loop decide` takes the run directory
    d = ls.add_parser("decide", help="promote or veto the latest (or given) measurement"); common(d)
    d.add_argument("--measurement"); d.set_defaults(fn=cmd_decide)  # optional explicit measurement file
    # `loop run` measures and decides in one step
    r = ls.add_parser("run", help="measure then decide"); common(r, True); r.set_defaults(fn=cmd_run)
    # `loop status` prints the rubric and history
    st = ls.add_parser("status", help="show the sealed rubric and the loop history"); common(st); st.set_defaults(fn=cmd_status)
