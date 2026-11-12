# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""speechkit-narrate MCP server:  python -m speechkit_narrate mcp   (stdio)

Requires the MCP Python SDK 2.x, mcp >= 2.3.0: the low-level `mcp.server.lowlevel.Server` that takes its
`on_list_tools` / `on_call_tool` handlers in the constructor, and `mcp.server.stdio.stdio_server`. It speaks
the 2025-11-25 initialize handshake and the 2026-07-28 per-request protocol (the SDK picks per connection).

Tools, regenerated from the stage registry at every tools/list (so plug-in edits and SPEECHKIT_PLUGINS
changes show up without restarting the server):
    narrate_describe        stages, tools, plug-ins, platform, delegate (optionally with a project's plug-ins)
    project_new              make a project folder from a .txt / .md / SpeechKit request .json
    project_status           what is done
    project_settings         show project.json, or deep-merge a `set` object into it
    project_plan             what project_run would do, and why
    project_run              bring stages up to date (job)
    stage_<name>             run one stage now, with that stage's own options (job)
    tool_<name>              a standalone tool on plain files (job)
    job_status / job_list / job_cancel

Everything runs as `python -m speechkit_narrate --json ...` in a subprocess (PYTHONPATH = the toolkit root,
PYTHONUTF8=1, the server's environment and working directory inherited, so SPEECHKIT_* and relative
SPEECHKIT_PLUGINS paths mean the same as for the server). The server's stdout carries only the protocol.

Jobs: run / stage / tool calls start a subprocess and wait up to `wait_seconds` for it. If it finishes in
time the call returns its result; otherwise it returns the job id and the client polls job_status. A job's
stderr (the toolkit log) goes to an in-memory ring buffer and to <SPEECHKIT_CACHE>/mcp-jobs/<time>_<id>.log;
its stdout is the command's one JSON object (the job's `result`). Two jobs never run on the same project at
once. Jobs live as long as the server: when it stops, running jobs are stopped too (stages resume where
they can, narration keeps the segments it finished).
"""
# Postpone evaluation of annotations. Besides saving import time this matters here: hints such as `types.Tool`
# name the MCP SDK, which may be missing, and as strings they are never evaluated.
from __future__ import annotations

import atexit  # stop running jobs when the interpreter exits normally
import collections  # deque: a fixed-size ring buffer of log lines per job
import inspect  # inspect.signature() checks that the installed SDK has the 2.x constructor
import json  # every tool result is JSON; arguments like `set` and `options` are passed on as JSON
import os  # environment for child processes, paths, killing process groups
import re  # NAME_RE: characters allowed in tool names and job ids
import secrets  # random hex suffix that makes job ids unique
import signal  # SIGTERM / SIGKILL for stopping jobs on Linux and macOS
import subprocess  # every toolkit call runs `python -m speechkit_narrate --json ...` as a child process
import sys  # stderr for the server's own log, sys.executable for children
import threading  # a supervisor thread per job plus reader threads for its pipes
import time  # wall-clock and monotonic times for jobs, timeouts and the catalog cache
from collections.abc import Callable  # type of callbacks such as a job's result reshaper
# dataclass builds Route and Catalog; field() gives each Route its own empty dicts
from dataclasses import dataclass, field
from pathlib import Path  # log files under the cache folder, project folders
from typing import Any  # loose typing for values that arrive from the client as JSON

# installed(): tells a pip-installed copy from a toolkit folder (named _env because child_env has a local `env`)
from . import env as _env
from . import winpaths  # Windows paths in and out when the server runs inside WSL (a no-op elsewhere)

# The SDK is optional for the rest of the toolkit: if it is missing this module still imports, and main() prints
# an install hint instead of a traceback.
try:
    import anyio  # the async library the MCP SDK is built on (event loop, threads, sleep)
    from mcp import types  # the protocol's data types: Tool, CallToolResult, TextContent, error codes ...
    from mcp.server.lowlevel import Server  # the low-level server: handlers are passed to its constructor
    from mcp.server.stdio import stdio_server  # async context manager that serves the protocol over stdin/stdout
    # raising it from a handler sends a JSON-RPC error instead of a tool result
    from mcp.shared.exceptions import MCPError
    _MCP_ERROR: Exception | None = None  # None means the SDK imported fine
# Keep the ImportError so the message can say exactly what failed to import.
except ImportError as _e:  # the toolkit works without the SDK; only this command needs it
    _MCP_ERROR = _e  # reported later by _sdk_problem()

# The oldest MCP Python SDK release with the constructor-based low-level Server this module is written for.
MIN_SDK = "2.3.0"

# ROOT goes first on the children's PYTHONPATH, so they import this same copy of the toolkit.
ROOT = Path(__file__).resolve().parent.parent      # the folder that holds the speechkit-narrate package
# 2000 lines is enough for a job_status tail while keeping memory per job small; the full log is in a file.
LOG_RING = 2000                                     # log lines kept in memory per job
KEEP_FINISHED = 200                                 # finished jobs remembered
LOG_DAYS = 14                                       # job logs older than this are removed at start
# 300 s = 5 minutes. Quick commands load the registry and read files; they should finish in seconds.
QUICK_TIMEOUT = 300                                 # describe / status / plan / settings / new
MAX_WAIT = 3600  # longest a single call may block, in seconds (one hour); larger wait_seconds values are capped
# A client usually calls tools/list and then tools/call; within 5 s the registry is not loaded twice.
CATALOG_TTL = 5.0                                   # tools/call reuses the last tools/list for this long
# anything outside letters, digits, "_" and "-" is replaced in tool names and job ids
NAME_RE = re.compile(r"[^A-Za-z0-9_-]")
WIN = os.name == "nt"  # True on Windows, where processes are stopped with taskkill instead of signals
# Windows flag that starts console programs without a visible window; 0 (no effect) elsewhere
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Text sent to the client in the initialize response, telling the model how to use these tools: describe first,
# projects as folders, every run as a background job polled with job_status. The pieces are joined by Python into
# one string at compile time.
INSTRUCTIONS = (
    "speechkit-narrate turns a text (.txt, .md or a SpeechKit request .json) into an offline neural narration: one "
    "audio file per chapter with per-word timings, after a pronunciation audit that hears every word back and fixes "
    "what was misheard. Start with narrate_describe: it lists the stages (what each needs and makes), "
    "the standalone tools and the plug-ins. A project is a folder: make one with project_new, look at it with "
    "project_status and project_plan, and bring it up to date with project_run (only what is missing or "
    "changed runs; the first full run stops after `check` so work/book.md can be reviewed, unless "
    "accept_draft=true). stage_<name> runs one stage now with its own options; tool_<name> runs a standalone "
    "tool on plain files. Everything that runs is a background job: if it does not finish within wait_seconds "
    "the call returns a job_id - poll job_status (state running|done|failed|cancelled, result, log tail) until "
    "it is no longer running, or stop it with job_cancel. Narration and the pronunciation loop take minutes to hours: "
    "pass wait_seconds=0 and poll every 30-60 s. Paths are on the server's machine."
)


# The exception tools raise for an expected failure (bad path, busy project, failed command). on_call_tool()
# catches it and returns a normal tool result with isError=true, so the model can read the message and react,
# instead of a protocol error.
class ToolError(Exception):
    """A tool-level failure: reported to the client as a result with isError=true."""

    # message: what went wrong, for the model to read; data: optional structured details (for example the failed
    # command's JSON result or the busy job's view), shown as structured content.
    def __init__(self, message: str, data: dict | None = None):
        super().__init__(message)  # the message becomes str(exception)
        self.data = data  # optional details that error_result() adds as structured content, e.g. a busy job's view


# Write one line to the server's log on stderr with a timestamp and an "mcp:" prefix. stdout belongs to the
# protocol, so the server never prints there. msg: the text.
def _log(msg: str) -> None:
    # flush at once so a client showing server logs sees it immediately
    print(f"[{time.strftime('%H:%M:%S')}] mcp: {msg}", file=sys.stderr, flush=True)


# A Unix timestamp as local "YYYY-MM-DDTHH:MM:SS" text, or None when t is None or 0. Used for job start times.
def _iso(t: float | None) -> str | None:
    # localtime because the user reads these next to their own clock
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) if t else None


# JSON without spaces, for tool result text and log headers. d: any value; Paths and other objects become text.
# Returns the string.
def _compact(d: Any) -> str:
    # separators drop the spaces after "," and ":"; ensure_ascii=False keeps accents readable
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"), default=str)


# The toolkit version reported to the client as the server version: the checkout's pyproject.toml when running
# from source, else the installed package's metadata, else "dev". Reads one file; never raises.
def toolkit_version() -> str:
    """The checkout's pyproject.toml version, else the installed distribution's, else "dev"."""
    try:  # first try the source checkout
        import tomllib  # standard-library TOML reader (Python 3.11+)
        # [project] version from pyproject.toml next to the package
        return str(tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])
    # An installed copy has no pyproject.toml beside it; fall through to the package metadata.
    except Exception:  # noqa: BLE001, S110 - an installed package has no pyproject.toml
        pass  # nothing to do here
    try:  # second choice: the installed distribution
        from importlib.metadata import version  # reads the version stored in the installed speechkit-narrate package's metadata
        return version("speechkit-narrate")  # version of the installed "speechkit-narrate" distribution
    # Neither source is available, e.g. running from a copied folder without metadata.
    except Exception:  # noqa: BLE001
        return "dev"  # a neutral version string


# ------------------------------------------------------------------------------------------- subprocesses
# The environment for every child `python -m speechkit_narrate` process: the server's own environment (SPEECHKIT_* settings
# included) with ROOT first on PYTHONPATH and Python forced to UTF-8. Returns a new dict; os.environ is untouched.
# A pip-installed copy gets no PYTHONPATH change: its ROOT is site-packages, which the same interpreter already
# searches, and which would shadow the packages of any other Python that a stage starts (the speechkit runtime's).
def child_env() -> dict:
    env = dict(os.environ)  # a copy of the server's environment
    if not _env.installed():  # a toolkit folder or checkout needs ROOT on the path; an installed copy does not
        # normcase lower-cases and uses backslashes on Windows, so path comparison ignores case there
        root = os.path.normcase(str(ROOT))
        # existing PYTHONPATH entries except ROOT itself and empty ones
        rest = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and os.path.normcase(p) != root]
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT), *rest])  # ROOT first, then the rest in their original order
    env["PYTHONUTF8"] = "1"  # UTF-8 mode: files and pipes default to UTF-8 even on a Windows code page
    # stdin/stdout/stderr encoded as UTF-8 too, so logs with accented words survive the pipes
    env["PYTHONIOENCODING"] = "utf-8"
    return env  # handed to Popen as the child's complete environment


# The full command line for one toolkit call. args: the words after `speechkit-narrate --json`. Uses the interpreter that
# runs this server, so the children get the same Python and installed packages. Returns the list.
def command(args: list[str]) -> list[str]:
    return [sys.executable, "-m", "speechkit_narrate", "--json", *args]  # --json: one result object on stdout, the log on stderr


# Start one toolkit child process with pipes for stdout and stderr. args: the words after `speechkit-narrate --json`.
# Returns the Popen object; raises OSError when the interpreter cannot be started.
def _popen(args: list[str]) -> subprocess.Popen:
    # stdin is the null device: the child must never read the client's protocol messages. Both outputs are captured.
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "env": child_env()}
    if WIN:  # Windows: no new process group is made, only the console window is suppressed
        # CREATE_NO_WINDOW: no console window flashes up for each job when the client is a desktop app.
        kw["creationflags"] = NO_WINDOW                 # no console window per job when the client is a GUI app
    else:  # Linux and macOS: give the child its own session so cancelling can signal the whole group
        # start_new_session makes the child the leader of a new process group, so one signal reaches everything it
        # starts.
        kw["start_new_session"] = True                  # own process group: cancel reaches ffmpeg, Chromium ...
    return subprocess.Popen(command(args), **kw)  # start it with the toolkit command line and the options above


# Stop a job's process and everything it started. proc: the Popen; hard: True for an immediate kill (SIGKILL),
# False for a polite request first (SIGTERM). On Windows there is no polite form: taskkill /T /F always forces.
# Never raises; a process that is already gone is fine.
def _kill_tree(proc: subprocess.Popen, hard: bool) -> None:
    """Stop a job's process and everything it started."""
    if WIN:  # Windows has no process-group signals, so taskkill ends the whole process tree instead
        if proc.poll() is None:  # poll() returns None while the child has not exited, so there is something to kill
            try:  # taskkill may be missing or fail
                # /T includes child processes (ffmpeg, Chromium, WSL), /F forces; no window, no output, at most 20 s
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], check=False, stdin=subprocess.DEVNULL,
                               # every standard stream discarded so nothing reaches the protocol
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                               creationflags=NO_WINDOW)  # CREATE_NO_WINDOW again for taskkill itself
            except (OSError, subprocess.SubprocessError):  # taskkill missing or timed out
                pass  # fall through to a direct kill
            try:  # make sure at least the main process ends
                proc.kill()  # TerminateProcess on the child itself
            except OSError:  # the process ended between the check and the kill, or access was denied
                pass  # nothing left to stop
        return  # the Windows branch is finished; the POSIX signalling below is skipped
    try:  # Linux and macOS: signal the child's whole process group
        # the child's pid is also its process group id (start_new_session), so the whole group gets the signal
        os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):  # the group is already gone, or belongs to someone else
        pass  # nothing left to signal, or not allowed to: treat the job as stopped
    except OSError:  # any other failure to signal the group
        try:  # try the child alone
            proc.kill()  # SIGKILL to the main process
        except OSError:  # kill() failed as well; the child has most likely exited already
            pass  # the process is already gone


