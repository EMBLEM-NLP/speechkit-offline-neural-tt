# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""speechkit-voice-tts-offline-pipeline command line.  python -m speechkit_narrate [--json] <command> ...

Projects
  new       DIR --input FILE [--title ...]    make a project folder from a .txt, .md or request .json file
  run       DIR [STAGE ...]                   bring stages up to date (only what is missing or changed runs)
  plan      DIR [STAGE ...]                   what `run` would do, and why
  all       DIR                               the whole pipeline (same as run; --from, --skip-audit)
  status    DIR                               what is done
  settings  DIR [--set JSON]                  show or change project.json (deep merge)
  <stage>   DIR [options]                     run one stage now, after anything stale it needs (extract, check,
                                              script, prevoice, narrate, autofix, audit, report, export, plug-ins);
                                              `run DIR --only S` skips the needs
  pron      DIR trial|apply|retake|revert|status|measure|decide ...   the audit loop's own commands

Toolkits
  stages    [DIR]                             every stage: needs, inputs, outputs, platforms, options, source
  tools                                       the standalone tools
  tool      NAME [--param value ...]          run a standalone tool on plain files (or --params JSON)
  mcp                                         MCP server on stdio (tools generated from the registry)

Machine
  doctor | setup                              check / install the Python packages, voice runtime and models