# Parse one output line as a JSON object, or return None. line: text from the child's stdout. Lines that do not
# start with "{" and end with "}" are rejected before parsing, so ordinary log text is cheap to skip.
def _json_line(line: str) -> dict | None:
    s = line.strip()  # ignore surrounding whitespace and the newline
    # cheap test first: a JSON object line must start with { and end with }
    if not (s.startswith("{") and s.endswith("}")):
        return None  # ordinary log text, not a result object
    try:  # the line looks like an object; decode it, catching malformed JSON
        v = json.loads(s)  # the actual JSON text
    except ValueError:  # json.JSONDecodeError is a ValueError
        return None  # broken JSON is treated as ordinary text
    return v if isinstance(v, dict) else None  # only objects count as results


# The error message for a failed toolkit call. res: the parsed result object (may be None); stderr_tail: log
# lines; code: the exit code. Prefers the toolkit's own error text, else the exit code plus the last three
# non-empty log lines. Returns a string.
def _failure(res: dict | None, stderr_tail: list[str], code: int | None) -> str:
    if res and res.get("error"):  # the toolkit explained the failure
        return str(res["error"])  # the toolkit's own message is the most precise explanation available
    tail = [ln for ln in stderr_tail if ln.strip()][-3:]  # last three lines that are not blank
    msg = f"speechkit-narrate exited with code {code}"  # fallback message naming the child's exit code
    return msg + (": " + " | ".join(tail) if tail else "")  # append the lines separated by " | ", if any


# Run a short toolkit command and wait for it. args: the words after `speechkit-narrate --json`; timeout: seconds
# (QUICK_TIMEOUT by default). Returns the command's JSON result; raises ToolError on timeout or failure.
# Blocking, so async callers run it on a worker thread.
def run_quick(args: list[str], timeout: float = QUICK_TIMEOUT) -> dict:
    """A short command (describe, status, plan, settings, new): its JSON, or ToolError."""
    p = _popen(args)  # launch python -m speechkit_narrate --json with these words, both output pipes attached
    try:  # communicate() reads both pipes fully, avoiding a deadlock on a full pipe
        out, err = p.communicate(timeout=timeout)  # wait at most `timeout` seconds for it
    except subprocess.TimeoutExpired:  # the command ran past its time limit (300 s by default)
        _kill_tree(p, hard=True)  # kill the process group at once
        p.communicate()  # collect what is left so the process is reaped
        # `from None` hides the timeout traceback; the message is enough
        raise ToolError(f"speechkit-narrate {args[0]} took longer than {timeout:.0f} s") from None
    res = None  # the parsed result, if any
    # search from the end: the result is the last JSON line
    for line in reversed(out.decode("utf-8", "replace").splitlines()):
        res = _json_line(line)  # a dict when this line is a JSON object, otherwise None
        if res is not None:  # the last JSON object on stdout is the command's result
            break  # stop at the last one
    # success: exit code 0, a result, and the result does not say ok=false
    if p.returncode == 0 and res is not None and res.get("ok", True) is not False:
        return res  # the command's JSON object
    # otherwise a ToolError with the best message and the result as data
    raise ToolError(_failure(res, err.decode("utf-8", "replace").splitlines(), p.returncode), res)


# ------------------------------------------------------------------------------------------- jobs
# The folder for job logs, <SPEECHKIT_CACHE>/mcp-jobs, created if needed. Returns it, or None when it cannot
# be created (a read-only cache only costs the log files, the jobs still run).
def jobs_dir() -> Path | None:
    try:  # anything below may fail
        from . import env  # env.py knows the cache folder
        d = Path(env.CACHE) / "mcp-jobs"  # one sub-folder for this server's logs
        d.mkdir(parents=True, exist_ok=True)  # create the folder and its parents; an existing folder is fine
        return d  # the usable log folder that each Job writes into
    # For example a read-only home folder or a cache path on a missing drive.
    except Exception:  # noqa: BLE001 - a read-only cache only costs the log file
        return None  # callers handle None by keeping logs in memory only


# Delete job log files older than `days` (LOG_DAYS by default) from the jobs folder. Called once when the server
# starts, so logs do not pile up. Errors on single files are ignored.
def prune_logs(days: float = LOG_DAYS) -> None:
    d = jobs_dir()  # the job log folder, or None when the cache is not writable
    if d is None:  # no folder: nothing to prune
        return  # without a log folder there are no old files to delete
    old = time.time() - days * 86400  # cut-off time: 86400 seconds per day
    for f in d.glob("*"):  # every file in the folder (.log and .json)
        try:  # a file can vanish or be locked (Windows) while we look
            if f.is_file() and f.stat().st_mtime < old:  # a plain file last modified before the cut-off
                f.unlink()  # delete the expired .log or .json file
        except OSError:  # locked by another process, or already gone
            pass  # leave it for next time


# One background job: a `python -m speechkit_narrate --json ...` child process plus everything known about it (state,
# result, a ring buffer of its log, a log file). A supervisor thread waits for the process and fills in the
# outcome; tool handlers read it through view(). Created and started by Jobs.start().
class Job:
    """One `python -m speechkit_narrate --json ...` subprocess."""

    # kind: "run", "stage" or "tool"; name: the stage or tool name ("run" for project_run); args: the words after
    # `speechkit-narrate --json`; project: the project folder, or None for tools (used to keep one job per project);
    # post: optional function that reshapes the successful result. Picks the job id and log file name.
    def __init__(self, kind: str, name: str, args: list[str], project: str | None = None,
                 # post is called with the result dict and returns a new dict
                 post: Callable[[dict], dict] | None = None):
        # store the arguments for the views, the busy check and the result reshaping
        self.kind, self.name, self.args, self.project, self.post = kind, name, args, project, post
        # label: "run" or e.g. "stage-narrate", made safe for file names, at most 40 characters
        label = NAME_RE.sub("-", name if kind == "run" else f"{kind}-{name}")[:40]
        self.id = f"{label}-{secrets.token_hex(3)}"  # plus 6 random hex digits (3 bytes), e.g. "stage-narrate-1f2e3d"
        self.argv = command(args)  # the full command line, for reports and the log header
        self.state = "running"  # the state a client sees: running, then done, failed or cancelled
        self.exit_code: int | None = None  # the child's exit code once it ends
        self.result: dict | None = None  # the command's JSON result (reshaped by post when it succeeded)
        self.error: str | None = None  # failure message for failed and cancelled jobs
        self.started = time.time()  # wall-clock creation time in seconds, used for durations and the started field
        self.ended: float | None = None  # end time once finished
        # last LOG_RING log lines; older lines drop off the front
        self.lines: collections.deque = collections.deque(maxlen=LOG_RING)
        self.total_lines = 0  # how many log lines there were in total
        # set when someone asks to stop it, so the outcome reads "cancelled", not "failed"
        self.cancel_requested = False
        self.done = threading.Event()  # set when the job has finished and its outcome is recorded
        self.proc: subprocess.Popen | None = None  # the child process, once started
        self._json: dict | None = None  # the last JSON object the child printed on stdout
        self._lock = threading.Lock()  # guards lines, total_lines and the log file between threads
        self._fh = None  # open log file while the job runs
        d = jobs_dir()  # the jobs folder, or None
        # e.g. 20261028-142501_stage-narrate-1f2e3d.log, or None when there is no folder
        self.log_file = d / f"{time.strftime('%Y%m%d-%H%M%S')}_{self.id}.log" if d else None

    # ---- running
    # Open the log file, start the child process and its supervisor thread. Returns the job itself so callers can
    # chain. Raises ToolError when the process cannot be started. Side effects: creates a log file and a process.
    def start(self) -> Job:
        if self.log_file:  # a jobs folder exists
            try:  # the log file is optional
                # Opened here and closed in _close_log() when the job ends, hence no `with` block.
                self._fh = open(self.log_file, "a", encoding="utf-8")   # noqa: SIM115 - open while the job runs
                # header: job id, start time, the exact command
                self._fh.write(f"# job {self.id}\n# started {_iso(self.started)}\n# command {_compact(self.argv)}\n")
                self._fh.flush()  # write it out now
            except OSError:  # cannot open the file
                self._fh, self.log_file = None, None  # continue with the in-memory log only
        try:  # starting the child can fail, for example when the interpreter is missing
            self.proc = _popen(self.args)  # the child toolkit command
        except OSError as e:  # e.g. the interpreter path is invalid
            self._close_log()  # do not leak the file handle
            raise ToolError(f"cannot start {self.argv[0]}: {e}") from e  # report it to the client as a tool error
        # the supervisor thread; daemon=True so it never keeps the server alive on exit
        threading.Thread(target=self._supervise, name=f"job-{self.id}", daemon=True).start()
        return self  # returns the job so a caller can write Job(...).start() in one expression

    # Append one line to the job's log (memory ring and log file), under the lock. line: text without the newline.
    def _add(self, line: str) -> None:
        with self._lock:  # readers and the supervisor add lines from different threads
            self.lines.append(line)  # newest last; the deque drops the oldest when full
            self.total_lines += 1  # count every line ever added
            if self._fh:  # also to the file when it is open
                try:  # a full disk or an already closed file must not break log collection
                    self._fh.write(line + "\n")  # one line per log entry
                    self._fh.flush()  # so the file is current while the job runs
                except (OSError, ValueError):  # disk full (OSError) or the file was closed meanwhile (ValueError)
                    pass  # the in-memory log still has the line

    # Reader thread: copy every stderr line (the toolkit's log) into the job log until the pipe closes.
    def _read_stderr(self) -> None:
        # iter() with a sentinel calls readline() until it returns b"" at end of file
        for raw in iter(self.proc.stderr.readline, b""):
            # decode tolerantly and drop the line ending (\r\n on Windows)
            self._add(raw.decode("utf-8", "replace").rstrip("\r\n"))

    # Reader thread: watch stdout for the command's JSON result. In --json mode the toolkit prints only that object
    # there; anything else that slips through is kept in the log, marked as stdout.
    def _read_stdout(self) -> None:
        for raw in iter(self.proc.stdout.readline, b""):  # one line at a time until end of file
            line = raw.decode("utf-8", "replace").rstrip("\r\n")  # text without the line ending
            obj = _json_line(line)  # a JSON object, or None
            if obj is not None and "ok" in obj:  # the result object always has an "ok" key
                # The last such object wins, in case a stage printed a JSON line of its own earlier.
                self._json = obj                       # the command's one JSON object (the last one wins)
            elif line.strip():  # any other non-blank stdout line, such as a stray print from stage code
                # Shown with a "stdout:" prefix so it is clearly not from the toolkit's log.
                self._add("stdout: " + line)           # stray prints from stage code

    # Supervisor thread: wait for the child, settle the outcome (done, failed or cancelled), reshape the result,
    # write a JSON summary next to the log, and signal `done`. Runs once per job.
    def _supervise(self) -> None:
        # one reader per pipe, so neither pipe can fill up and stall the child
        readers = [threading.Thread(target=f, daemon=True) for f in (self._read_stderr, self._read_stdout)]
        for t in readers:  # launch the stderr and stdout readers before waiting on the child
            t.start()  # each reader now drains its pipe until end of file
        rc = self.proc.wait()  # blocks until the child exits; rc is its exit code
        for t in readers:  # give the readers time to drain what is left
            # Wait at most 10 s per reader: a grandchild that inherited a pipe could keep it open forever.
            t.join(timeout=10)                         # a grandchild holding a pipe must not hang the job
        res = self._json  # the parsed result object, if any
        if self.cancel_requested:  # job_cancel or shutdown asked for the stop, so the outcome is cancelled
            # outcome reads cancelled even though the exit code is non-zero
            state, err = "cancelled", "cancelled on request"
        # exit code 0 and the result does not report a failure
        elif rc == 0 and res is not None and res.get("ok", True) is not False:
            state, err = "done", None  # a successful job: state done, with no error message
            if self.post:  # a reshaping function was given (stage jobs)
                try:  # reshaping must not turn a success into a crash
                    res = self.post(res)  # e.g. pick the one stage's step out of a run report
                # A reshaping bug only costs the nicer shape; the raw result is kept.
                except Exception as e:  # noqa: BLE001
                    self._add(f"mcp: could not reshape the result ({type(e).__name__}: {e})")  # noted in the job log
        else:  # non-zero exit or ok=false
            # best error message from the result or the log tail
            state, err = "failed", _failure(res, list(self.lines), rc)
        # store result, error text, exit code and end time before the state changes
        self.result, self.error, self.exit_code, self.ended = res, err, rc, time.time()
        # final line in the log with the duration
        self._add(f"# {state} (exit code {rc}) after {self.ended - self.started:.1f} s")
        # Set the state last: a reader that sees the final state is guaranteed to see the result and error as well.
        self.state = state                             # after the result: whoever sees the final state sees it too
        if self.log_file:  # a log file exists
            try:  # writing the JSON summary is optional, so its errors are swallowed
                # <same name>.json: the full job view without the log tail, kept after the server stops
                self.log_file.with_suffix(".json").write_text(_compact(self.view(0)), encoding="utf-8")
            except OSError:  # disk problems are ignored
                pass  # the in-memory record is what matters
        self._close_log()  # close the log file
        self.done.set()  # wake anyone waiting for this job
        _log(f"job {self.id} {state} in {self.ended - self.started:.1f} s")  # one line in the server's own log

    # Close the job's log file if it is open, under the lock so a reader thread is not writing at that moment.
    def _close_log(self) -> None:
        with self._lock:  # no reader may write while closing
            if self._fh:  # a log file is open, so it needs closing
                try:  # close() can raise on a full disk while it flushes
                    self._fh.close()  # writes any buffered lines to disk, then releases the handle
                except OSError:  # nothing useful to do about it
                    pass  # the handle is dropped either way
                self._fh = None  # mark as closed so _add() stops writing

    # Stop the job: SIGTERM to its process group (taskkill on Windows), then SIGKILL if it has not ended within
    # `grace` seconds (3 by default). Blocks for up to about twice `grace`, so async callers use a worker thread.
    # Returns True if a running process was stopped, False if the job had already finished or never started.
    def cancel(self, grace: float = 3.0) -> bool:
        if self.done.is_set() or self.proc is None:  # finished, or the process never started
            return False  # False tells job_cancel the job was not running: finished already or never started
        # The process has exited but the supervisor has not recorded it yet.
        if self.proc.poll() is not None:               # already exited, only the bookkeeping is left
            self.done.wait(grace)  # give the supervisor a moment to settle the outcome
            return False  # it ended on its own, so this call cancelled nothing
        self.cancel_requested = True  # must be set before the signal, so the supervisor reports "cancelled"
        _kill_tree(self.proc, hard=False)  # polite stop first: stages can clean up, narration keeps finished segments
        if not self.done.wait(grace):  # not finished within the grace period
            # SIGKILL the process group (taskkill /F on Windows) after the grace period
            _kill_tree(self.proc, hard=True)
            self.done.wait(grace)  # and wait once more for the bookkeeping
        return True  # it was running and has been told to stop

    # ---- reporting
    # The newest log line, or None when the log is empty. Used to send progress messages while a call waits.
    def last_line(self) -> str | None:
        with self._lock:  # lock: a reader thread may be appending
            return self.lines[-1] if self.lines else None  # newest entry, or None

    # The job as a JSON-able dict for tool results. log_lines: how many of the newest log lines to include;
    # brief: True for the short form used by job_list. Includes the result, error, log tail, log file, command and
    # pid in the full form, plus a hint to keep polling while it runs. No side effects.
    def view(self, log_lines: int = 40, brief: bool = False) -> dict:
        state = self.state  # read once so every field below describes the same state
        # ok is false only for failed jobs; identity fields follow
        d = {"ok": state != "failed", "job_id": self.id, "kind": self.kind, "name": self.name,
             # what it works on, its state, exit code and start time
             "project": self.project, "state": state, "exit_code": self.exit_code, "started": _iso(self.started),
             # running time so far, or the final duration, rounded to 0.1 s
             "seconds": round((self.ended or time.time()) - self.started, 1)}
        if brief:  # job_list wants just the summary
            return d  # without log and result
        if self.error:  # failed or cancelled jobs carry a message
            d["error"] = self.error  # add the failure or cancellation message to the full view
        d["result"] = self.result  # the command's JSON result (None while running)
        with self._lock:  # consistent snapshot of the ring buffer
            n = max(0, int(log_lines))  # negative or odd values are made safe
            d["log"] = list(self.lines)[-n:] if n else []  # the newest n lines, or none at all for 0
            d["log_lines_total"] = self.total_lines  # how many lines exist in total (the ring may have dropped some)
        d["log_file"] = str(self.log_file) if self.log_file else None  # where the complete log is
        d["command"] = self.argv  # the exact command line
        d["pid"] = self.proc.pid if self.proc else None  # process id, useful for looking it up in a task manager
        if state == "running":  # a job still running gets a hint telling the model how to follow it
            # tell the model what to do next
            d["next"] = f"poll job_status with job_id \"{self.id}\" until state is not running (or job_cancel)"
        return d  # the full view, ready to serialise into the tool result