--json   print one JSON object on stdout (logs go to stderr): for scripts, PowerShell and other programs.
Plug-ins: SPEECHKIT_PLUGINS, --plugin FILE|MODULE, project.json "plugins", or the project's plugins/ folder.
"""
# Postpone evaluation of annotations, so hints like `dict | None` cost nothing at import time.
from __future__ import annotations

import argparse  # command-line parsing: sub-commands, options and generated help
import json  # --json output, --params / --set / --options values
import os  # file-descriptor juggling for --json and environment variables
import sys  # argv, stdout/stderr and exit codes
from pathlib import Path  # filesystem paths (detecting a project folder on the command line)
import time  # HH:MM:SS timestamps on log lines
import traceback  # full tracebacks for unexpected errors under --json

from . import env  # tool locations, `doctor` checks and `setup`
# the project folder class, its error type, and the function that makes a new project
from .project import Project, ProjectError, create
from .registry import RegistryError  # registry problems are reported like usage errors (exit code 2)
from . import registry as R  # the registry module itself under a short name: R.load, R.run, R.plan ...

# True while this process runs in --json mode: one JSON object on stdout, every log line on stderr.
# main() sets it; log(), emit() and the Parser read it.
JSON = False


# Print one progress line with a local HH:MM:SS timestamp. msg: the text. Under --json it goes to stderr so
# stdout stays reserved for the result object; otherwise it goes to stdout. Passed to stages as their logger.
def log(msg):
    # flush=True so callers piping the output (PowerShell, the MCP server) see each line at once
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr if JSON else sys.stdout, flush=True)


# Under --json this becomes a text stream on the real standard output (see protect_stdout); None means "use
# sys.stdout as it is".
RESULT = None            # where the one JSON object goes (the real stdout) in --json mode


# Under --json, make sure the one result line is the only thing on standard output. File descriptor 1 is pointed
# at stderr for the rest of the process, so prints from stages, libraries and child processes (ffmpeg, Chromium)
# all land in the log; the original stdout is kept as RESULT for result(). No parameters, returns nothing.
def protect_stdout() -> None:
    """--json: point file descriptor 1 at stderr for the whole command, so nothing a stage, a library or a child
    process prints can get mixed into the result; the result is written to a saved copy of the real stdout."""
    global RESULT  # rebinds the module-level RESULT
    sys.stdout.flush()  # push out anything Python has buffered before the descriptors change
    real = os.dup(1)  # a new descriptor that still refers to the real stdout
    os.dup2(2, 1)  # fd 1 now refers to stderr, for this process and every child it starts
    # a text stream on the saved descriptor; newline="\n" avoids \r\n on Windows
    RESULT = os.fdopen(real, "w", encoding="utf-8", newline="\n")


# Write the command's result as one compact JSON line. obj: a dict. Goes to the saved real stdout under --json,
# else to sys.stdout. Non-JSON values such as Paths are turned into text by default=str.
def result(obj: dict) -> None:
    out = RESULT or sys.stdout  # the protected stdout when it exists
    # ensure_ascii=False keeps non-English titles readable; one line per result
    out.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    out.flush()  # make it visible to the reading program right away


# Report a command's outcome. obj: the result (a dict, or any value); human: optional text for interactive use.
# Under --json the result becomes {"ok": true, ...}. Otherwise `human` is printed when given (an empty string
# prints an empty line), else the result is pretty-printed as JSON.
def emit(obj, human=None):
    if JSON:  # --json mode: wrap the result in one JSON object with ok=true for the calling program
        # a dict is merged with ok=true; anything else goes under "result"
        result({"ok": True, **obj} if isinstance(obj, dict) else {"ok": True, "result": obj})
    elif human is not None:  # interactive mode with a ready-made message
        print(human)  # print it as given
    else:  # interactive mode without a message
        print(json.dumps(obj, indent=1, ensure_ascii=False, default=str))  # readable JSON with one-space indentation


# --------------------------------------------------------------------------------------- option plumbing
# Turn JSON-schema properties (a stage's options or a tool's params) into argparse options on parser p.
# props: {name: schema}; required: names that must be given (accepted, but the check happens after parsing).
# Booleans get --x / --no-x, arrays may repeat, objects take JSON, numbers are converted. Returns nothing.
def add_schema_options(p: argparse.ArgumentParser, props: dict, required=()):
    for key, sch in props.items():  # one option per property
        flag = "--" + key.replace("_", "-")  # option spelling: workers -> --workers, out_prefix -> --out-prefix
        t = sch.get("type")  # the JSON type, a string or a list of types
        help_ = sch.get("description", "")  # help text from the schema description
        if t == "boolean":  # a yes/no option such as --dry-run; it becomes a pair of on and off flags
            # BooleanOptionalAction adds --x and --no-x; default None means "not given"
            p.add_argument(flag, dest=key, action=argparse.BooleanOptionalAction, default=None, help=help_)
        elif t == "array":  # a list option such as --targets; the flag may be given several times to add items
            # repeated flags collect into a list; commas are split later in collect()
            p.add_argument(flag, dest=key, action="append", default=None, help=help_ + " (repeat or comma-separate)")
        elif t == "object":  # a nested settings object, which the user types as one JSON string
            # parsed from a JSON string on the command line
            p.add_argument(flag, dest=key, type=json.loads, default=None, help=help_ + " (JSON)")
        else:  # strings, integers, numbers, and union types
            # integer -> int, number -> float; strings and unions (a list type) stay text
            conv = {"integer": int, "number": float}.get(t if isinstance(t, str) else "", str)
            # enum values become argparse choices
            p.add_argument(flag, dest=key, type=conv, default=None, choices=sch.get("enum"), help=help_,
                           # `and False` always gives False, so argparse never enforces required here (cmd_tool reports
                           # it instead)
                           required=key in required and False)


# Collect the parsed values of schema-defined options back into a plain dict. ns: the argparse namespace; props:
# the same properties given to add_schema_options. Options that were not given are left out, so the stage or
# tool defaults still apply. Returns {name: value}.
def collect(ns, props: dict) -> dict:
    out = {}  # the values that were actually given
    # only declared options are read; the namespace also holds unrelated fields such as dir and plugin
    for key, sch in props.items():
        v = getattr(ns, key, None)  # None means the option was not on the command line
        if v is None:  # skip it, so the key stays absent and the stage or tool falls back to its own default
            continue  # move on to the next declared option without storing anything for this one
        # Split every repeated value on commas too, so "--targets a,b --targets c" gives ["a", "b", "c"].
        if sch.get("type") == "array":
            items = [x for part in v for x in str(part).split(",") if x]  # drop empty pieces such as a trailing comma
            it = (sch.get("items") or {}).get("type")  # the declared item type
            # convert each item to the declared number type, else keep the text
            v = [float(x) if it == "number" else int(x) if it == "integer" else x for x in items]
        # union types such as creep: a number of points, or the word "auto"
        if sch.get("type") == ["number", "string"] or sch.get("type") == ["string", "number"]:
            try:  # a number if it looks like one
                v = float(v)  # e.g. "4.5" becomes 4.5
            except (TypeError, ValueError):  # "auto" is not a number
                pass  # so it stays text
        out[key] = v  # keep the converted value
    return out  # only what was given


# --------------------------------------------------------------------------------------- commands
# `speechkit-voice-tts-offline-pipeline stages [DIR]`: list every stage (needs, description, where it runs, plug-in source) and the tools.
# a: parsed arguments (dir, plugin). With a project folder its own plug-ins are included. Prints or emits JSON.
def cmd_stages(a):
    p = Project(a.dir) if a.dir else None  # the project, if one was named
    reg = R.load(p, a.plugin or ())  # built-ins plus plug-ins from --plugin and the project
    d = reg.describe()  # the whole registry as a dict
    if JSON:  # machine mode: the full description
        emit(d)  # the full registry description as one JSON object with ok=true, for PowerShell or another program
        return  # nothing else to print
    for s in d["stages"]:  # human listing, one line per stage
        # stages that cannot run here show their platforms, and whether a delegate will take them
        mark = "" if s["runs_here"] else f"  [{'/'.join(s['platforms'])}{', delegated' if d['delegate'] else ''}]"
        # name, needs and the first 70 characters of the description, padded into columns
        print(f"{s['name']:<10} needs {', '.join(s['needs']) or '-':<26} {s['description'][:70]}{mark}"
              # plug-in stages show where they came from
              + ("" if s["source"] == "speechkit-narrate" else f"  (plug-in {s['source']})"))
    print("\ntools: " + ", ".join(t["name"] for t in d["tools"]))  # one line naming the standalone tools


# `speechkit-voice-tts-offline-pipeline tools`: the standalone tools with their parameters. a: parsed arguments (plugin). Prints the
# listing, or emits {"tools": [...]} under --json.
def cmd_tools(a):
    reg = R.load(None, a.plugin or ())  # built-ins plus --plugin plug-ins; no project here
    tl = [t.info() for t in reg.tools.values()]  # each tool as a dict with its parameter schema
    if JSON:  # --json: a list of tool dicts
        emit({"tools": tl})  # the list as JSON
        return  # skip the human listing
    for t in tl:  # interactive mode: a heading line per tool, then an indented line per parameter
        req = t["params"]["required"]  # names of the required parameters
        print(f"{t['name']:<14} {t['description']}")  # tool name and description
        for k, v in t["params"]["properties"].items():  # one indented line per parameter
            # shown as a command-line flag, marked when required, with its description
            print(f"    --{k.replace('_', '-'):<14} {'(required) ' if k in req else ''}{v.get('description', v.get('type'))}")


# `speechkit-voice-tts-offline-pipeline tool NAME [--param value ...]` or `--params JSON`: run one standalone tool on plain files.
# a: parsed arguments; extra: the arguments argparse did not recognise (the tool's own options).
# Delegates the tool when this platform cannot run it. Emits {"tool": name, ...result}.
def cmd_tool(a, extra):
    reg = R.load(None, a.plugin or ())  # built-ins plus --plugin plug-ins
    if a.name not in reg.tools:  # the name typed after `tool` matches no built-in or plug-in tool
        raise ProjectError(f"no tool {a.name!r}; tools: {', '.join(reg.tools)}")  # list the valid names
    t = reg.tools[a.name]  # the Tool record: its run function, parameter schema, required names and supported platforms
    props = t.params  # its parameter schema properties
    if a.params:  # all parameters as one JSON object (how PowerShell and the MCP server call it)
        # --params JSON is taken as the whole parameter set, with no per-flag parsing or type conversion
        params = json.loads(a.params)
    else:  # otherwise parse --name value options with a parser built from the schema
        # The `tool` sub-command was created with add_help=False, so `speechkit-voice-tts-offline-pipeline tool impose --help` reaches this
        # parser and shows the tool's own options.
        tp = Parser(prog=f"speechkit-voice-tts-offline-pipeline tool {a.name}", description=t.description)
        add_schema_options(tp, props)  # one option per parameter
        params = collect(tp.parse_args(extra), props)  # parse the leftover arguments and keep only those given
    miss = [k for k in t.required if k not in params]  # required parameters that were not supplied
    if miss:  # at least one required parameter is absent, so stop before running the tool
        # named as the flags the user would type
        raise ProjectError(f"tool {a.name}: missing {', '.join('--' + m.replace('_', '-') for m in miss)}")
    if not R.platform_ok(t.platforms):  # this platform is not listed for the tool (speak on Windows)
        # no delegate command is set for passing the tool to WSL or another system, so explain how to set one
        if not os.environ.get("SPEECHKIT_DELEGATE"):
            # explain the problem and the fix
            raise ProjectError(f"tool {a.name} needs {'/'.join(t.platforms)}; this is {R.current_platform()}. "
                               # the --exec form keeps WSL's shell from re-parsing arguments
                               "Set SPEECHKIT_DELEGATE (e.g. \"wsl -d Ubuntu-24.04 --exec\") to run it there")
        res = R.delegate_tool(t, params, log)  # run it through the delegate, path parameters translated both ways
    else:  # the tool supports this platform, so it runs inside this process
        res = t.run(**params, log=log)  # keyword arguments plus the logger
    emit({"tool": a.name, **res})  # the result, labelled with the tool name


# The planner arguments shared by `plan` and `run`, taken from the parsed command line. a: parsed arguments.
# Returns a dict of keyword arguments for R.plan / R.run. --options is JSON and may be absent.
def _run_args(a):
    # {"narrate": {"workers": 4}} style JSON, or empty
    opts = json.loads(a.options) if getattr(a, "options", None) else {}
    # empty lists become None or () so the planner applies its defaults
    return dict(targets=a.targets or None, force=tuple(a.force or ()), only=a.only or None, skip=tuple(a.skip or ()),
                from_stage=a.from_stage, options=opts)  # --from and the options dict


# `speechkit-voice-tts-offline-pipeline plan DIR [STAGE ...]`: show what `run` would do and why, without running anything. a: parsed
# arguments. Prints one line per step, or emits {"project", "steps"} under --json.
def cmd_plan(a):
    p = Project(a.dir)  # open the project (raises ProjectError if project.json is missing)
    reg = R.load(p, a.plugin or ())  # registry with the project's plug-ins
    kw = _run_args(a)  # targets, force, only, skip, from
    # Output-affecting options are part of the plan fingerprint.  # plan() takes no stage options
    steps = R.plan(p, reg, adopt=not a.no_adopt, **kw)  # --no-adopt shows the plan as a delegated run would see it
    if JSON:  # --json mode: the calling program gets the plan as data instead of a printed table
        emit({"project": str(p.root), "steps": steps})  # the steps as JSON
        return  # the human table is not needed
    for s in steps:  # interactive mode: print one line per planned step
        where = "" if s["where"] == "here" else " (delegated)"  # mark stages that would run through the delegate
        # action, stage and the reasons in columns
        print(f"{s['action']:<8} {s['stage']:<10}{where} {'; '.join(s['reasons'])}")


# `speechkit-voice-tts-offline-pipeline run DIR [STAGE ...]`: bring stages up to date (only what is missing or changed runs). a: parsed
# arguments. Interactive mode logs what ran; --json emits the full report from R.run.
def cmd_run(a):
    p = Project(a.dir)  # open the project named on the command line
    # built-in stages plus plug-ins from --plugin, SPEECHKIT_PLUGINS and the project's settings and folder
    reg = R.load(p, a.plugin or ())
    # plan and run; the stages log through log()
    r = R.run(p, reg, accept_draft=a.accept_draft, log=log, adopt=not a.no_adopt, dry_run=a.dry_run, **_run_args(a))
    if not JSON:  # interactive mode: a short summary
        for s in r["steps"]:  # walk the run report to summarise what actually happened
            if s["action"] in ("ran", "adopt", "failed"):  # only steps that did something (or failed)
                # with the duration when known
                log(f"{s['action']}: {s['stage']}" + (f" in {s.get('seconds')} s" if s.get("seconds") is not None else ""))
        if r.get("stopped"):  # a stage asked to stop the pipeline
            log(f"stopped after {r['stopped']['stage']}: {r['stopped']['why']}")  # say which and why
        elif r.get("dry_run"):  # a dry run: show the plan instead
            for s in r["steps"]:  # a dry run executes nothing, so list each step the planner would have taken
                # padded columns: action (run, skip, adopt or blocked), stage name, then its reasons joined by
                # semicolons
                print(f"{s['action']:<8} {s['stage']:<10} {'; '.join(s['reasons'])}")
        return  # interactive mode ends here
    emit({"project": str(p.root), **r})  # machine mode: project folder plus the run report


# `speechkit-voice-tts-offline-pipeline all DIR`: the whole default pipeline, the same as `run` without targets. a: parsed arguments
# (dir, from_stage, skip_audit, accept_draft, plugin). Fills in the fields `run` expects, then calls cmd_run.
def cmd_all(a):
    a.targets, a.only, a.force = None, None, []  # no targets, no --only, nothing forced
    a.skip = ["autofix"] if a.skip_audit else []  # --skip-audit leaves out autofix, the long pronunciation loop
    a.options, a.no_adopt, a.dry_run = None, False, False  # no stage options, adoption on, really run
    cmd_run(a)  # the shared run path


# `speechkit-voice-tts-offline-pipeline <stage> DIR [options]`: run one stage now, like make: stale prerequisites first, then the stage
# itself (forced). a: parsed arguments including the stage's own options; name: the stage. Emits its result.
def cmd_stage(a, name):
    p = Project(a.dir)  # load the project folder
    reg = R.load(p, a.plugin or ())  # registry with plug-in stages (they get sub-commands too)
    # the Stage record behind this sub-command; its options schema tells collect() which values to read
    st = reg.stages[name]
    opts = collect(a, st.options)  # its options as given on the command line
    # like make: whatever this stage needs that is stale runs first, then the stage itself (forced)
    # target the stage, force it, pass its options under its name
    r = R.run(p, reg, targets=[name], force=[name], options={name: opts},
              # only stage sub-commands define --accept-draft, hence getattr
              accept_draft=getattr(a, "accept_draft", False), log=log)
    # this stage's step in the report (always present: it is the target)
    step = next(s for s in r["steps"] if s["stage"] == name)
    # project, stage, its result and duration
    emit({"project": str(p.root), "stage": name, "result": step.get("result"), "seconds": step.get("seconds"),
          # human="" prints just an empty line in interactive mode (the stage already logged)
          "stopped": r.get("stopped")}, human="")


# `speechkit-voice-tts-offline-pipeline status DIR`: what is done per stage, the narration progress and the exported files. a: parsed
# arguments. Prints a table and the extras, or emits everything as JSON.
def cmd_status(a):
    p = Project(a.dir)  # open it (fails clearly without project.json)
    reg = R.load(p, a.plugin or ())  # include plug-in stages so their progress is listed beside the built-in ones
    # per stage from the saved run state: done, partial, finish time, adopted, missing outputs, last error
    st = R.status(p, reg)
    extra = {}  # narration and export details
    if p.p("request").exists():  # a request.json means the script stage ran, so narration can be counted
        try:  # counting is cheap: it only reads request.json and the per-segment .wav.json files
            from .voice import narrate  # imported only here, so status works without loading narration code otherwise
            extra["narration"] = narrate.status(p)  # segments in total, segments voiced, and voiced minutes
        # For example a corrupt request.json or a half-written timing file: reported as text instead.
        except Exception as e:  # noqa: BLE001
            extra["narration"] = str(e)  # report the problem instead of failing the whole status
    # names of the exported files, if any
    extra["exported"] = [x.name for x in sorted(p.p("out_narration").glob("*")) if x.is_file()] \
        if p.p("out_narration").exists() else []
    if JSON:  # machine mode: stage status and the extras go out together as one JSON object
        emit({**st, **extra})  # merged into one object
        return  # no table in this mode
    for s in st["stages"]:  # interactive mode: one row per stage, in pipeline order
        flag = "done" if s["done"] else ("ERROR" if s["error"] else "-")  # done, ERROR, or "-" for not yet
        # status, stage name, finish time, adopted mark
        print(f"{flag:<6} {s['stage']:<10} {s['finished'] or ''}{' (adopted)' if s['adopted'] else ''}"
              # outputs missing although the stage is recorded as done
              f"{'  missing: ' + ', '.join(s['missing']) if s['missing'] and s['done'] else ''}"
              f"{'  ' + s['error'] if s['error'] else ''}")  # the last error message
    print(json.dumps(extra, indent=1, ensure_ascii=False))  # extras as indented JSON


# `speechkit-voice-tts-offline-pipeline settings DIR [--set JSON] [--effective]`: show project.json or deep-merge changes into it.
# a: parsed arguments. Writes project.json when --set is given. Emits the settings.
def cmd_settings(a):
    p = Project(a.dir)  # current settings are read from project.json
    if a.set:  # --set was given: merge that JSON into project.json before showing anything
        p.save_settings(json.loads(a.set))  # deep-merge them into project.json (the file is rewritten and reloaded)
    if a.effective:  # show every setting including defaults
        emit({"project": str(p.root), "settings": p.cfg})  # the merged, effective configuration
    else:  # default view: only the values written in project.json, without the built-in defaults
        emit({"project": str(p.root), "settings": p.raw})  # only what the project itself sets


# `speechkit-voice-tts-offline-pipeline pron DIR <cmd> ...`: pass a command to the pronunciation audit loop (emblem_rsi). a: parsed
# arguments (dir, pron_cmd, rest). Exits the process with that tool's exit code.
def cmd_pron(a):
    from . import audit  # imported only for this command
    audit.configure(Project(a.dir))  # set the EMBLEM_* variables for this project and import the audit modules
    d = audit.run_dir(Project(a.dir))  # the audit run folder, sealing the rubric on first use
    from emblem_rsi import cli as ecli  # the audit loop's own command line
    # sys.exit bypasses main()'s handlers, so --json gets no result object from this command
    sys.exit(ecli.main(["pron", a.pron_cmd, "--dir", str(d), *a.rest]))


# --------------------------------------------------------------------------------------- parser
# Raised instead of argparse's usual exit when a usage mistake happens under --json, so main() can report it
# as {"ok": false, "type": "UsageError"} on stdout.
class UsageError(Exception):
    pass  # a plain marker exception; its class name alone tells main() the problem was a usage mistake


# An ArgumentParser whose usage errors become exceptions under --json. Other programs then always receive a JSON
# answer instead of a usage message on stderr and exit code 2 from argparse.
class Parser(argparse.ArgumentParser):
    """Under --json a usage mistake becomes {"ok": false, "type": "UsageError"} instead of text on stderr."""

    # argparse calls error() for a bad or missing argument. message: argparse's explanation. Raises UsageError
    # under --json; otherwise prints usage and exits with code 2 as argparse normally does.
    def error(self, message):
        if JSON:  # only when --json is active
            raise UsageError(f"{self.prog}: {message}")  # prefixed with the program name, e.g. "speechkit-voice-tts-offline-pipeline run: ..."
        # interactive mode: argparse prints the usage line and message to stderr, then exits with code 2
        super().error(message)


# Build the full command-line parser. reg: a loaded Registry; every stage that is not already a fixed command
# gets its own sub-command with its declared options, so plug-in stages become commands too. Returns the parser.
def build_parser(reg) -> argparse.ArgumentParser:
    # the module docstring is the help text; RawDescription keeps its column layout
    ap = Parser(prog="speechkit-voice-tts-offline-pipeline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # also removed from argv by main() before parsing; declared here for --help
    ap.add_argument("--json", action="store_true", help="one JSON object on stdout, logs on stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)  # one sub-parser per command; a command is mandatory

    # Add --plugin (repeatable) to sub-parser x and return x, so calls can be chained.
    def with_plugin(x):
        # each --plugin adds one module or .py file
        x.add_argument("--plugin", action="append", help="load a plug-in (module or .py file)")
        return x  # hand the sub-parser back so the caller can keep adding its own arguments
    n = sub.add_parser("new", help="make a project folder")  # `new DIR --input FILE`
    # the project folder to create for the new book; create() refuses one that already has a project.json
    n.add_argument("dir")
    n.add_argument("--input", required=True)  # the book file is mandatory
    for k in ("title", "subtitle", "author"):  # optional metadata
        n.add_argument(f"--{k}", default="")  # each defaults to empty
    n.add_argument("--set", help="JSON settings for project.json, e.g. '{\"spoken\": {\"code_names\": true}}'")
    sub.add_parser("doctor")  # `doctor`: report which packages, runtime and models this machine has or lacks
    sub.add_parser("setup")  # installs what is missing
    sub.add_parser("mcp", help="MCP server on stdio")  # handled early in main(); declared here so it appears in help
    # `stages [DIR]`: list the pipeline stages; --plugin adds plug-in stages to the listing
    x = with_plugin(sub.add_parser("stages"))
    # a project folder is optional; when given, that project's own plug-in stages are listed too
    x.add_argument("dir", nargs="?")
    # `tools`: list the standalone tools and their parameters, including ones added by --plugin
    with_plugin(sub.add_parser("tools"))
    # add_help=False: --help passes through to the tool's own parser
    x = with_plugin(sub.add_parser("tool", add_help=False))
    # which standalone tool to run; its own options follow and are parsed later from the tool's schema
    x.add_argument("name")
    x.add_argument("--params", help="all parameters as one JSON object")  # alternative to --name value options
    for name in ("plan", "run"):  # the two planner commands share their options
        x = with_plugin(sub.add_parser(name))  # build the `plan` or the `run` sub-parser, both accepting --plugin
        x.add_argument("dir")  # DIR: the book's project folder
        # zero or more stage names
        x.add_argument("targets", nargs="*", help="stages to bring up to date (default: the whole pipeline)")
        # may be given several times
        x.add_argument("--force", action="append", help="run this stage even if it is up to date ('all' for every one)")
        # one --only per stage
        x.add_argument("--only", action="append", help="run just this stage, without what it needs")
        x.add_argument("--skip", action="append", help="leave this stage out")  # one --skip per stage to leave out
        # stored as from_stage because `from` is a Python keyword
        x.add_argument("--from", dest="from_stage", help="force this stage and everything after it")
        # stops the planner from recording outputs that predate tracking
        x.add_argument("--no-adopt", action="store_true", help="do not adopt outputs made before tracking began")
        x.add_argument("--options", help='stage options as JSON, e.g. {"narrate": {"workers": 4}}')
        if name == "run":  # options that only make sense when running
            # skip the review stop after the first check
            x.add_argument("--accept-draft", action="store_true", help="narrate the extracted draft without stopping")
            # per-stage options as JSON
            x.add_argument("--dry-run", action="store_true")  # plan and return without running
    x = with_plugin(sub.add_parser("all"))  # `all DIR`: the whole pipeline with fewer choices than `run` offers
    x.add_argument("dir")  # DIR of the book
    x.add_argument("--from", dest="from_stage")  # start from this stage (forced)
    # skip the autofix stage (the slow pronunciation audit) for a quicker full run
    x.add_argument("--skip-audit", action="store_true")
    x.add_argument("--accept-draft", action="store_true")  # skip the review stop
    # `status DIR`: show which stages are done; --plugin adds plug-in stages to the table
    x = with_plugin(sub.add_parser("status"))
    x.add_argument("dir")  # which project to report on
    x = sub.add_parser("settings")  # `settings DIR`; plug-ins do not matter here
    x.add_argument("dir")  # whose project.json to show or change
    x.add_argument("--set", help="JSON merged into project.json")  # JSON to merge in
    # --effective prints the merged configuration with defaults instead of only project.json's own values
    x.add_argument("--effective", action="store_true", help="show every setting with its default")
    x = sub.add_parser("pron")  # `pron DIR <cmd> ...`
    x.add_argument("dir")  # the project whose audit run folder is used
    # the audit loop's sub-commands
    x.add_argument("pron_cmd", choices=["trial", "apply", "retake", "revert", "status", "measure", "decide"])
    x.add_argument("rest", nargs=argparse.REMAINDER)  # everything after the command is passed through untouched
    for st in reg.stages.values():  # one sub-command per registered stage
        if st.name in sub.choices:  # a stage named like a fixed command (run, status ...) would clash
            continue  # it is reachable through `run DIR --only NAME` instead
        x = with_plugin(sub.add_parser(st.name, help=st.description))  # the stage's description is its help line
        x.add_argument("dir")  # every stage command takes the project folder first
        x.add_argument("--accept-draft", action="store_true")  # lets `check` continue past the review stop
        add_schema_options(x, st.options)  # the stage's own options, e.g. --workers for narrate
    return ap  # the finished parser: fixed commands plus one sub-command for each remaining stage


# Entry point of `python -m speechkit_narrate` and the `speechkit-voice-tts-offline-pipeline` script. argv: the arguments (default sys.argv[1:]).
# Returns the exit code: 0 success, 1 failure (doctor misses or an unexpected error under --json), 2 usage or
# project error. Under --json exactly one JSON object is written to stdout whatever happens.
def main(argv=None) -> int:
    global JSON  # main() switches the module into JSON mode
    argv = list(sys.argv[1:] if argv is None else argv)  # a list we may modify
    if "--json" in argv:  # --json may appear anywhere on the command line
        argv = [x for x in argv if x != "--json"]  # remove it, so sub-parsers never see it
        JSON = True  # machine mode from here on
    # The MCP server must start before the registry loads: a plug-in that prints would corrupt the protocol stream.
    if argv[:1] == ["mcp"]:                    # before any plug-in loads: nothing may print on the protocol channel
        from . import mcp_server  # imported only when needed (it needs the MCP SDK)
        return mcp_server.main()  # serves until the client disconnects
    if JSON:  # output reserved for one JSON object
        protect_stdout()  # from now on only the result goes to the real stdout
    try:  # registry loading can fail (broken plug-in, bad project.json)
        # The first two words after the command are tried as a project folder (e.g. `run DIR` or `narrate DIR`).
        # a project named on the command line contributes its own plug-in stages as commands
        # the first one that holds a project.json, or None
        proj = next((Path(x) for x in argv[1:3] if not x.startswith("-") and (Path(x) / "project.json").exists()), None)
        # registry with that project's plug-ins, so its stages get sub-commands
        reg0 = R.load(Project(proj) if proj else None)
    # a broken plug-in or an unreadable project.json stops the command before any parsing
    except (RegistryError, ProjectError) as e:
        if JSON:  # report the failure as JSON
            result({"ok": False, "error": str(e), "type": type(e).__name__})  # one JSON error object
        else:  # no --json, so the error is meant for the person at the terminal
            print(f"speechkit-voice-tts-offline-pipeline: {e}", file=sys.stderr)  # a short message on stderr
        return 2  # exit code 2, the same code argparse uses for a usage mistake
    ap = build_parser(reg0)  # the parser now knows every stage
    extra = []  # unrecognised arguments for `tool`
    try:  # every error below is reported the same way
        if argv and argv[0] == "tool":  # the tool command passes unknown options to the tool's own parser
            # unknown options come back in extra instead of failing; cmd_tool parses them with the tool's schema
            a, extra = ap.parse_known_args(argv)
        else:  # all other commands: their options are fully known to this parser
            a = ap.parse_args(argv)  # strict parsing: unknown options are errors
        if a.cmd == "new":  # `speechkit-voice-tts-offline-pipeline new DIR --input FILE`: create a project folder from a book file
            # make the folder and copy the input in
            p = create(a.dir, a.input, a.title, a.subtitle, a.author, settings=json.loads(a.set) if a.set else None)
            # suggest the next command
            log(f"new project {p.root} ({p.input_kind()} input). Next: python -m speechkit_narrate run \"{p.root}\"")
            emit({"project": str(p.root), "settings": p.raw}, human="")  # the new project's settings
        # `speechkit-voice-tts-offline-pipeline doctor`: check the machine for the packages, runtime and models the narration needs
        elif a.cmd == "doctor":
            rows = env.checks()  # (name, ok, detail) for every dependency
            bad = sum(1 for _, ok, _ in rows if not ok)  # how many are missing
            if JSON:  # a JSON report of every check
                # ok only when nothing is missing
                result({"ok": bad == 0, "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in rows]})
            else:  # without --json the checks are printed as a table for a person to read
                for name, ok, detail in rows:  # one row per check
                    print(f"{'ok  ' if ok else 'MISS'} {name:<45} {detail}")  # ok or MISS, name, detail in columns
            return 1 if bad else 0  # exit code 1 when anything is missing
        elif a.cmd == "setup":  # `speechkit-voice-tts-offline-pipeline setup`: install whatever the doctor checks would report missing
            env.setup(log)  # install what is missing, logging each step
            # under --json this sends {"ok": true, "setup": "done"}; interactively it prints just an empty line
            emit({"setup": "done"}, human="")
        elif a.cmd == "mcp":  # a fallback: main() normally starts the server earlier, when `mcp` is the first word
            from . import mcp_server  # imported late because it requires the MCP SDK, which no other command needs
            # answer MCP requests over stdin and stdout until the client hangs up; its exit code becomes ours
            return mcp_server.main()
        elif a.cmd == "stages":  # `speechkit-voice-tts-offline-pipeline stages [DIR]`: list the pipeline stages
            cmd_stages(a)  # prints the stage table, or emits the registry description as JSON
        elif a.cmd == "tools":  # `speechkit-voice-tts-offline-pipeline tools`: list every standalone tool with its parameters
            cmd_tools(a)  # prints each tool with its options, or the whole list as JSON
        elif a.cmd == "tool":  # `speechkit-voice-tts-offline-pipeline tool NAME ...`: run one standalone tool on plain files
            cmd_tool(a, extra)  # run a standalone tool
        elif a.cmd == "plan":  # `speechkit-voice-tts-offline-pipeline plan DIR`: preview what `run` would do
            cmd_plan(a)  # lists each step and its reasons without running any stage
        elif a.cmd == "run":  # `speechkit-voice-tts-offline-pipeline run DIR [STAGE ...]`: bring the chosen stages up to date
            cmd_run(a)  # runs only what is missing or changed, then logs or emits the report
        elif a.cmd == "all":  # `speechkit-voice-tts-offline-pipeline all DIR`: run the full default pipeline
            cmd_all(a)  # fills in the defaults that `run` expects and goes through cmd_run
        elif a.cmd == "status":  # `speechkit-voice-tts-offline-pipeline status DIR`: report the project's progress
            cmd_status(a)  # prints stage status, narration counts and exported files
        elif a.cmd == "settings":  # `speechkit-voice-tts-offline-pipeline settings DIR`: view or change project.json
            cmd_settings(a)  # show or change settings
        elif a.cmd == "pron":  # `speechkit-voice-tts-offline-pipeline pron DIR CMD`: hand a command to the pronunciation audit tool
            cmd_pron(a)  # never returns here: it ends the process with sys.exit and the audit tool's exit code
        else:  # any other command is a stage name
            cmd_stage(a, a.cmd)  # run that stage now
        return 0  # the command finished without an exception, so the process exits with code 0
    # Expected failures: a bad project, registry or argument (json.JSONDecodeError is a ValueError) or a missing file.
    except (ProjectError, RegistryError, ValueError, FileNotFoundError, UsageError) as e:
        if JSON:  # a calling program gets JSON
            # one JSON failure object with the message and the exception class name, e.g. ProjectError
            result({"ok": False, "error": str(e), "type": type(e).__name__})
        else:  # no --json: a short note for the person at the terminal
            print(f"speechkit-voice-tts-offline-pipeline: {e}", file=sys.stderr)  # short message without a traceback
        return 2  # exit code 2 marks an expected error the user can fix, unlike 1 for a crash
    # Anything else is a bug or an environment problem.
    except Exception as e:  # noqa: BLE001
        if JSON:  # programs still get one JSON object
            # error, type and the last 3000 characters of the traceback
            result({"ok": False, "error": f"{type(e).__name__}: {e}", "type": type(e).__name__,
                    "trace": traceback.format_exc()[-3000:]})  # so the calling program can show where it failed
            return 1  # exit code 1 for an unexpected failure, after the JSON report has been written
        raise  # interactive mode: let Python print the full traceback