# The table of jobs this server has started: starts new ones (one per project at a time), finds them by id,
# lists them, and stops them when the server shuts down. One instance, JOBS, exists per server process.
class Jobs:
    # Start with no jobs.
    def __init__(self):
        self._jobs: dict[str, Job] = {}  # job id -> Job, oldest first (dicts keep insertion order)
        # An RLock may be taken again by the thread that holds it: Python runs signal handlers on the main thread, so
        # the SIGTERM handler can need the lock while the main thread is inside a locked section.
        self._lock = threading.RLock()                 # re-entrant: the SIGTERM handler may run on a holder

    # Start a job unless its project is busy. kind, name, args, project, post: as for Job. Raises ToolError (with
    # the busy job's view and its last 5 log lines) when another job is still running on the same project, or when
    # the process cannot
    # start. Also forgets the oldest finished jobs beyond KEEP_FINISHED. Returns the started Job.
    def start(self, kind: str, name: str, args: list[str], project: str | None = None,
              post: Callable[[dict], dict] | None = None) -> Job:  # post reshapes a successful result
        with self._lock:  # check and start atomically, so two calls cannot both start on one project
            if project:  # project jobs only; tool jobs never conflict
                for j in self._jobs.values():  # look through every job for one already busy with this project
                    if j.project == project and not j.done.is_set():  # same project folder and not finished yet
                        # refuse: two runs on one project would overwrite each other's files
                        raise ToolError(f"project {project} is busy with job {j.id} ({j.kind} {j.name}); wait for "
                                        # include the busy job's view with its last 5 log lines
                                        "it (job_status) or stop it (job_cancel)", j.view(5))
            # create the job record; its id and log file name are chosen here
            job = Job(kind, name, args, project, post)
            job.start()  # open its log and start the process
            self._jobs[job.id] = job  # add it to the table so job_status can find it by id
            done = [k for k, j in self._jobs.items() if j.done.is_set()]  # ids of finished jobs, oldest first
            for k in done[:max(0, len(done) - KEEP_FINISHED)]:  # keep only the newest KEEP_FINISHED finished jobs
                del self._jobs[k]  # forget the oldest beyond that limit
        _log(f"job {job.id} started: {' '.join(args[:3])}")  # log the first three words of the command
        return job  # the started job, still running, for the caller to wait on

    # Find a job by id. job_id: the id given to the client (surrounding spaces ignored). Raises ToolError naming
    # recent job ids when it is unknown (jobs are forgotten when the server restarts). Returns the Job.
    def get(self, job_id: str) -> Job:
        with self._lock:  # safe against concurrent starts
            job = self._jobs.get(str(job_id).strip())  # the job, or None
            known = list(self._jobs)[-10:]  # the 10 most recent ids for the error message
        if job is None:  # no job with that id, e.g. a typo or one from before a server restart
            # list recent ids, or explain that this server has not run anything yet
            raise ToolError(f"no job {job_id!r}" + (f"; recent jobs: {', '.join(known)}" if known else
                                                     "; this server has run no jobs (jobs end with the server)"))
        return job  # the matching Job object

    # A snapshot list of every job (running and finished), so callers can iterate without holding the lock.
    def all(self) -> list[Job]:
        with self._lock:  # copy under the lock so a concurrent start cannot change the dict mid-copy
            return list(self._jobs.values())  # a separate list the caller may iterate while jobs come and go

    # The jobs that have not finished yet.
    def running(self) -> list[Job]:
        return [j for j in self.all() if not j.done.is_set()]  # done not set means still running

    # Called when the server stops: ask every running job to stop (SIGTERM), give them together up to 1.5 s, then
    # kill any that are left. Jobs do not outlive the server.
    def shutdown(self) -> None:
        """Server stopping: stop running jobs (TERM, then KILL)."""
        run = self.running()  # snapshot of the running jobs
        for j in run:  # ask every running job to stop politely (taskkill still forces on Windows)
            j.cancel_requested = True  # report them as cancelled rather than failed
            _kill_tree(j.proc, hard=False)  # polite stop to the whole process group
        deadline = time.time() + 1.5  # one shared deadline 1.5 s from now
        for j in run:  # wait for each job in turn against the shared 1.5 s deadline
            # wait the remaining time; the deadline is shared, so the total stays 1.5 s
            if not j.done.wait(max(0.0, deadline - time.time())):
                _kill_tree(j.proc, hard=True)  # still running: kill it

    # Kill every running job at once (SIGKILL). Used by the SIGTERM handler, where there is no time for a grace
    # period.
    def kill_all(self) -> None:
        for j in self.running():  # every job that has not finished yet
            j.cancel_requested = True  # mark it as cancelled
            _kill_tree(j.proc, hard=True)  # SIGKILL the whole process group with no grace period


# The one job table of this server process.
JOBS = Jobs()


# ------------------------------------------------------------------------------------------- schemas
# Schema for a list of strings (stage names). desc: the description shown to the model. Returns the schema dict.
def _arr(desc: str) -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": desc}  # array whose items are strings


# Schema for the `project` argument shared by most tools: a folder path on the server's machine.
PROJECT = {"type": "string", "description": "project folder (absolute, or relative to the server's working folder)"}
# Schema for accept_draft: by default the first full run stops after `check` so the extracted text can be read
# and corrected before hours of narration.
ACCEPT_DRAFT = {"type": "boolean", "default": False,
                "description": "go on past the first `check` (narrate the extracted draft) instead of stopping so "
                               "work/book.md can be reviewed"}
# The planner arguments shared by project_plan and project_run, mirroring `speechkit-narrate run` options.
RUN_PROPS = {
    # positional stage names; whatever they need is added, as in speechkit-narrate run DIR TARGET
    "targets": _arr("stages to bring up to date, with what they need (default: the whole default pipeline)"),
    # each name becomes a --force=NAME flag: run it even when up to date
    "force": _arr("run these even if up to date ('all' for every selected stage)"),
    # each name becomes an --only=NAME flag: just that stage, no prerequisites
    "only": _arr("run just these stages, without what they need"),
    "skip": _arr("leave these stages out"),  # each name becomes a --skip=NAME flag: left out of the run
    # one stage name passed as --from=NAME: it and every later stage are forced
    "from": {"type": "string", "description": "force this stage and everything after it"},
}


# Schema for wait_seconds: how long a call waits for its job before returning the job_id. default: the
# default number of seconds. Returns the schema dict.
def _wait(default: float) -> dict:
    return {"type": "number", "minimum": 0, "default": default,  # zero or more seconds, with the given default
            # the description states the default; :g prints 30.0 as 30
            "description": f"seconds to wait for the job before returning its job_id (default {default:g}; "
                           "0 = return at once)"}


# Schema for an object argument list. props: property schemas; required: names that must be given; extra: allow
# undeclared keys (False by default, so typos are rejected). Returns the schema dict.
def _obj(props: dict, required=(), extra: bool = False) -> dict:
    # JSON-schema object with exactly these properties
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": extra}


# Tool annotation hints for the client (fields of types.ToolAnnotations): read_only, destructive and idempotent.
# open_world is always False because every tool here works on local files only. Unset hints are left out.
def _ann(read_only=False, destructive=None, idempotent=None) -> dict:
    """Tool annotation hints (types.ToolAnnotations fields); every tool here works on local files only."""
    # the destructive hint only means something for tools that write, so it is dropped for read-only ones
    d = {"read_only_hint": read_only, "destructive_hint": None if read_only else destructive,
         # idempotent may be None (unknown); closed world for all
         "idempotent_hint": idempotent, "open_world_hint": False}
    return {k: v for k, v in d.items() if v is not None}  # drop the None values so the SDK uses its own defaults


# Build one MCP tool description. name: the tool name; title: a short human title; description: text for the
# model; schema: the JSON schema of its arguments; ann: annotation hints from _ann(). Returns a types.Tool.
def _tool(name: str, title: str, description: str, schema: dict, ann: dict) -> types.Tool:
    # input_schema is sent to clients as inputSchema
    return types.Tool(name=name, title=title, description=description, input_schema=schema,
                      annotations=types.ToolAnnotations(**ann))  # the hints as a ToolAnnotations object


# A Route is how one advertised tool name is served: a static tool with its own handler, a stage (run through
# `speechkit-narrate run`) or a standalone tool (run through `speechkit-narrate tool`). It also keeps what is needed to describe
# the tool again: schema, title, description and annotations.
@dataclass
class Route:  # one per tool name in the catalog
    kind: str                                         # static | stage | tool
    name: str                                         # stage / tool name in the registry
    schema: dict  # the JSON schema of the tool's arguments (also used for validation)
    handler: Callable | None = None  # the async handler of a static tool; None for stages and tools
    opt_map: dict = field(default_factory=dict)       # schema key -> stage option name
    info: dict = field(default_factory=dict)  # the stage or tool description from the registry
    title: str = ""  # short human title shown by clients
    description: str = ""  # text for the model
    ann: dict = field(default_factory=dict)  # annotation hints from _ann()

    # Describe this route as an MCP tool under the name tool_name. description: optional text that replaces the
    # route's own (narrate_describe appends a registry error to it). Returns a types.Tool.
    def tool(self, tool_name: str, description: str | None = None) -> types.Tool:
        # fall back to the route's description
        return _tool(tool_name, self.title, description or self.description, self.schema, self.ann)


# The fixed tools (describe, project_*, job_*), filled by the @_static decorator below at import time.
STATIC: dict[str, Route] = {}


# Decorator that registers an async handler as a fixed tool. name: tool name; title, description: for clients;
# schema: argument schema; ann: annotation hints. Returns the decorator; the function is stored in STATIC.
def _static(name: str, title: str, description: str, schema: dict, ann: dict):
    # The decorator itself: store the route, return the function unchanged.
    def deco(fn):
        # a "static" route with the handler and its description
        STATIC[name] = Route("static", name, schema, fn, title=title, description=description, ann=ann)
        return fn  # the function stays usable on its own
    return deco  # applied by Python to the async def below it


# ------------------------------------------------------------------------------------------- the catalog
# A Catalog is one snapshot of the tool list: the MCP tools to advertise and how to serve each name, built from
# one `speechkit-narrate stages` description of the registry. It is rebuilt at every tools/list.
@dataclass
class Catalog:  # one snapshot of the advertised tools
    tools: list  # list of types.Tool, in the order clients see them
    routes: dict  # tool name -> Route
    describe: dict | None  # the registry description it was built from, or None if loading failed
    error: str | None  # why the registry did not load, or None
    at: float  # time.monotonic() when it was built, for the cache lifetime


# The latest catalog, reused by tools/call for CATALOG_TTL seconds.
_CATALOG: Catalog | None = None


# A unique tool name made of prefix + name, with characters outside [A-Za-z0-9_-] replaced by "_", cut to 64
# characters (the limit many clients enforce) and given a _2, _3 ... suffix on a clash. taken: names in use,
# updated here. Returns the name.
def _safe_name(prefix: str, name: str, taken: set) -> str:
    base = (prefix + NAME_RE.sub("_", name))[:64]  # e.g. "stage_" + "narrate", made safe and capped at 64 characters
    out, n = base, 2  # start with the plain name, suffix counter at 2
    while out in taken:  # clash with an existing tool name
        sfx = f"_{n}"  # try the suffix _2, then _3 and so on until the name is free
        out, n = base[:64 - len(sfx)] + sfx, n + 1  # shorten the base so name plus suffix still fits in 64 characters
    taken.add(out)  # mark the name as used so later tools cannot take it
    return out  # a tool name unique within this catalog and at most 64 characters long


# Argument names the stage tools use themselves; a stage option with one of these names is offered as option_<name>.
RESERVED = {"project", "accept_draft", "wait_seconds"}


# The MCP tool for one stage. st: the stage's dict from `speechkit-narrate stages`; tname: the tool name to use; delegate:
# the SPEECHKIT_DELEGATE command or None. The arguments are project, accept_draft, the stage's own options and
# wait_seconds. Returns (Route, types.Tool).
def _stage_tool(st: dict, tname: str, delegate: str | None) -> tuple[Route, types.Tool]:
    props = {"project": PROJECT, "accept_draft": ACCEPT_DRAFT}  # the fixed arguments every stage tool has
    opt_map = {}  # argument name -> the stage's own option name
    for k, sch in (st.get("options") or {}).items():  # each declared stage option
        key = k if k not in RESERVED else f"option_{k}"  # rename options that clash with the fixed arguments
        props[key] = sch  # the option's own schema, as declared by the stage
        opt_map[key] = k  # remember the original name for the command line
    props["wait_seconds"] = _wait(30)  # stage calls wait 30 s by default before handing back a job id
    schema = _obj(props, ["project"])  # only project is required
    notes = []  # sentences appended to the description
    if st.get("needs"):  # the stage has prerequisites
        # tell the model they run first if stale
        notes.append(f"Needs: {', '.join(st['needs'])} (any of these that are out of date run first).")
    if st.get("outputs"):  # the stage declares files or folders it produces
        notes.append(f"Makes: {', '.join(st['outputs'])}.")  # tell the model which outputs it will produce
    if st.get("heavy"):  # flagged heavy: narration or the pronunciation loop can take minutes to hours
        notes.append("Long-running: expect a job_id and poll job_status.")  # set expectations: poll the job
    if not st.get("runs_here", True):  # this machine cannot run it
        plats = "/".join(st.get("platforms") or [])  # its platforms, e.g. "linux"
        # it will be delegated, or the model learns how to enable delegation
        notes.append(f"Runs on {plats} only: " + (f"delegated through SPEECHKIT_DELEGATE ({delegate})." if delegate
                                                  # the suggestion names the WSL delegate
                                                  else "set SPEECHKIT_DELEGATE (e.g. \"wsl\") to run it here."))
    if st.get("source", "speechkit-narrate") != "speechkit-narrate":  # comes from a plug-in
        notes.append(f"Plug-in: {st['source']}.")  # name the plug-in module or file that provides this stage
    # the stage's description (or name), then what the tool does
    desc = (f"{(st.get('description') or st['name']).rstrip('.')}. Runs the `{st['name']}` stage now on a project "
            "(forced), as a background job: returns the result if it finishes within wait_seconds, else a job_id. "
            + " ".join(notes))  # the notes collected above, joined with spaces
    # stage route with the option name map and the stage's info
    route = Route("stage", st["name"], schema, opt_map=opt_map, info=st, title=f"Stage: {st['name']}",
                  # writes files but is not destructive; running it twice is not a no-op
                  description=desc.strip(), ann=_ann(destructive=False, idempotent=False))
    return route, route.tool(tname)  # the route and its tool description


# The MCP tool for one standalone tool. t: the tool's dict from `speechkit-narrate stages` (params is a full JSON schema);
# tname: the tool name to use. Adds wait_seconds unless the tool declares its own. Returns (Route, types.Tool).
def _tool_tool(t: dict, tname: str) -> tuple[Route, types.Tool]:
    # a deep copy via a JSON round trip, so adding wait_seconds never changes the registry description
    schema = json.loads(json.dumps(t.get("params") or _obj({})))
    schema.setdefault("type", "object")  # make sure it is an object schema
    props = schema.setdefault("properties", {})  # the properties dict (created if missing)
    if "wait_seconds" not in props:  # the tool does not use that name itself
        props["wait_seconds"] = _wait(30)  # 30 s default wait
    notes = ["Works on plain files, no project.",  # sentences for the description
             "Runs as a background job: returns the result if it finishes within wait_seconds, else a job_id."]
    if t.get("heavy"):  # the tool is declared heavy, such as narrating a long text
        # advise the model to expect a job_id and poll job_status
        notes.append("Long-running: expect a job_id and poll job_status.")
    if not t.get("runs_here", True):  # cannot run on this machine
        # unlike stage tools this does not mention SPEECHKIT_DELEGATE, which `speechkit-narrate tool` would still use
        notes.append(f"Runs on {'/'.join(t.get('platforms') or [])} only (not on this machine).")
    if t.get("source", "speechkit-narrate") != "speechkit-narrate":  # the tool comes from a plug-in rather than the built-in set
        notes.append(f"Plug-in: {t['source']}.")  # name the plug-in that provides this tool
    # the tool's description followed by the notes
    desc = f"{(t.get('description') or t['name']).rstrip('.')}. " + " ".join(notes)
    # tool route with its schema and info
    route = Route("tool", t["name"], schema, info=t, title=f"Tool: {t['name']}", description=desc,
                  # not read-only; no destructive hint, so clients assume it may overwrite files
                  ann=_ann(idempotent=False))
    return route, route.tool(tname)  # the route and its tool description


# Assemble the full tool list. desc: the registry description from `speechkit-narrate stages` (None if it failed); error:
# why it failed. Order: the fixed project tools, one stage_<name> per stage, one tool_<name> per tool, then the
# job tools. Returns a Catalog stamped with the current monotonic time.
def build_catalog(desc: dict | None, error: str | None) -> Catalog:
    tools, routes = [], {}  # MCP tools in order, and name -> Route
    # fixed tools that come first
    order = ["narrate_describe", "project_new", "project_status", "project_settings", "project_plan", "project_run"]
    for n in order:  # the six fixed tools, in the order clients list them
        r = STATIC[n]  # the route registered for this name by @_static
        # narrate_describe carries the registry error (first 300 characters) so the model sees it
        extra = f" (the registry did not load: {error[:300]})" if n == "narrate_describe" and error else ""
        tools.append(r.tool(n, r.description + extra))  # advertised under its own name
        routes[n] = r  # served by the static handler
    taken = set(STATIC)  # generated names must not clash with the fixed ones
    if desc:  # the registry description loaded, so stage and tool entries can be generated
        dlg = desc.get("delegate")  # the delegate command, for stage descriptions
        # stage tools, each with the delegate as an extra argument
        items = [("stage_", _stage_tool, st, (dlg,)) for st in desc.get("stages") or []]
        # one tool_<name> entry per standalone tool, which takes no delegate argument
        items += [("tool_", _tool_tool, tl, ()) for tl in desc.get("tools") or []]
        for prefix, make, item, extra in items:  # turn each stage or tool declaration into a route and an MCP tool
            try:  # one bad declaration must not break the whole list
                # unique name, then the route and tool
                r, t = make(item, _safe_name(prefix, str(item["name"]), taken), *extra)
            # A plug-in can declare an option schema the SDK rejects; that item is left out and the rest still appear.
            except Exception as e:  # noqa: BLE001 - one malformed plug-in declaration must not hide the rest
                # logged on stderr with the item's name
                _log(f"skipped {prefix}{item.get('name') if isinstance(item, dict) else item}: {type(e).__name__}: {e}")
                continue  # skip only this item; the remaining tools are still built
            tools.append(t)  # add the generated tool to the advertised list
            routes[t.name] = r  # and route calls to it
    for n in ("job_status", "job_list", "job_cancel"):  # the job tools come last
        r = STATIC[n]  # route of this job tool, registered by @_static
        tools.append(r.tool(n))  # advertised with its own description
        routes[n] = r  # served by the static handler
    return Catalog(tools, routes, desc, error, time.monotonic())  # the snapshot with its build time


# The current catalog. fresh=True always rebuilds it (tools/list); fresh=False reuses one younger than
# CATALOG_TTL seconds (tools/call). The registry is loaded in a child process, so plug-in code never runs inside
# the server and edited plug-ins appear without a restart. Returns the Catalog.
async def catalog(fresh: bool) -> Catalog:
    global _CATALOG  # the cache is module state
    c = _CATALOG  # read the module-level cache once into a local name
    if not fresh and c is not None and time.monotonic() - c.at < CATALOG_TTL:  # recent enough to reuse
        return c  # serve it from the cache
    try:  # loading the registry can fail, e.g. a broken plug-in or a timeout
        # `speechkit-narrate --json stages` on a worker thread, so the event loop stays responsive
        desc, err = await anyio.to_thread.run_sync(run_quick, ["stages"]), None
    except ToolError as e:  # the registry did not load (bad plug-in, timeout)
        desc, err = None, str(e)  # no stages or tools, only the reason
        _log(f"registry: {err}")  # log it on stderr as well
    _CATALOG = build_catalog(desc, err)  # build and cache the new snapshot
    return _CATALOG  # the newly built snapshot, now also the cached one


# ------------------------------------------------------------------------------------------- helpers
# Check call arguments against a tool's JSON schema (Draft 2020-12) and return readable messages such as
# "workers: 'four' is not of type 'integer'". The toolkit validates again; this only gives clearer errors.
# Returns an empty list when everything is valid, or when jsonschema is not installed.
def _validate(schema: dict, args: dict) -> list[str]:
    """Argument errors against the tool's input schema (the toolkit checks again; this gives clear messages)."""
    try:  # jsonschema is optional; without it validation is left to the toolkit
        # validator for JSON Schema draft 2020-12, used to check the call's arguments
        from jsonschema import Draft202012Validator
        # every error, sorted by where it is in the arguments
        found = sorted(Draft202012Validator(schema).iter_errors(args), key=lambda e: [str(p) for p in e.path])
    # No jsonschema, or a plug-in's schema it cannot read: skip local validation and let the toolkit check.
    except Exception:  # noqa: BLE001 - no jsonschema, or a plug-in's schema it cannot read: leave it to the toolkit
        return []  # no local messages; the toolkit will still check the arguments
    errs = []  # readable error strings, one per problem
    for e in found:  # each schema violation, in argument order
        where = ".".join(str(p) for p in e.path)  # dotted path of the faulty value, e.g. "options.narrate"
        errs.append(f"{where}: {e.message}" if where else e.message)  # prefix the path when there is one
    return errs  # the list of messages


# Turn a path argument into an absolute Path on the server's machine. p: the value from the client; what: the
# argument name for the error message. Translates a Windows path (C:\Users\me\Book) when the server runs inside WSL,
# expands "~" and resolves relative paths against the server's working folder. Raises ToolError when the value is not
# a non-empty string.
def _path(p: Any, what: str) -> Path:
    if not isinstance(p, str) or not p.strip():  # missing, empty or not a string
        raise ToolError(f"{what}: give a path")  # the message names the argument, e.g. input: give a path
    # a Windows path becomes the WSL one first (unchanged elsewhere); ~ becomes the home folder; resolve() makes it
    # absolute
    return Path(os.path.expanduser(winpaths.to_local(p.strip()))).resolve()


# One path argument of a standalone tool, ready for the toolkit: translated like _path (a Windows path under WSL) and
# with a leading "~" expanded, but not made absolute (the toolkit resolves it). v: the client's value; lists are handled
# item by item and other types come back unchanged.
def _path_arg(v: Any) -> Any:
    if isinstance(v, list):  # a list of paths
        return [_path_arg(x) for x in v]  # each item on its own
    if isinstance(v, str):  # a single path
        v = winpaths.to_local(v)  # Windows form to Linux form, when that applies
        if v.startswith("~"):  # a home-relative path
            v = os.path.expanduser(v)  # expand it here; the toolkit takes paths literally
    return v  # the prepared value


# Check and normalise a project folder argument. p: the client's value; must_exist: True to require a
# project.json inside. Returns the absolute folder as a string (also the key for the one-job-per-project rule).
def project_dir(p: Any, must_exist: bool = True) -> str:
    d = _path(p, "project")  # absolute Path of the folder; ToolError when it is empty or not text
    if must_exist and not (d / "project.json").is_file():  # it must already be a project
        # explain what is wrong with the folder
        hint = "it does not exist" if not d.exists() else "no project.json in it"
        # the folder is named the way the client wrote it (C:\... for a client that sends Windows paths)
        raise ToolError(f"{winpaths.present(str(d))} is not a speechkit-narrate project ({hint}); create one with project_new")
    return str(d)  # as text for command lines and job bookkeeping


# The planner flags for `speechkit-narrate run/plan` from the arguments targets, force, only, skip and from. Rejects empty
# names and names starting with "-" so a value can never be read as an extra command-line option.
# Returns the words to append to the command.
def run_flags(a: dict) -> list[str]:
    # every stage name given in any of the lists
    names = [str(s) for k in ("targets", "force", "only", "skip") for s in a.get(k) or []]
    names += [str(a["from"])] if a.get("from") else []  # plus --from, when given
    bad = [s for s in names if not s or s.startswith("-")]  # empty, or starting with "-"
    if bad:  # at least one name is empty or looks like a command-line option
        raise ToolError(f"not a stage name: {', '.join(map(repr, bad))}")  # refuse rather than pass them to argparse
    out = [str(t) for t in a.get("targets") or []]  # targets are positional words
    for key in ("force", "only", "skip"):  # list options are repeated
        out += [f"--{key}={s}" for s in a.get(key) or []]  # the name=value form keeps each name attached to its flag
    if a.get("from"):  # a from stage was given
        out.append(f"--from={a['from']}")  # --from=NAME as one word, so the name cannot become a separate option
    return out  # the flags in order


# A successful tool result. d: the JSON-able dict to return. It is sent twice: as compact JSON text (for clients
# that only read text content) and as structured_content (for clients that read structured results).
def ok_result(d: dict) -> types.CallToolResult:
    d = winpaths.outbound(d)  # drive paths in the Windows form for a client that sends Windows paths (a copy)
    # one text block with the JSON, plus the same dict as structured content
    return types.CallToolResult(content=[types.TextContent(type="text", text=_compact(d))], structured_content=d)


# A failed tool result (isError=true), which the model reads as an ordinary answer. msg: the error message;
# data: optional details (a job view, the command's result) included as JSON after the message.
def error_result(msg: str, data: dict | None = None) -> types.CallToolResult:
    data = winpaths.outbound(data) if data else data  # the details use the client's path form too (a copy)
    sc = {**(data or {}), "ok": False, "error": msg}  # the details plus ok=false and the message, as structured content
    # the message alone, or followed by the compact details on the next line
    text = msg if not data else msg + "\n" + _compact(data)
    # one text block plus the structured form
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], structured_content=sc,
                                is_error=True)  # is_error marks the call as failed without breaking the protocol


# The tool result for a job's current state. job: the Job; log_lines: how many log lines to include. A failed
# job becomes an error result with at least 40 log lines (enough to diagnose it); anything else is a normal one.
def job_result(job: Job, log_lines: int) -> types.CallToolResult:
    if job.state == "failed":  # the command exited non-zero or reported ok false
        # error result with the failure message and the full view
        return error_result(f"job {job.id} failed: {job.error}", job.view(max(log_lines, 40)))
    return ok_result(job.view(log_lines))  # running, done or cancelled


# Send a progress notification for the current call, if the client asked for progress. ctx: the request context;
# elapsed: seconds since the job started, used as the progress value (it only grows, as the protocol requires);
# message: the latest log line, cut to 300 characters. Errors are ignored.
async def _progress(ctx, elapsed: float, message: str | None) -> None:
    try:  # report_progress can fail, e.g. after the client disconnected
        # total=None: the length of a job is unknown; an empty message is sent as None
        await ctx.session.report_progress(round(elapsed, 1), None, (message or "")[:300] or None)
    # Progress is optional: a closed connection or a client without progress support must not fail the call.
    except Exception:  # noqa: BLE001, S110 - progress is best-effort
        pass  # progress is optional, so the call goes on as if it had been sent


# Wait up to `wait` seconds for a job, reporting progress while it runs, then return its view. ctx: the request
# context; job: the Job; wait: the client's wait_seconds (any value; invalid becomes 0, the maximum is MAX_WAIT).
# Returns the result if the job finished, else the running job's view with its id for polling.
async def wait_for(ctx, job: Job, wait: Any) -> types.CallToolResult:
    try:  # the value came from the client
        wait = max(0.0, min(float(wait), MAX_WAIT))  # clamp to 0 .. 3600 seconds
    except (TypeError, ValueError):  # wait_seconds was not a number, e.g. null or text
        wait = 0.0  # do not wait at all; the client gets the job id at once
    deadline = time.monotonic() + wait  # when to give up waiting, on the monotonic clock
    last = None  # last log line already reported
    while not job.done.is_set():  # until the job finishes
        left = deadline - time.monotonic()  # seconds remaining before the wait runs out
        if left <= 0:  # the requested wait is over, though the job continues in the background
            break  # return the running job's view
        await anyio.sleep(min(0.1, left))  # short naps keep the server responsive and the result prompt
        line = job.last_line()  # the newest log line, read under the job's lock
        if line != last:  # only send progress when the log has moved on
            last = line  # remember it so the same line is not reported twice
            await _progress(ctx, time.time() - job.started, line)  # report it as progress
    # 12 log lines for a finished job, 8 while it is still running
    return job_result(job, 12 if job.done.is_set() else 8)


# Start a job and wait for it as wait_for() does. ctx: request context; kind, name, args, project, post: as for
# Jobs.start; wait: the client's wait_seconds. Returns the tool result; a busy project raises ToolError.
async def start_job(ctx, kind: str, name: str, args: list[str], project: str | None, wait: Any,
                    # post reshapes the successful result
                    post: Callable[[dict], dict] | None = None) -> types.CallToolResult:
    # spawning a process can block for a moment (especially on Windows), so it runs on a worker thread
    job = await anyio.to_thread.run_sync(lambda: JOBS.start(kind, name, args, project, post))
    return await wait_for(ctx, job, wait)  # wait up to wait_seconds, then return the result or the job id


# Run a short toolkit command on a worker thread and return its JSON result. args: the words after `speechkit-narrate
# --json`. Raises ToolError on failure.
async def quick(args: list[str]) -> dict:
    return await anyio.to_thread.run_sync(run_quick, args)  # run_quick blocks; the event loop must not


# A result reshaper for stage jobs. name: the stage. `speechkit-narrate run` reports every step of the pipeline; the
# returned function picks this stage's step, so stage_<name> answers with just its own result.
def _stage_post(name: str) -> Callable[[dict], dict]:
    # The reshaper: res is the run report from `speechkit-narrate --json run`.
    def post(res: dict) -> dict:
        # this stage's step, or an empty dict if absent
        step = next((s for s in res.get("steps") or [] if s.get("stage") == name), {})
        # project and stage, the step's action
        return {"ok": True, "project": res.get("project"), "stage": name, "action": step.get("action"),
                # its result and duration, and where the pipeline stopped (if it did)
                "result": step.get("result"), "seconds": step.get("seconds"), "stopped": res.get("stopped")}
    return post  # handed to Jobs.start as `post`


# ------------------------------------------------------------------------------------------- static tools
# The fixed tools follow. Each @_static(...) gives the tool name, a title, the description the model reads,
# the argument schema and annotation hints; the async function below serves the call. Every handler takes
# ctx (the request context) and a (the validated arguments as a dict) and returns a CallToolResult.
# narrate_describe: the registry as JSON (stages, tools, plug-ins, platform, delegate).
@_static("narrate_describe", "Describe the toolkit",
         "What this speechkit-narrate toolkit can do: every stage (needs, inputs, outputs, settings, options, platforms, "
         "plug-in source, whether it runs here), the standalone tools with their parameters, the loaded plug-ins, "
         "this platform and the delegate (e.g. WSL) used for Linux-only stages. Give `project` to include that "
         "project's own plug-ins. Call this first.",
         # one optional argument: a project whose own plug-ins should be included
         _obj({"project": {**PROJECT, "description": "optional project folder: adds its own plug-ins"}}),
         _ann(read_only=True, idempotent=True))  # reads only; the same answer every time
# Serve narrate_describe with `speechkit-narrate --json stages [DIR]`.
async def t_describe(ctx, a):
    # add the project folder when given (it must be a project)
    args = ["stages"] + ([project_dir(a["project"])] if a.get("project") else [])
    res = await quick(args)  # the registry description from the toolkit
    res["windows_paths"] = winpaths.status()  # whether Windows paths are accepted here, so the model knows
    return ok_result(res)  # the registry description as the result


# project_new: make a project folder from a text file.
@_static("project_new", "New project",
         "Make a project folder from a text file (.txt, .md or a SpeechKit request .json): copies the input into "
         "<project>/input and writes project.json (with `settings` merged in, e.g. {\"spoken\": {\"code_names\": "
         "true}} to read names from source code the way a programmer says them). Next: project_plan, then "
         "project_run.",
         _obj({"project": {"type": "string", "description": "folder to create (must not hold a project yet)"},
               "input": {"type": "string", "description": "the text: .txt, .md or a SpeechKit request .json"},
               "title": {"type": "string", "description": "default: the file name"},
               "subtitle": {"type": "string"}, "author": {"type": "string"},
               "settings": {"type": "object", "description": "project.json settings to start with"}},
              ["project", "input"]),
         _ann(destructive=False, idempotent=False))  # writes files; a second call fails because project.json exists
# Serve project_new with `speechkit-narrate --json new DIR --input FILE ...`. The required arguments are checked here
# too, so a call without them gets a clear message even when jsonschema (the schema validator) is not installed.
async def t_new(ctx, a):
    for k in ("project", "input"):
        if not a.get(k):
            raise ToolError(f"project_new needs `{k}`")
    d = project_dir(a["project"], must_exist=False)  # absolute folder path; it need not exist yet
    args = ["new", d, f"--input={_path(a['input'], 'input')}"]  # the input as an absolute path, in --input=... form
    for k in ("title", "subtitle", "author"):  # the optional text fields, passed only when the client gave them
        if a.get(k):
            args.append(f"--{k}={a[k]}")  # --title=value form, so a title starting with "-" stays a value
    if a.get("settings"):
        if not isinstance(a["settings"], dict):
            raise ToolError("`settings` must be an object")
        args.append("--set=" + json.dumps(a["settings"], ensure_ascii=False))
    return ok_result(await quick(args))  # the new project's settings


# project_status: per-stage done flags, finish times and errors, plus narration progress and exported files.
@_static("project_status", "Project status",
         "What is done in a project: per stage done / finished time / adopted / missing outputs / last error, "
         "the narration progress and the exported files.",
         # just the project folder; read-only and repeatable
         _obj({"project": PROJECT}, ["project"]), _ann(read_only=True, idempotent=True))
# Serve project_status with `speechkit-narrate --json status DIR`.
async def t_status(ctx, a):
    # per-stage state, narration progress and exported files
    return ok_result(await quick(["status", project_dir(a["project"])]))


# project_settings: show project.json, or deep-merge changes into it.
@_static("project_settings", "Project settings",
         "Show project.json (the project's own settings; effective=true shows every setting with its default), or "
         "deep-merge the `set` object into it, e.g. {\"subtitle\": \"...\", \"spoken\": {\"code_names\": true}}. "
         "Changed settings make the stages that depend on them run again.",
         # arguments: the required project folder, an optional set object and the effective switch
         _obj({"project": PROJECT,
               # the changes to merge, as an object
               "set": {"type": "object", "description": "settings to deep-merge into project.json"},
               "effective": {"type": "boolean", "default": False,  # show the effective settings, defaults included
                             # effective defaults to false; only project is required
                             "description": "show every setting, defaults included"}}, ["project"]),
         # writes project.json when `set` is given; the same `set` twice gives the same file
         _ann(destructive=False, idempotent=True))
# Serve project_settings with `speechkit-narrate --json settings DIR [--set=JSON] [--effective]`.
async def t_settings(ctx, a):
    args = ["settings", project_dir(a["project"])]  # speechkit-narrate settings DIR; the folder must already hold a project.json
    if a.get("set") is not None:  # an empty {} is still passed (it changes nothing)
        args.append("--set=" + json.dumps(a["set"], ensure_ascii=False))  # the object as one JSON argument
    if a.get("effective"):  # the client asked for every setting with its default filled in
        args.append("--effective")  # the CLI then merges the defaults into the reported settings
    return ok_result(await quick(args))  # the settings as stored (or effective)


# project_plan: what project_run would do with the same arguments, without running anything.
@_static("project_plan", "Plan a run",
         "What project_run would do with the same arguments, and why, without running anything: one step per "
         "stage with action run|skip|adopt|blocked, its reasons and where it runs (here or delegate).",
         # project plus the planner arguments
         _obj({"project": PROJECT, **RUN_PROPS, "options": {"type": "object"}}, ["project"]), _ann(read_only=True, idempotent=True))
# Serve project_plan with `speechkit-narrate --json plan DIR [targets] [--force=...] ...`.
async def t_plan(ctx, a):
    args = ["plan", project_dir(a["project"]), *run_flags(a)]
    if a.get("options"):
        args.append("--options=" + json.dumps(a["options"], ensure_ascii=False))
    return ok_result(await quick(args))  # the plan, one step per stage


# project_run: bring the project up to date as a background job.
@_static("project_run", "Run the pipeline",
         "Bring a project's stages up to date as a background job: only what is missing or changed runs (see "
         "project_plan), in dependency order. A first full run stops after `check` so work/book.md can be "
         "reviewed, unless accept_draft=true. Returns the result if it finishes within wait_seconds, else a "
         "job_id to poll with job_status. Stages from a project's own plug-ins run here too, e.g. "
         "only=[name], force=[name].",
         # project, the planner arguments and accept_draft
         _obj({"project": PROJECT, **RUN_PROPS, "accept_draft": ACCEPT_DRAFT,
               # per-stage options: an object whose values are objects
               "options": {"type": "object", "additionalProperties": {"type": "object"},
                           "description": "stage options, e.g. {\"narrate\": {\"workers\": 4}}"},
               "wait_seconds": _wait(20)}, ["project"]),  # runs wait 20 s by default before returning a job id
         _ann(destructive=False, idempotent=True))  # marked idempotent: a second run only redoes what changed
# Serve project_run with `speechkit-narrate --json run DIR ...` as a job.
async def t_run(ctx, a):
    d = project_dir(a["project"])  # the project must exist
    args = ["run", d, *run_flags(a)]  # base command with the planner flags
    if a.get("accept_draft"):  # go past the review stop
        args.append("--accept-draft")  # --accept-draft for the run
    if a.get("options"):  # per-stage options were given, e.g. the number of narrate workers
        args.append("--options=" + json.dumps(a["options"], ensure_ascii=False))  # passed as one JSON argument
    # one job per project; result or job id within wait_seconds
    return await start_job(ctx, "run", "run", args, d, a.get("wait_seconds", 20))


# job_status: the state, result and log tail of a job.
@_static("job_status", "Job status",
         "State of a background job: running|done|failed|cancelled, exit code, its result (the command's JSON), "
         "the last log_lines lines of its log, when it started and how many seconds it has run. A failed job is "
         "reported with isError and its error message.",
         _obj({"job_id": {"type": "string"},  # the job id returned by an earlier call
               # how much of the log to include
               "log_lines": {"type": "integer", "minimum": 0, "default": 40, "description": "log lines to return"}},
              ["job_id"]),  # the required list: job_id must be given, log_lines is optional
         # reading only; not idempotent because the answer changes while the job runs
         _ann(read_only=True, idempotent=False))
# Serve job_status from the in-memory job table (no subprocess).
async def t_job_status(ctx, a):
    # unknown ids raise ToolError; failed jobs come back as error results
    return job_result(JOBS.get(a["job_id"]), a.get("log_lines", 40))


# job_list: every job of this server, newest first.
@_static("job_list", "List jobs",
         "The background jobs of this server, newest first, with state, project and seconds.",
         # no arguments; read-only, though the answer changes over time
         _obj({}), _ann(read_only=True, idempotent=False))
# Serve job_list from the in-memory job table: short views, newest first, plus how many still run.
async def t_job_list(ctx, a):
    jobs = sorted(JOBS.all(), key=lambda j: j.started, reverse=True)  # newest first by start time
    # brief views without log or result, to keep the list small
    return ok_result({"ok": True, "jobs": [j.view(brief=True) for j in jobs],
                      "running": sum(1 for j in jobs if j.state == "running")})  # how many are still running


# job_cancel: stop a running job and its processes.
@_static("job_cancel", "Cancel a job",
         "Stop a running job and the processes it started. Stages pick up where they can when run again "
         "(narration keeps the segments it finished).",
         # the job id; destructive because work in progress is lost; repeating it is harmless
         _obj({"job_id": {"type": "string"}}, ["job_id"]), _ann(destructive=True, idempotent=True))
# Serve job_cancel: stop the job on a worker thread and say whether this call stopped it.
async def t_job_cancel(ctx, a):
    job = JOBS.get(a["job_id"])  # find the job (ToolError if unknown)
    # cancel() blocks up to about 6 s, so it runs on a worker thread
    stopped = await anyio.to_thread.run_sync(job.cancel)
    out = job.view(20)  # the job's view after stopping, with 20 log lines
    out["cancelled"] = stopped  # whether this call stopped it
    if not stopped:  # the job had already ended before this call
        out["note"] = f"job was already {job.state}"  # say what state it was already in
    return ok_result(out)  # always a normal result: cancelling a finished job is not an error


# ------------------------------------------------------------------------------------------- dynamic tools
# Serve a stage_<name> call: run that stage now on a project, after any stale prerequisites, as a job.
# ctx: request context; route: the stage's Route; a: the validated arguments. Returns the tool result.
async def run_stage(ctx, route: Route, a: dict):
    d = project_dir(a["project"])  # the project must exist
    # map tool arguments back to the stage's option names; fixed arguments are not options
    opts = {route.opt_map[k]: v for k, v in a.items() if k in route.opt_map}
    st = route.name  # the stage's registry name, used in the command and its options
    # `run DIR <stage> --force=<stage>`: the planner brings stale needs up to date, then runs this stage even if
    # current.
    args = ["run", d, st, f"--force={st}"]             # like the CLI: stale prerequisites first, then the stage
    if a.get("accept_draft"):  # go past the review stop
        args.append("--accept-draft")  # passes --accept-draft to the run
    if opts:  # the client set some of this stage's own options
        args.append("--options=" + json.dumps({st: opts}, ensure_ascii=False))  # passed under the stage's name
    # 30 s default wait; the reshaper picks this stage's step from the run report
    return await start_job(ctx, "stage", st, args, d, a.get("wait_seconds", 30), _stage_post(st))


# Serve a tool_<name> call: run a standalone tool on plain files as a job. ctx: request context; route: the
# tool's Route; a: the validated arguments. Returns the tool result.
async def run_tool(ctx, route: Route, a: dict):
    # does the tool itself declare a wait_seconds parameter?
    own_wait = "wait_seconds" in ((route.info.get("params") or {}).get("properties") or {})
    # Either way the call waits as long as the client asked: a tool that declares wait_seconds also receives it as
    # its own argument; otherwise the value is the server's alone and is not passed on.
    wait = a.get("wait_seconds", 30) if own_wait else a.pop("wait_seconds", 30)
    props = route.schema.get("properties") or {}  # the argument schema (with wait_seconds added)
    params = {}  # the arguments sent to the tool
    for k, v in a.items():  # copy each argument, preparing the path arguments
        if (props.get(k) or {}).get("format") == "path":  # the schema declares this argument a path
            v = _path_arg(v)  # a Windows path under WSL becomes the Linux one, and ~ is expanded
        params[k] = v  # store the argument, prepared or as given
    # `speechkit-narrate --json tool NAME --params=JSON`
    args = ["tool", route.name, "--params=" + json.dumps(params, ensure_ascii=False)]
    # tool jobs have no project, so they never block each other
    return await start_job(ctx, "tool", route.name, args, None, wait)


# tools/list handler: rebuild the catalog from the registry every time, so plug-in edits and SPEECHKIT_PLUGINS
# changes show up without restarting. ctx: request context; params: pagination (unused). Returns every tool.
async def on_list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=(await catalog(fresh=True)).tools)  # no pagination: the list is short


# tools/call handler: find the route for the tool name, validate the arguments and run it. ctx: request
# context; params: name and arguments. Returns a CallToolResult; an unknown tool raises MCPError (a protocol
# error, as the specification asks), every other failure becomes an error result.
async def on_call_tool(ctx, params) -> types.CallToolResult:
    name = params.name  # the tool name the client called, e.g. stage_narrate
    args = dict(params.arguments or {})  # a mutable copy; missing arguments become {}
    if name in STATIC:  # fixed tools need no catalog
        route = STATIC[name]  # fixed tools are served straight from the STATIC table
    else:  # a stage_ or tool_ name generated from the registry
        cat = await catalog(fresh=False)  # the cached catalog, if it is recent
        route = cat.routes.get(name)  # look the name up
        if route is None:  # not there: the registry may have changed since
            cat = await catalog(fresh=True)  # rebuild the catalog once, in case a plug-in was just added
            route = cat.routes.get(name)  # look the name up in the rebuilt catalog
        if route is None:  # the name is still unknown after the rebuild
            # mention why the registry did not load, if it did not
            hint = f" (the registry did not load: {cat.error})" if cat.error else ""
            # JSON-RPC "invalid params" (-32602) error for an unknown tool name
            raise MCPError(code=types.INVALID_PARAMS, message=f"Unknown tool: {name}{hint}")
    errs = _validate(route.schema, args)  # check the arguments against the schema
    if errs:  # the arguments break the schema
        # reported as a tool error, at most 8 messages
        return error_result(f"{name}: invalid arguments: " + "; ".join(errs[:8]))
    try:  # run the call; failures become error results instead of protocol errors
        if route.kind == "static":  # a fixed tool such as project_status or job_cancel
            return await route.handler(ctx, args)  # call that tool's own async handler
        if route.kind == "stage":  # a generated stage_<name> tool
            return await run_stage(ctx, route, args)  # run the stage as a job
        return await run_tool(ctx, route, args)  # everything else is a standalone tool
    except ToolError as e:  # a foreseen problem such as a bad path or a busy project
        return error_result(str(e), e.data)  # becomes an error result with its details
    # Any other exception is a bug; it is logged and returned so the connection stays up.
    except Exception as e:  # noqa: BLE001 - a server bug must not take the connection down
        _log(f"{name}: {type(e).__name__}: {e}")  # log the exception on stderr
        return error_result(f"{name}: internal error: {type(e).__name__}: {e}")  # the model still gets an answer


# ------------------------------------------------------------------------------------------- server
# Create the low-level MCP server with its identity, the instructions for the model and the two request
# handlers. Only tools are offered, so the SDK advertises just the tools capability. Returns the Server.
def build_server() -> Server:
    # server name and the toolkit version; title for client displays
    return Server("speechkit-narrate", version=toolkit_version(), title="speechkit-narrate",
                  # one-line description of the server
                  description="Offline neural narration with word timings and a pronunciation audit",
                  # handlers passed to the constructor, the SDK 2.x style; a sentence about Windows paths is added when
                  # they are accepted here
                  instructions=INSTRUCTIONS + winpaths.note(), on_list_tools=on_list_tools, on_call_tool=on_call_tool)


# Why the MCP server cannot start, or None if it can: the SDK is missing, or it is an older release whose low-level
# Server has no on_list_tools constructor argument (1.x registers handlers with decorators instead).
def _sdk_problem() -> str | None:
    if _MCP_ERROR is not None:  # the import at the top failed
        # show the import error and the pip command
        return f"the MCP Python SDK is not installed ({_MCP_ERROR}); pip install \"mcp>={MIN_SDK}\""
    # the constructor does not accept the handlers: an older SDK
    if "on_list_tools" not in inspect.signature(Server.__init__).parameters:
        try:  # find the installed version for the message
            # importlib.metadata.version looks up an installed package's version number
            from importlib.metadata import version
            have = version("mcp")  # version of the installed mcp distribution
        # No metadata for the package (an unusual install).
        except Exception:  # noqa: BLE001
            have = "unknown"  # report it as unknown
        # name the minimum and how to upgrade
        return f"needs the MCP Python SDK >= {MIN_SDK} (found {have}); pip install -U \"mcp>={MIN_SDK}\""
    return None  # everything needed is there


# SIGTERM handler (Linux and macOS): kill every job at once, then die by the same signal. signum: the signal
# number; frame: the interrupted stack frame (unused). Exit handlers do not run when a signal kills a process,
# which is why jobs are stopped here.
def _on_term(signum, frame):
    JOBS.kill_all()  # immediate kill, no grace period
    signal.signal(signum, signal.SIG_DFL)  # restore the default action for this signal
    os.kill(os.getpid(), signum)  # send it to ourselves again, so the process ends with the usual signal exit status


# Run the MCP server on stdio until the client disconnects. argv: unused (kept for a uniform entry point).
# Returns the exit code: 0 after a normal end, 2 when the SDK is missing or too old. Side effects: prunes old
# job logs, installs a SIGTERM handler and an exit hook, and stops every job on the way out.
def main(argv=None) -> int:
    problem = _sdk_problem()  # check the SDK first
    if problem:  # the SDK is missing or too old to run the server
        print(f"speechkit-narrate mcp: {problem}", file=sys.stderr)  # explain on stderr; stdout is the protocol channel
        return 2  # exit code 2, as for a usage error, so scripts notice the failure
    prune_logs()  # delete job logs older than LOG_DAYS
    if not WIN:  # signals exist only on Linux and macOS in this form
        try:  # installing a handler can fail
            signal.signal(signal.SIGTERM, _on_term)  # kill jobs when the client or system terminates the server
        # signal.signal() only works on the main thread, e.g. not when a test embeds the server in a thread.
        except ValueError:                             # not the main thread
            pass  # carry on without a SIGTERM handler; atexit still stops the jobs
    atexit.register(JOBS.shutdown)  # on a normal interpreter exit, stop jobs too
    server = build_server()  # the low-level server with the speechkit-narrate handlers

    # The coroutine that serves one connection: stdio_server() turns stdin/stdout into message streams (while
    # serving, the SDK points file descriptor 1 at stderr so stray prints cannot reach the protocol).
    async def serve():
        async with stdio_server() as (read, write):  # read: client messages; write: server messages
            # handle requests until the client closes stdin; the options carry name, version and capabilities
            await server.run(read, write, server.create_initialization_options())

    # one start-up line on stderr: version, toolkit folder, job log folder
    _log(f"speechkit-narrate MCP server {server.version} on stdio (toolkit {ROOT}, jobs log to {jobs_dir()})")
    try:  # Ctrl+C should end quietly
        anyio.run(serve)  # start the event loop and serve
    except KeyboardInterrupt:  # interrupted from the keyboard
        pass  # a normal way to stop the server
    finally:  # runs after a normal end, an interrupt or an error
        JOBS.shutdown()  # stop running jobs (SIGTERM, then SIGKILL)
    return 0  # exit code 0: the server stopped normally


# `python -m speechkit_narrate.mcp_server` starts the server too. It cannot run as a plain script
# (python mcp_server.py): the module-level relative imports at the top fail first, before main() is reached.
if __name__ == "__main__":
    sys.exit(main())  # the return value becomes the process exit code
