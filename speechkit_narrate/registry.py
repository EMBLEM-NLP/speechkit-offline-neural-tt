# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Stage registry: what each toolkit needs and makes, plug-ins, change tracking and the run planner.

A stage declares:
    name        unique name ("narrate")
    needs       stages that must have run first
    inputs      what it reads: PATHS keys ("book"), project-relative paths or globs ("out/narration/*.wav"),
                "@input" (or "@<setting>") for the files named in project.json
    outputs     what it writes (same forms; a trailing "?" marks an output that may legitimately be absent)
    settings    project.json keys it depends on, dotted ("export.format")
    platforms   where it can run: "any", or a subset of "linux", "windows", "macos"
    options     its own parameters, as JSON-schema properties (used by the CLI, MCP and PowerShell layers)
    run         run(project, ctx) -> dict | None; returning {"stop": "why"} halts the pipeline after it, and
                {"partial": True} (e.g. a test clip) leaves the stage marked as still to do

The planner fingerprints each stage's implementation, settings and inputs. A stage runs when it never ran,
an output is missing, its fingerprint changed, a stage it needs runs, or it is forced; otherwise it is
skipped. Outputs that already exist from before the registry (no record yet) are adopted, not rebuilt, unless the
stage's last run failed or was interrupted (killed, or died before it could record a result).
State: work/.speechkit_narrate/state.json.

Plug-ins add or replace stages and standalone tools without editing the toolkit. A plug-in is a module or a
.py file with functions decorated by @stage(...) / @tool(...), or a register(registry) function. Loaded from:
    - entry points, group "speechkit_narrate.plugins"
    - SPEECHKIT_PLUGINS: module names or .py files, separated by os.pathsep
    - project.json "plugins": [...] (module names, or paths relative to the project)
    - every .py file in the project's plugins/ folder
A plug-in stage named like an existing one must say replaces=True. Undeclared fields default to the
original's, and the implementation is part of the fingerprint, so swapping one in re-runs it and what follows.

Stages that cannot run on this platform (the voice runtime is Linux-only) are delegated to SPEECHKIT_DELEGATE,
a command prefix that runs the toolkit there, e.g. "wsl -d Ubuntu-24.04 --exec" on Windows (a bare "wsl" gets
--exec added, so arguments never pass through the Linux shell).
"""
# Postpone evaluation of type annotations: hints such as `Callable | None` and `list[Stage]` stay plain strings,
# so they cost nothing at import time and may name classes that are defined further down.
from __future__ import annotations

import fnmatch  # shell-style wildcard matching, used by glob_match() at the end of the file
import glob  # expands glob specs such as "out/narration/*.wav" into the files that exist
import hashlib  # SHA-256 fingerprints of files and settings, SHA-1 for plug-in module names
import importlib  # imports a plug-in given as a dotted module name
import importlib.util  # loaded alongside importlib although nothing below calls importlib.util directly
import inspect  # inspect.getsource() reads a plug-in function's code so edits change its identity
import json  # state.json, settings hashing and the JSON exchanged with delegated runs
import os  # environment variables (SPEECHKIT_*), os.walk over folders, os.pathsep
import shlex  # splits the SPEECHKIT_DELEGATE command line the way a shell would
import subprocess  # runs wslpath and the delegated toolkit in WSL (or another delegate)
import sys  # sys.platform for platform detection and sys.modules for plug-in modules
import time  # timestamps for state records and stage durations in seconds
# dataclass builds Stage/Tool; replace() copies a Stage with some fields changed
from dataclasses import dataclass, field, replace
from pathlib import Path  # object-oriented file paths, used for every project file
from typing import Any, Callable
from .locking import locked_project, lock_token, DELEGATE_TOKEN

# Where the planner remembers what ran: per-stage fingerprints, finish times and last errors.
# It lives under work/ so deleting the work folder also forgets the history.
STATE_FILE = "work/.speechkit_narrate/state.json"
# 4 << 20 is 4 x 1,048,576 bytes = 4 MiB. Hashing bigger files (WAV narration) on every plan would
# be slow, so for those the size and modification time stand in for the content.
HASH_LIMIT = 4 << 20                  # files up to 4 MiB are fingerprinted by content, larger by size + time


# ------------------------------------------------------------------------------------------- declarations
# Stage is the declaration of one pipeline step: its name, the function that does the work, and everything the
# planner needs to decide whether that step is stale (what it needs, reads, writes and which settings it depends on).
# Built-in stages are declared in stages.py with @stage(...); plug-ins declare theirs the same way.
# The @dataclass decorator generates __init__, __repr__ and __eq__ from the annotated fields below.
@dataclass
class Stage:  # one stage of the narration pipeline (extract, narrate, export ...)
    name: str  # unique stage name, also the CLI sub-command and the MCP tool suffix
    run: Callable | None = None  # the function run(project, ctx) that does the work; None only in tests
    needs: tuple = ()  # names of stages that must be up to date before this one
    inputs: tuple = ()  # what the stage reads: PATHS keys, project-relative paths/globs, or @input
    outputs: tuple = ()  # what the stage writes; a trailing "?" marks an output allowed to be missing
    settings: tuple = ()  # dotted project.json keys whose values feed the fingerprint ("export.format")
    platforms: tuple = ("any",)  # "any", or a subset of "linux", "windows", "macos" where the stage can run
    # JSON-schema properties for the stage's own options; field() gives each Stage its own dict
    options: dict = field(default_factory=dict)
    description: str = ""  # one-line human description shown by `stages` and in MCP tool descriptions
    version: str = "1"  # bump this to make every project re-run the stage after a behaviour change
    # default=False keeps a stage (like `audit`) out of the pipeline unless asked for by name.
    default: bool = True                 # part of a plain `run` / `all`
    # heavy=True marks narration and the pronunciation loop: the MCP layer always treats it as a long job.
    heavy: bool = False                  # long-running: MCP runs it as a background job
    refreshes: tuple = ()                # stages whose outputs this one rewrites consistently (re-recorded after it)
    satisfied: Callable | None = None    # satisfied(project) -> str | None: a reason it needs no run at all
    source: str = "speechkit-narrate"
    dependencies: tuple = ()
    operational_options: tuple = ()
    fingerprint_extra: Callable | None = None
    validate: Callable | None = None

    # Identity string of the code behind this stage, stored in the fingerprint. Built-in stages are identified by
    # function name plus `version`, so upgrading the toolkit does not re-voice a finished book. Plug-in stages
    # also carry a 12-hex-digit hash of their source, so editing a plug-in re-runs it and what depends on it.
    # Read-only property (no parameters); returns a string such as "speechkit-narrate:narrate@1".
    @property  # a property: computed each time it is read, never stored on the Stage
    def impl(self) -> str:  # e.g. "plugins/stats.py:narrate_plugin_x.stats@1#a1b2c3d4e5f6" for a plug-in
        """Versioned implementation identity, including explicitly declared helper sources."""
        fn = self.run  # the implementation function (may be None for a bare declaration)
        mod = getattr(fn, "__module__", "?")  # defining module name, "?" when the object has none
        qual = getattr(fn, "__qualname__", repr(fn))  # qualified function name; repr() is a fallback for odd callables
        if self.source == "speechkit-narrate":
            base = Path(__file__).parent
            try:
                body = inspect.getsource(fn).encode()
            except (OSError, TypeError):
                body = b"no source"
            h = hashlib.sha256(body)
            for name in self.dependencies:
                path = base / name
                files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
                for file in files:
                    h.update(f"{name}/{file.relative_to(path) if path.is_dir() else path.name}".encode())
                    h.update(file.read_bytes() if file.is_file() else b"missing")
            return f"speechkit-narrate:{qual}@{self.version}:cache2:{h.hexdigest()[:20]}"
        try:  # plug-ins: hash the actual source text
            # first 12 hex digits (48 bits) of SHA-256 are plenty to detect an edit
            source_file = inspect.getsourcefile(fn)
            body = Path(source_file).read_bytes() if source_file else inspect.getsource(fn).encode()
            h = hashlib.sha256(body)
            base = Path(source_file).parent if source_file else Path.cwd()
            for name in self.dependencies:
                path = base / name
                files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
                for file in files:
                    h.update(f"{name}/{file.relative_to(path) if path.is_dir() else path.name}".encode())
                    h.update(file.read_bytes() if file.is_file() else b"missing")
            code = h.hexdigest()[:20]
        except (OSError, TypeError):  # no source available (a C function, code without a source file, or run=None)
            code = "nosrc"  # constant marker: such a plug-in changes identity only via its version
        # source, module, name, version and code hash together identify the plug-in implementation
        return f"{self.source}:{mod}.{qual}@{self.version}#{code}"

    # The stage as a plain JSON-able dict, used by Registry.describe() for `speechkit-narrate stages`, the MCP catalog and the
    # PowerShell module. Tuples become lists for JSON; runs_here tells the caller whether this machine can run it
    # directly or must delegate it (e.g. to WSL). No side effects.
    def info(self) -> dict:
        # name and the declared dependency, input and output lists
        return {"name": self.name, "needs": list(self.needs), "inputs": list(self.inputs), "outputs": list(self.outputs),
                # settings it watches, where it runs, and its option schema
                "settings": list(self.settings), "platforms": list(self.platforms), "options": self.options,
                # text plus flags: part of a default run, long-running, origin
                "description": self.description, "default": self.default, "heavy": self.heavy, "source": self.source,
                "runs_here": platform_ok(self.platforms)}  # evaluated now, so SPEECHKIT_PLATFORM overrides show up


# Tool is the declaration of a standalone tool: a function that works on plain files (no project folder), with
# a JSON-schema description of its parameters. Built-in tools live in tools.py, plug-ins add more with @tool(...).
# The CLI turns `params` into --options, the MCP server into a tool_<name> input schema.
@dataclass
class Tool:  # a standalone tool (speak, quick)
    """A standalone tool: works on plain files, no project folder."""
    name: str  # tool name, used as `speechkit-narrate tool <name>` and MCP tool_<name>
    run: Callable  # function called with the parameters as keyword arguments plus log=
    params: dict = field(default_factory=dict)     # JSON-schema properties
    required: tuple = ()  # parameter names the caller must always give
    description: str = ""  # one-line description for listings and generated help
    platforms: tuple = ("any",)  # where the tool can run; "any" means everywhere
    heavy: bool = False  # long-running (the speak tool, quick): MCP callers should expect to poll a job
    source: str = "speechkit-narrate"  # "speechkit-narrate" for built-ins, otherwise the plug-in that declared it

    # The full JSON schema for the tool's arguments, returned as a dict. Unknown arguments are rejected
    # (additionalProperties false), so a typo in a parameter name fails loudly instead of being ignored.
    def schema(self) -> dict:
        # an object whose properties are the declared params; required as a list for JSON
        return {"type": "object", "properties": self.params, "required": list(self.required),
                "additionalProperties": False}  # no extra keys allowed beyond the declared parameters

    # The tool as a JSON-able dict for listings: name, description, full parameter schema, platforms, the heavy
    # flag, origin and whether it can run on this machine. Used by `speechkit-narrate tools` and the MCP catalog.
    def info(self) -> dict:
        # identity, text, and the complete argument schema
        return {"name": self.name, "description": self.description, "params": self.schema(),
                # platform list, long-running flag, and where it was declared
                "platforms": list(self.platforms), "heavy": self.heavy, "source": self.source,
                "runs_here": platform_ok(self.platforms)}  # true when this machine's platform is listed (or "any")


# Decorator for plug-in (and built-in) stages, e.g. @stage("stats", needs=["narrate"], outputs=["out/stats.json"]).
# name: the stage name; **kw: any Stage field (needs, inputs, outputs, settings, options ...) plus replaces=True
# to swap out an existing stage. It does not register anything itself: it only tags the function with a
# __narrate_stage__ dict that Registry.scan_module() later turns into a Stage. Returns the decorator.
def stage(name: str, **kw):
    """Decorator for plug-in stages: @stage("stats", needs=["narrate"], outputs=["out/stats.json"])."""
    # kept apart from the Stage fields because it controls registration, not the stage
    replaces = kw.pop("replaces", False)

    # The actual decorator: receives the stage function and returns it unchanged apart from the tag.
    def deco(fn):
        # metadata read later by scan_module(); the function still works as a normal function
        fn.__narrate_stage__ = {"name": name, "replaces": replaces, **kw}
        return fn  # returning fn keeps the module-level name bound to the real function
    return deco  # stage(...) returns this decorator, which Python then applies to the def below


# Decorator for standalone tools, e.g. @tool("wordcount", params={"text": {"type": "string"}}, required=["text"]).
# name: the tool name; **kw: Tool fields (params, required, description, platforms, heavy) and optionally
# replaces=True. Like @stage it only attaches a __narrate_tool__ dict for scan_module() to find.
def tool(name: str, **kw):
    """Decorator for plug-in tools: @tool("wordcount", params={"text": {"type": "string"}}, required=["text"])."""
    # Tag the function and hand it back unchanged.
    def deco(fn):
        # name plus every declared field; replaces stays inside and is popped at scan time
        fn.__narrate_tool__ = {"name": name, **kw}
        return fn  # the module keeps the plain function, still callable directly
    return deco  # the decorator itself, applied by Python to the def that follows


# ------------------------------------------------------------------------------------------- platform
# The platform this process counts as: "windows", "macos" or "linux". SPEECHKIT_PLATFORM overrides detection,
# which the tests use to simulate Windows and which delegated runs set to empty so the remote side detects itself.
def current_platform() -> str:
    forced = os.environ.get("SPEECHKIT_PLATFORM")  # an explicit override, e.g. "windows" in tests
    if forced:  # an empty value counts as unset, so detection applies
        return forced  # trust the override as given
    # sys.platform is "win32" on Windows and "darwin" on macOS; Linux, WSL and anything else count as linux
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


# True when something declared for `platforms` (a tuple or list of names) can run on this machine: either it
# is declared for "any" platform or the current platform is in the list.
def platform_ok(platforms) -> bool:
    # "any" wins at once; otherwise compare with the detected platform
    return "any" in platforms or current_platform() in platforms


# ------------------------------------------------------------------------------------------- registry
# The error type for registry problems: duplicate or unknown stages, cycles, broken plug-ins, a plan that cannot
# run here. The CLI reports it as a usage-style failure (exit code 2, or {"ok": false} under --json).
class RegistryError(Exception):
    pass  # no extra behaviour; the class exists so callers can catch it by type


# The registry holds every stage and tool known in this process, the plug-ins loaded and which built-in stages
# they replaced. It orders stages by their needs (topo), expands targets to what they need (closure) and
# describes itself as JSON. One is built per command by load() below.
class Registry:
    # Start empty; load() fills it with the built-ins first and then the plug-ins.
    def __init__(self):
        # stage name -> Stage, in registration order (definition order of stages.py first)
        self.stages: dict[str, Stage] = {}
        self.tools: dict[str, Tool] = {}  # tool name -> Tool
        self.plugins: list[dict] = []  # one record per plug-in loaded: {"plugin": source, "items": count}
        self.replaced: dict[str, str] = {}  # stage name -> source of the stage it replaced (shown by describe)

    # ---- adding
    # Register a stage. st: the Stage; replaces: True when a plug-in deliberately swaps out an existing stage.
    # Without replaces a duplicate name is an error; with it, every field the replacement left at its default value
    # is copied from the original, so a plug-in can swap only the implementation. Returns the stage actually stored.
    def add_stage(self, st: Stage, replaces: bool = False) -> Stage:
        old = self.stages.get(st.name)  # the stage already registered under this name, if any
        if old and not replaces:  # a silent clash would make the pipeline depend on load order
            # name the existing stage's source so the user can find the clash
            raise RegistryError(f"stage {st.name!r} already exists ({old.source}); a plug-in that swaps it in must "
                                "say replaces=True")  # explains the fix in the same message
        if old and replaces:  # an intended replacement of an existing stage
            dflt = Stage(st.name)  # a Stage with only the name set: holds the default value of every field
            # The fields a replacement may inherit. name, run, version and source are never inherited: the replacement
            # brings its own code. A field explicitly set to its default value looks undeclared and is inherited too.
            for f in ("needs", "inputs", "outputs", "settings", "platforms", "options", "description", "default",
                      # also refreshes and satisfied, the last of the inheritable fields
                      "heavy", "refreshes", "satisfied"):
                if getattr(st, f) == getattr(dflt, f):  # still at its default, so the plug-in did not declare it
                    # dataclasses.replace() makes a copy with the original's value for that field
                    st = replace(st, **{f: getattr(old, f)})
            # remember which source was replaced, for describe() and `speechkit-narrate stages`
            self.replaced[st.name] = old.source
        if not old and replaces:  # replaces=True with nothing to replace is almost certainly a typo in the name
            # fail instead of silently adding a new stage
            raise RegistryError(f"stage {st.name!r} says replaces=True but there is no such stage")
        self.stages[st.name] = st  # store (or overwrite) the stage under its name
        return st  # the caller gets the merged version

    # Register a standalone tool. t: the Tool; replaces: True to overwrite a tool of the same name.
    # Unlike add_stage nothing is inherited and a replacement of a missing tool is simply an addition. Returns t.
    def add_tool(self, t: Tool, replaces: bool = False) -> Tool:
        # a second tool with the same name is refused unless it says replaces=True
        if t.name in self.tools and not replaces:
            # say who owns the existing tool so the clash is easy to trace
            raise RegistryError(f"tool {t.name!r} already exists ({self.tools[t.name].source})")
        self.tools[t.name] = t  # store it by name; with replaces=True this quietly overwrites the earlier tool
        return t  # returned for convenience, e.g. in a plug-in's register()

    # Find everything a module declares and register it: functions tagged by @stage / @tool, then the module's own
    # register(registry) function if it has one. mod: an imported module object; source: the label recorded on each
    # item ("speechkit-narrate" or the plug-in's path/name). Returns how many stages and tools were added.
    def scan_module(self, mod, source: str) -> int:
        n = 0  # running count of items registered from this module
        # Every callable module attribute carrying either decorator's tag. vars(mod) also contains names the module
        # imported, so a plug-in that imports an already decorated built-in function is refused: the second
        # registration of that name raises RegistryError as a duplicate (unless the plug-in marks it replaces=True).
        items = [o for o in vars(mod).values() if callable(o) and (hasattr(o, "__narrate_stage__")
                                                                  # ... or a @tool tag
                                                                  or hasattr(o, "__narrate_tool__"))]
        # order by line number in the file, so stages register in definition order (that order breaks ties in topo)
        items.sort(key=lambda o: getattr(getattr(o, "__code__", None), "co_firstlineno", 0))
        for obj in items:  # each tagged function, top of the file first
            meta = getattr(obj, "__narrate_stage__", None)  # the @stage metadata, or None when only @tool applies
            if meta and callable(obj):  # callable() was already checked by the filter above
                meta = dict(meta)  # a copy, so popping keys never alters the function's own tag
                rep = meta.pop("replaces", False)  # registration flag, not a Stage field
                # Lists given in the decorator become tuples, matching the Stage field types and making them immutable.
                # only the list-like fields are converted; None values are left out
                tup = {k: tuple(v) for k, v in meta.items() if k in ("needs", "inputs", "outputs", "settings",
                                                                      # the remaining list-valued Stage fields
                                                                      "platforms", "refreshes", "dependencies", "operational_options") and v is not None}
                meta.update(tup)  # merged back over the original values
                # build and register the Stage; source records where it came from
                self.add_stage(Stage(run=obj, source=source, **meta), replaces=rep)
                n += 1  # one more item registered
            meta = getattr(obj, "__narrate_tool__", None)  # a function may carry both tags; check the tool one too
            if meta and callable(obj):  # True when @tool was applied
                meta = dict(meta)  # copy again, so the original tag dict stays intact
                rep = meta.pop("replaces", False)  # registration flag for tools
                for k in ("required", "platforms"):  # the two tuple-typed Tool fields
                    if k in meta:  # only those actually declared
                        # lists from the @tool decorator become tuples to match the Tool fields
                        meta[k] = tuple(meta[k])
                self.add_tool(Tool(run=obj, source=source, **meta), replaces=rep)  # build and register the Tool
                n += 1  # count the tool among the items this module contributed
        # optional register(registry) function for plug-ins that build stages in code
        reg = getattr(mod, "register", None)
        if callable(reg):  # only a real callable counts
            before = len(self.stages) + len(self.tools)  # how many items existed before, to count what register() adds
            reg(self)  # the plug-in gets this registry and may call add_stage/add_tool directly
            n += len(self.stages) + len(self.tools) - before  # net increase; a replacement does not add to the count
        return n  # reported in the plug-in list by describe()

    # Load one plug-in and register what it declares. spec: a dotted module name ("mypkg.stages") or a path to a .py
    # file; base: the folder a relative path is resolved against (cwd for SPEECHKIT_PLUGINS, the project folder for
    # project.json "plugins"). Executes the plug-in's code in this process. Raises RegistryError on any failure.
    def load_plugin(self, spec: str, base: Path | None = None) -> None:
        """spec: a module name, or a path to a .py file (relative to base)."""
        p = Path(spec)  # treat the spec as a path first; harmless for module names
        if not p.is_absolute() and base is not None:  # relative path and a base folder to anchor it to
            p = base / p  # resolve against the given base instead of the current folder
        try:  # one error path for every failure below
            # a path to a .py file (both tests normally agree); anything else is a module name
            if spec.endswith(".py") or p.suffix == ".py":
                if not p.exists():  # a missing file is reported plainly, not as an import error
                    # include the resolved path, which shows which base was used
                    raise RegistryError(f"plug-in file {p} not found")
                # a unique, stable module name derived from the file's absolute path (10 hex digits of SHA-1)
                name = "narrate_plugin_" + hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:10]
                # compiled from the source every time (no bytecode cache), so an edit is always picked up
                import types  # local import: types.ModuleType creates an empty module object
                mod = types.ModuleType(name)  # a fresh module, so reloading the same file never reuses old definitions
                mod.__file__ = str(p)  # lets inspect.getsource() and tracebacks find the file
                # registered before exec: dataclasses and pickle look a module up in sys.modules by name
                sys.modules[name] = mod
                # compile the file's bytes and run them as the module body; definitions land in mod.__dict__
                exec(compile(p.read_bytes(), str(p), "exec"), mod.__dict__)
                src = str(p)  # the plug-in's label: its path
            else:  # otherwise a module importable from sys.path
                # a normal import (cached in sys.modules for the rest of the process)
                mod = importlib.import_module(spec)
                src = spec  # the label is the dotted name
            n = self.scan_module(mod, src)  # register its stages and tools
            self.plugins.append({"plugin": src, "items": n})  # remember it for describe() and `speechkit-narrate stages`
        except RegistryError:  # errors from add_stage/add_tool already say what is wrong
            raise  # pass them on unchanged
        # Anything else (syntax error, failed import, an exception in the plug-in body) is wrapped so callers only
        # need to catch RegistryError; `from e` keeps the original traceback attached.
        except Exception as e:  # noqa: BLE001
            # name the plug-in and the original exception type
            raise RegistryError(f"plug-in {spec}: {type(e).__name__}: {e}") from e

    # ---- order
    # All stages in a run order where every stage comes after the stages it needs (a depth-first topological sort).
    # Stages that do not depend on each other keep registration order. Raises RegistryError for a dependency cycle
    # or a need that names no registered stage. Returns a list of Stage objects.
    def topo(self) -> list[Stage]:
        order, seen, temp = [], set(), set()  # result list, finished names, and the names on the current DFS path

        # Visit stage n after its needs. chain is the list of stages that led here, used in error messages.
        def visit(n, chain):
            if n in seen:  # already placed in the order
                return  # nothing more to do
            if n in temp:  # n is on the current path: following needs came back to it
                # show the loop, e.g. "stage cycle: a -> b -> a"
                raise RegistryError("stage cycle: " + " -> ".join(chain + [n]))
            if n not in self.stages:  # a need that is not registered (typo, or a plug-in not loaded)
                # name the stage that declared the bad need
                raise RegistryError(f"stage {chain[-1] if chain else '?'} needs unknown stage {n!r}")
            temp.add(n)  # mark n as in progress
            for d in self.stages[n].needs:  # visit everything n needs first
                visit(d, chain + [n])  # extend the chain for error messages
            temp.discard(n)  # no longer on the path
            seen.add(n)  # fully visited, so later visits of this stage return at once
            order.append(self.stages[n])  # n lands after all its needs
        for n in self.stages:  # start from every stage, in registration order
            visit(n, [])  # a fresh, empty chain for each root
        return order  # every stage appears exactly once

    # The set of stage names needed to bring `names` up to date: the names themselves plus everything they need,
    # directly or indirectly. names: any iterable of stage names. Raises RegistryError for an unknown name.
    def closure(self, names) -> set:
        out, todo = set(), list(names)  # result set and a work list (stack) of names still to visit
        while todo:  # until every reachable stage was visited
            n = todo.pop()  # take the next name
            if n in out:  # this stage was already reached through another path
                continue  # its needs are queued already, so visiting it again adds nothing
            if n not in self.stages:  # unknown names are user typos; list the valid ones
                # the message shows every registered stage
                raise RegistryError(f"unknown stage {n!r}; stages: {', '.join(self.stages)}")
            out.add(n)  # a valid stage: put it in the result set
            todo += list(self.stages[n].needs)  # and queue what it needs
        return out  # unordered; plan() applies the run order

    # The whole registry as one JSON-able dict: stages in run order, tools, plug-ins loaded, stages replaced, this
    # platform and the delegate command (None when unset). This is what `speechkit-narrate --json stages` prints and what the
    # MCP server turns into tools.
    def describe(self) -> dict:
        # stages sorted by topo(), tools in registration order
        return {"stages": [s.info() for s in self.topo()], "tools": [t.info() for t in self.tools.values()],
                # plug-in list, replacements and the detected platform
                "plugins": self.plugins, "replaced": self.replaced, "platform": current_platform(),
                # an empty SPEECHKIT_DELEGATE is reported as None
                "delegate": os.environ.get("SPEECHKIT_DELEGATE") or None}


# Build a registry for one command: built-in stages and tools first, then plug-ins from entry points,
# SPEECHKIT_PLUGINS, extra_plugins (the CLI's --plugin), the project's project.json "plugins" list and its plugins/
# folder. project: a Project or None; extra_plugins: module names or file paths. Returns the checked Registry.
def load(project=None, extra_plugins=()) -> Registry:
    """Built-in stages and tools, then plug-ins (entry points, SPEECHKIT_PLUGINS, project's own)."""
    # imported here, not at the top: stages.py imports this module (the @stage decorator)
    from . import stages as builtin_stages
    from . import tools as builtin_tools  # same reason: tools.py imports @tool from here
    reg = Registry()  # a fresh registry with no stages, tools or plug-ins yet
    reg.scan_module(builtin_stages, "speechkit-narrate")  # built-in stages, labelled "speechkit-narrate"
    reg.scan_module(builtin_tools, "speechkit-narrate")  # built-in tools, labelled "speechkit-narrate"
    try:  # installed packages may advertise plug-ins through entry points
        from importlib.metadata import entry_points  # standard-library lookup of installed package metadata
        for ep in entry_points(group="speechkit_narrate.plugins"):  # each entry point in the "speechkit_narrate.plugins" group
            # "module:attr" form; only the module part is used, its tags are scanned
            reg.load_plugin(ep.value.split(":")[0])
    # Problems in the entry-point machinery itself are ignored (an installed package with broken metadata must not
    # stop the toolkit), but a plug-in that loads and is wrong still fails loudly.
    except Exception as e:  # noqa: BLE001
        if isinstance(e, RegistryError):  # a real plug-in error
            raise  # re-raised; anything else is dropped
    # SPEECHKIT_PLUGINS lists modules or .py files separated by os.pathsep (";" on Windows, ":" elsewhere)
    for spec in filter(None, os.environ.get("SPEECHKIT_PLUGINS", "").split(os.pathsep)):
        reg.load_plugin(spec.strip(), Path.cwd())  # relative paths are resolved against the current folder
    for spec in extra_plugins:  # plug-ins given with --plugin on the command line
        reg.load_plugin(str(spec), Path.cwd())  # also resolved against the current folder
    if project is not None:  # project-level plug-ins only when a project is known
        # project.json "plugins": a list of module names or project-relative paths
        for spec in project.cfg.get("plugins") or []:
            reg.load_plugin(spec, project.root)  # resolved against the project folder, not the current folder
        pdir = project.root / "plugins"  # the project's own plugins/ folder
        if pdir.is_dir():  # a project need not have a plugins folder; scan it only when present
            for f in sorted(pdir.glob("*.py")):  # every .py file there, sorted so the load order is stable
                # files starting with "_" (e.g. __init__.py, _helpers.py) are not plug-ins
                if not f.name.startswith("_"):
                    reg.load_plugin(str(f))  # already absolute because project.root is resolved
    # Run the ordering once now so an unknown need or a cycle is reported when the registry loads.
    reg.topo()                                    # fail early on unknown needs or cycles
    return reg  # ready for plan() / run() / describe()


# ------------------------------------------------------------------------------------------- fingerprints
# The files a spec stands for in this project. spec: an input/output entry of a Stage (PATHS key, @input or @<setting>,
# a project-relative path, or a glob). Returns a list of Paths: globs and @-specs list only existing
# files, plain paths are returned whether or not they exist (callers test existence themselves).
def resolve(project, spec: str) -> list[Path]:
    """Paths a spec stands for (existing ones only for globs)."""
    spec = spec.rstrip("?")  # drop the "optional output" marker before resolving
    if spec.startswith("@"):  # @-specs name files chosen in project.json
        key = spec[1:]  # "input", or another file setting a plug-in stage names
        if key == "input":  # @input means the source text, found through project.input_file()
            try:  # input_file() raises when the setting is empty or the file is missing
                return [project.input_file()]  # the configured input file
            # No input configured (or it is missing): an empty list, which the fingerprint records as "none".
            except Exception:  # noqa: BLE001
                return []  # an empty list makes the fingerprint record this input as none
        try:  # another file setting (a plug-in's): the configured file, or none when unset
            f = project.optional(key)
        except Exception:  # noqa: BLE001  # a configured file that is missing: recorded as none, like @input
            f = None
        return [f] if f else []  # one path or none
    from .project import PATHS  # local import: project.py does not depend on the registry
    if spec in PATHS:  # a PATHS key such as "book" or "speech"
        return [project.p(spec)]  # the project's standard location for it
    if any(c in spec for c in "*?["):  # any glob character marks a pattern
        # sorted for a stable fingerprint; recursive=True lets "**" match nested folders
        return [Path(p) for p in sorted(glob.glob(str(project.root / spec), recursive=True))]
    return [project.root / spec]  # a plain project-relative path


# A short signature of a file or folder that changes when its content changes. p: the path. Returns "missing",
# "dir:<hash>" (names, sizes and modification times of every file inside), "sha:<hash>" (content of files up to
# HASH_LIMIT) or "big:<size>:<mtime_ns>" for larger files. Reads files; writes nothing.
def _sig(p: Path) -> str:
    if not p.exists():  # a path that does not exist has a fixed signature
        return "missing"  # so it can still be compared with the recorded one
    if p.is_dir():  # folders, e.g. work/speech with one WAV per segment
        h = hashlib.sha256()  # one running hash over the whole tree
        for root, dirs, files in os.walk(p):  # visit the folder and every sub-folder below it, top down
            dirs.sort()  # sort in place: os.walk then visits sub-folders in a stable order
            # Skip the toolkit's own bookkeeping folder; checking the absolute path's parts means a project stored under
            # a folder named ".speechkit_narrate" would have every folder skipped.
            if ".speechkit_narrate" in Path(root).parts:
                continue  # do not hash this folder's files
            for f in sorted(files):  # stable file order within each folder
                fp = Path(root) / f  # full path of the file
                st = fp.stat()  # size and modification time, without reading the content
                # relative path, size in bytes and mtime in nanoseconds, one line per file
                h.update(f"{fp.relative_to(p).as_posix()}|{st.st_size}|{st.st_mtime_ns}\n".encode())
        return "dir:" + h.hexdigest()[:16]  # 16 hex digits (64 bits) are enough to notice a change
    st = p.stat()  # a plain file: read its size and modification time
    if st.st_size <= HASH_LIMIT:  # small enough to hash the bytes
        # content hash: touching the file without changing it keeps the signature
        return "sha:" + hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    return f"big:{st.st_size}:{st.st_mtime_ns}"  # large file: size plus modification time in nanoseconds


# Look up a dotted setting such as "export.format" in the effective config. cfg: the merged project.json dict;
# dotted: the key path. Returns the value, or None when any step is missing or not a dict.
def _setting(cfg: dict, dotted: str):
    cur = cfg  # start at the top level
    for k in dotted.split("."):  # one level per dot-separated part
        if not isinstance(cur, dict) or k not in cur:  # a missing key or a non-dict in the way
            return None  # counts as "not set"
        cur = cur[k]  # step into the nested dict for the next part of the dotted name
    return cur  # the value (any JSON type)


# The fingerprint of a stage in this project: its implementation identity, a hash of each watched setting and
# a signature of each input. Returns a dict with "impl", "settings", "inputs" and "all" (a hash of the rest).
# The planner compares "all" with the recorded one; _diff() explains which part changed. Reads input files.
def fingerprint(project, st: Stage, options=None) -> dict:
    parts = {"impl": st.impl,  # implementation identity string from Stage.impl
             # Each watched setting becomes 12 hex digits of SHA-256 over its JSON form; sort_keys makes dicts stable
             # and default=str copes with values JSON cannot encode.
             "settings": {k: hashlib.sha256(json.dumps(_setting(project.cfg, k), sort_keys=True, default=str)
                                            # one entry per dotted setting name
                                            .encode()).hexdigest()[:12] for k in st.settings},
             "inputs": {}}  # filled per input spec below
    supplied = options or {}
    scoped = supplied.get(st.name, {})
    parts["options"] = {k: scoped.get(k, supplied.get(k, schema.get("default")))
                        for k, schema in st.options.items() if k not in st.operational_options}
    if st.fingerprint_extra:
        parts["extra"] = st.fingerprint_extra(project)
    for spec in st.inputs:
        paths = resolve(project, spec)  # the files it stands for right now
        # globs and @-specs may stand for several files (or none)
        if any(c in spec for c in "*?[") or spec.startswith("@"):
            # combine "name:signature" of every file into one hash
            parts["inputs"][spec] = hashlib.sha256("|".join(f"{q.name}:{_sig(q)}" for q in paths)
                                                   # "none" when the pattern matches nothing
                                                   .encode()).hexdigest()[:16] if paths else "none"
        else:  # a single fixed path
            # its own signature, or "none" if it could not be resolved
            parts["inputs"][spec] = _sig(paths[0]) if paths else "none"
    # 20 hex digits summarising everything above; sort_keys keeps it stable
    parts["all"] = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:20]
    return parts  # the full breakdown is stored in state.json


# The outputs of a stage that are not there yet. Outputs ending with "?" are optional and never count. A folder
# counts as present only when it holds something. Returns the missing specs as written in the declaration.
def missing_outputs(project, st: Stage) -> list[str]:
    miss = []  # missing specs, in declaration order
    for spec in st.outputs:  # check every output path or pattern the stage declares
        if spec.endswith("?"):  # optional output (e.g. a report that is skipped on some books)
            continue  # optional outputs never make a stage look incomplete
        paths = resolve(project, spec)  # what the spec stands for now
        # at least one match, all exist, and folders are non-empty
        ok = bool(paths) and all(q.exists() and (not q.is_dir() or any(q.iterdir())) for q in paths)
        if not ok:  # otherwise the output is incomplete
            miss.append(spec)  # report the spec, not the resolved path
    if not miss and st.validate:
        miss.extend(st.validate(project))
    return miss


# Human-readable reasons why a stage's fingerprint changed. old: the recorded fingerprint; new: the current one.
# Returns phrases like "setting voice changed" or "input book changed"; used in plan() reasons.
def _diff(old: dict, new: dict) -> list[str]:
    out = []  # change descriptions, in the order implementation, settings, inputs
    if old.get("impl") != new["impl"]:  # the implementation identity differs (plug-in edited, version bumped)
        # listed first: the stage's code or plug-in version differs from the recorded one
        out.append("implementation changed")
    for k, v in new["settings"].items():  # compare each dotted setting's hash with the recorded one
        if old.get("settings", {}).get(k) != v:  # new setting or different value
            out.append(f"setting {k} changed")  # name the dotted setting
    for k, v in new["inputs"].items():  # compare each input's signature with the recorded one
        if old.get("inputs", {}).get(k) != v:  # new input or different signature
            out.append(f"input {k} changed")  # name the input spec
    return out or ["fingerprint changed"]  # fallback when only removed keys changed


# ------------------------------------------------------------------------------------------- state
# Absolute path of this project's state file (work/.speechkit_narrate/state.json). project: a Project. No side effects.
def state_path(project) -> Path:
    return project.root / STATE_FILE  # STATE_FILE is relative to the project folder


# Load the planner's state for a project: {stage: {"fingerprint", "finished", ...}, "_errors": {...}}.
# A missing or corrupt file gives an empty dict, which makes every stage look never run (or adoptable).
def read_state(project) -> dict:
    p = state_path(project)  # where the state lives
    if p.exists():  # a state file exists from an earlier run of the planner
        try:  # parse the JSON; decoding errors are caught just below
            return json.loads(p.read_text(encoding="utf-8"))  # the whole state as a dict
        except json.JSONDecodeError:  # a half-written or hand-edited file
            return {}  # treated as no history rather than crashing every command
    return {}  # the file does not exist, so nothing is recorded and every stage looks never run


# Save the planner's state. project: a Project; state: the dict from read_state(), updated. Writes a temporary
# file and renames it over the old one, so an interrupted write never leaves a truncated state.json.
def write_state(project, state: dict) -> None:
    p = state_path(project)  # work/.speechkit_narrate/state.json inside the project folder
    p.parent.mkdir(parents=True, exist_ok=True)  # create work/.speechkit_narrate if needed
    tmp = p.with_suffix(".tmp")  # state.tmp next to it
    # indent=1 keeps it readable; default=str turns odd values into text instead of failing
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(p)  # rename into place, replacing the old file in one step on Windows and Linux


# Absolute path of this project's event log (work/.speechkit_narrate/events.jsonl). project: a Project. No side effects.
def events_path(project) -> Path:
    return state_path(project).with_name("events.jsonl")  # beside state.json, in the same work/.speechkit_narrate folder


# Append one event to the project's event log, one JSON object per line, for workers and status pages that follow a
# run while it happens. event: run-start, start, done, failed, adopt, skip or run-end; **fields adds stage, seconds,
# reasons, error and the like. The time is UTC with a Z. Never raises: a log that cannot be written must not stop a run.
def log_event(project, event: str, **fields) -> None:
    line = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}  # one event
    try:  # writing is best effort, like a log file
        p = events_path(project)  # work/.speechkit_narrate/events.jsonl
        p.parent.mkdir(parents=True, exist_ok=True)  # create work/.speechkit_narrate on a fresh project
        with p.open("a", encoding="utf-8") as f:  # append, so earlier runs' events stay readable
            f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")  # one line per event, flushed on close
    except OSError:  # a read-only folder or a full disk
        pass  # the run itself carries on; state.json remains the record that matters


# Record that a stage is now up to date: stores its current fingerprint and finish time in `state` (in memory;
# the caller writes it). **extra adds fields such as seconds=, adopted=True or refreshed_by=.
def record(project, st: Stage, state: dict, options=None, **extra) -> None:
    # fingerprint taken now, after the run, so inputs the stage rewrote itself (check writes book.md) count as current
    state[st.name] = {"fingerprint": fingerprint(project, st, options), "finished": time.strftime("%Y-%m-%dT%H:%M:%S"), **extra}


# ------------------------------------------------------------------------------------------- planning
# Decide, without running anything, what each selected stage should do. Parameters: targets (stages to bring up
# to date, with their needs), force (run even if current; "all" forces every selected stage), only (exactly these
# stages, no needs), skip (leave out), from_stage (force it and every later selected stage), adopt (accept
# outputs that predate tracking). Returns one dict per selected stage in run order with action run, skip,
# adopt or blocked, its reasons, and where it runs ("here" or "delegate"). Reads state.json and input files.
def plan(project, reg: Registry, targets=None, force=(), only=None, skip=(), from_stage=None,
         adopt: bool = True, options=None) -> list[dict]:  # adopt=False is used by delegated runs, which must really run the stage
    """One step per stage in run order: {"stage", "action": run|skip|adopt|blocked, "reasons", "where"}."""
    order = reg.topo()  # every stage in dependency order
    names = [s.name for s in order]  # the same order as plain names
    # every stage name the caller mentioned anywhere; "all" has a meaning only in force (force every selected stage)
    for n in list(targets or []) + list(force) + list(only or []) + list(skip) + ([from_stage] if from_stage else []):
        if n != "all" and n not in reg.stages:
            raise RegistryError(f"unknown stage {n!r}; stages: {', '.join(names)}")  # a typo fails before anything runs
    for what, given in (("targets", targets), ("only", only), ("skip", skip), ("from", [from_stage] if from_stage else [])):
        if "all" in (given or []):  # "all" there would pass the check above and then fail later with a ValueError
            raise RegistryError(f"\"all\" is not a stage name for {what}; use --force all to force every stage")
    if only:  # --only: exactly these stages
        wanted = set(only)  # exactly the named stages; their needs are deliberately not added
    elif targets:  # targets: the targets and everything they need
        wanted = reg.closure(targets)  # expanded through the needs
    else:  # no selection: the default pipeline (stages with default=True) and their needs
        # e.g. audit is left out because it is not a default stage
        wanted = reg.closure([s.name for s in order if s.default])
    force = set(force)  # a set for fast membership tests
    if "all" in force:  # "all" means force everything selected
        force = set(wanted)  # replace it with the selected names
    if from_stage:  # --from S: force S and every selected stage after it in run order
        # slice of the run order from S onwards, limited to the selection
        force |= {n for n in names[names.index(from_stage):] if n in wanted}
    state = read_state(project)  # what ran before, with fingerprints
    steps, running = [], set()  # the plan being built, and the stages that will run (their dependants must follow)
    for st in order:  # walk in dependency order
        if st.name not in wanted:  # the stage is outside the selection for this plan
            continue  # not part of this plan
        # "delegate" when this platform cannot run it (e.g. narrate on Windows)
        step = {"stage": st.name, "reasons": [], "where": "here" if platform_ok(st.platforms) else "delegate"}
        if st.name in skip:  # left out on request
            # recorded with its reason so the plan explains itself
            step.update(action="skip", reasons=["skipped on request"])
            steps.append(step)  # keep it in the list
            continue  # continue with the following stage
        # the stage's own check that nothing needs doing (extract when work/book.md exists)
        sat = st.satisfied(project) if st.satisfied else None
        if sat and st.name not in force:  # respected unless the stage is forced
            step.update(action="skip", reasons=[sat])  # the reason comes from the stage's satisfied() function
            steps.append(step)  # listed as a skip with the stage's own reason, so the plan explains it
            continue  # on to the following stage
        reasons = []  # reasons this stage must run
        if st.name in force:  # forced by the caller
            reasons.append("forced")  # forcing runs the stage even when nothing else changed
        up = [d for d in st.needs if d in running]  # needs that are running in this plan
        if up:  # at least one needed stage runs in this plan, so this one must follow it
            # the stage follows them (run() may still skip it if its inputs come out unchanged)
            reasons.append("after " + ", ".join(up))
        miss = missing_outputs(project, st)  # declared outputs that are not there
        if miss:  # some required outputs are absent, or are empty folders
            # a missing output means the stage must run again to rebuild it
            reasons.append("missing " + ", ".join(miss))
        failed = state.get("_errors", {}).get(st.name)  # the error kept when this stage last failed, if it did
        if failed:  # its outputs may be half-made, so they must be rebuilt, never adopted
            reasons.append(f"last run failed ({failed.get('at', 'time unknown')})")  # a reason, so no adoption below
        # A run that was killed (a time limit, a closed terminal) or that died before it could write its error (a full
        # disk) leaves no error record, but it does leave the marker run() wrote when the stage started: its outputs
        # are just as half-made and must never be adopted as current.
        cut = state.get("_running", {}).get(st.name)
        if cut and not failed:
            reasons.append(f"last run was interrupted (started {cut.get('at', 'time unknown')})")
        rec = state.get(st.name)  # what was recorded last time
        fp = fingerprint(project, st, options)  # fingerprint of the current implementation, settings and inputs
        if rec is not None and rec.get("partial"):  # the last run was a test clip or another incomplete run
            reasons.append("last run was partial (e.g. a test clip)")  # so it is still to do
        # never recorded (the project predates tracking or this stage is new)
        elif rec is None or "fingerprint" not in rec:
            # nothing else requires a run, adopting is allowed, and the stage has outputs to adopt
            if not reasons and adopt and st.outputs:
                # outputs already exist: record them as current instead of rebuilding
                step.update(action="adopt", reasons=["outputs already there, recorded as current"])
                steps.append(step)  # the step goes into the plan with action adopt and never runs
                continue  # nothing else to decide for this one
            if not reasons:  # no outputs to adopt (or adoption is off)
                reasons.append("never run")  # run it the first time
        elif rec["fingerprint"].get("all") != fp["all"]:  # recorded, but something changed since
            reasons += _diff(rec["fingerprint"], fp)  # add one reason per changed implementation, setting or input
        if reasons:  # something requires a run
            step["reasons"] = reasons  # store the reasons on the step so plan output and logs can show them
            # it needs another platform and no delegate is configured
            if step["where"] == "delegate" and not os.environ.get("SPEECHKIT_DELEGATE"):
                step["action"] = "blocked"  # cannot run here at all
                # explain how to fix it on Windows
                step["reasons"].append(f"needs {'/'.join(st.platforms)}; this is {current_platform()}. Set "
                                       # the example uses --exec so arguments bypass the Linux shell
                                       "SPEECHKIT_DELEGATE (e.g. \"wsl -d Ubuntu-24.04 --exec\") to run it there")
            else:  # runnable here or through the delegate
                step["action"] = "run"  # runs on this machine, or through SPEECHKIT_DELEGATE when the platform differs
            running.add(st.name)  # dependants of a blocked or running stage get "after ..." reasons
        else:  # no reasons at all: nothing changed since the recorded run
            step.update(action="skip", reasons=["up to date"])  # skip it, giving up to date as the reason
        steps.append(step)  # every selected stage appears in the plan, whatever its action
    return steps  # the plan: one dict per selected stage


# ------------------------------------------------------------------------------------------- running
# Ctx is the second argument of every stage function: logging, the stage's options, the registry and run-wide
# flags such as accept_draft. Stages read options with ctx.opt("workers") or the older form ctx.workers.
class Ctx:
    """What a stage gets besides the project: log, its options, the registry, run-wide flags."""

    # reg: the Registry (for option defaults); log: a function taking one message; options: {stage: {key: value}},
    # possibly with run-wide top-level values; accept_draft: go past the first check stop; stage: whose options apply.
    def __init__(self, reg: Registry, log=print, options: dict | None = None, accept_draft: bool = False,
                 # the stage this context belongs to (None for tools that call stages directly)
                 stage: str | None = None):
        self.reg = reg  # registry, used for declared option defaults
        self.log = log  # logger for progress lines (print by default)
        self.options = options or {}  # never None, so lookups need no checks
        self.accept_draft = accept_draft  # True lets `check` continue without the review stop
        self.stage = stage  # name of the running stage

    # Look up one option for the current stage. key: option name; default: value when nothing provides one.
    # Order: this stage's own options, then a run-wide (non-dict) value of the same name, then the default declared
    # in the stage's options schema, then `default`. Returns the value found.
    def opt(self, key, default=None):
        o = self.options.get(self.stage, {}) if self.stage else {}  # the options dict meant for this stage, or empty
        if key in o:  # given explicitly for this stage
            return o[key]  # an option given for this stage overrides every other source
        # a run-wide value; dict values are other stages' option sets, not values
        if key in self.options and not isinstance(self.options[key], dict):
            return self.options[key]  # e.g. {"workers": 4} at the top level
        st = self.reg.stages.get(self.stage)  # the declared stage, for its schema defaults
        # the option is declared and its schema has a default
        if st and key in st.options and "default" in st.options[key]:
            return st.options[key]["default"]  # e.g. autofix takes 3 per candidate fix
        return default  # nothing found: the caller's fallback

    # Called only when normal attribute lookup fails, so ctx.workers behaves like ctx.opt("workers").
    def __getattr__(self, key):                   # lets older stage code read ctx.workers, ctx.format ...
        if key.startswith("_"):  # private and dunder names must fail normally (copy, pickle and hasattr probe them)
            raise AttributeError(key)  # so those probes behave as for any object
        return self.opt(key)  # missing options read as None instead of raising


# Translate a local path into the path the delegate sees. cmd: the delegate command prefix; path: a local path.
# SPEECHKIT_DELEGATE_PATHMAP ("C:\=/mnt/c/;D:\=/mnt/d/") is used when set; otherwise a WSL delegate is asked via
# `wslpath -a`; any other delegate gets the path unchanged. Returns the remote path as a string; may start wsl.
def remote_path(cmd: list[str], path: Path) -> str:
    pm = os.environ.get("SPEECHKIT_DELEGATE_PATHMAP", "")  # semicolon-separated local=remote prefix pairs
    if pm:  # an explicit map wins over asking WSL
        s = str(path)  # text form of the local path
        for pair in pm.split(";"):  # try each local=remote prefix pair in the order written
            if "=" in pair:  # a pair without an equals sign is malformed and skipped
                a, b = pair.split("=", 1)  # split at the first "=" only
                if s.lower().startswith(a.lower()):  # Windows paths compare case-insensitively
                    # swap the prefix and turn backslashes into forward slashes
                    return (b + s[len(a):]).replace("\\", "/")
        return s  # no pair matched: unchanged
    if Path(cmd[0]).name.lower() in ("wsl", "wsl.exe"):  # a WSL delegate without a map: ask WSL itself
        # wslpath -a prints the absolute Linux path, e.g. /mnt/c/Users/me/Book; check=True raises on failure
        r = subprocess.run(cmd + ["wslpath", "-a", str(path)], capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")
        return r.stdout.strip()  # the translated path without the trailing newline
    return str(path)  # not WSL and no map: assume both sides see the same paths


# The reverse of remote_path: turn a path printed by the delegated run back into a local path. cmd: delegate
# prefix; remote: the remote path string. Uses SPEECHKIT_DELEGATE_PATHMAP or `wslpath -w`; on any failure the
# remote text is returned unchanged. Returns a string.
def local_path(cmd: list[str], remote: str) -> str:
    """Inverse of remote_path, for paths that come back in a delegated result."""
    pm = os.environ.get("SPEECHKIT_DELEGATE_PATHMAP", "")  # the same prefix map as remote_path
    if pm:  # an explicit map is used instead of asking WSL
        for pair in pm.split(";"):  # try the prefix pairs in order; the first match wins
            if "=" in pair:  # skip text without an equals sign, such as an empty trailing part
                a, b = pair.split("=", 1)  # local prefix, remote prefix
                if remote.startswith(b):  # remote side compared case-sensitively (Linux paths)
                    return a + remote[len(b):].replace("/", "\\")  # local prefix plus the rest with Windows separators
        return remote  # outside every mapped prefix
    if Path(cmd[0]).name.lower() in ("wsl", "wsl.exe"):  # WSL without a map
        # wslpath -w prints the Windows form, e.g. C:\Users\me\out.mp3
        r = subprocess.run(cmd + ["wslpath", "-w", remote], capture_output=True, text=True, encoding="utf-8",
                           errors="replace")  # undecodable bytes become replacement characters instead of raising
        # fall back to the remote text if wslpath failed or printed nothing
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else remote
    return remote  # a delegate that is not WSL is assumed to share the same paths


# Parse SPEECHKIT_DELEGATE (or `raw`) into an argument list, e.g. "wsl -d Ubuntu-24.04" becomes
# ["wsl", "-d", "Ubuntu-24.04", "--exec"]. Also used by the tests and to show the effective command.
def delegate_prefix(raw: str | None = None) -> list[str]:
    """SPEECHKIT_DELEGATE as an argument list. Quotes are honoured on every platform, and a bare `wsl` gets
    `--exec` so the Linux shell never re-parses the arguments (JSON, $, quotes, brackets pass through intact)."""
    # raw=None reads the environment; a KeyError means no delegate is set
    raw = raw if raw is not None else os.environ["SPEECHKIT_DELEGATE"]
    # Split like a shell. On Windows (posix=False) shlex keeps the quote characters, so a token wrapped in matching
    # quotes has them removed here, which makes quoting work the same on every platform.
    cmd = [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t
           # POSIX rules except on Windows, where backslashes in paths must survive
           for t in shlex.split(raw, posix=(os.name != "nt"))]
    # a wsl command without an explicit "run this program" switch
    if cmd and Path(cmd[0]).name.lower() in ("wsl", "wsl.exe") and not ({"--exec", "-e", "--"} & set(cmd)):
        # --exec runs the program directly, so JSON, $, quotes and brackets are not re-parsed by bash
        cmd.append("--exec")
    return cmd  # the command prefix, ready to have the toolkit command appended


# Everything a delegated call needs: the command prefix, the toolkit folder as the delegate sees it, and the
# Python interpreter to run there. Returns (cmd, root, python). root is "" for a pip-installed copy of speechkit-narrate: its
# folder is site-packages, which holds this machine's other packages too and must not reach the delegate's PYTHONPATH,
# so the delegate's own Python has to have speechkit-narrate installed (the PowerShell module installs it there).
def _delegate_cmd() -> tuple[list[str], str, str]:
    if os.environ.get("SPEECHKIT_DELEGATION_DEPTH"):
        raise RegistryError("delegate still cannot run this stage on its platform; refusing recursive delegation")
    cmd = delegate_prefix()  # SPEECHKIT_DELEGATE split into arguments, e.g. wsl -d Ubuntu-24.04 --exec
    from . import env  # local import: env.py works out where the toolkit checkout is
    # SPEECHKIT_DELEGATE_ROOT overrides (an empty value counts as unset); otherwise the local checkout translated into
    # a remote path, or nothing at all for an installed copy
    root = os.environ.get("SPEECHKIT_DELEGATE_ROOT") or ("" if env.installed() else remote_path(cmd, env.REPO))
    # SPEECHKIT_DELEGATE_PYTHON, e.g. a venv inside WSL; default python3
    return cmd, root, os.environ.get("SPEECHKIT_DELEGATE_PYTHON", "python3")


# The words that start a delegated run, placed after the delegate prefix: the Linux `env` command, PYTHONPATH when
# there is a toolkit folder to point at, and an empty SPEECHKIT_PLATFORM so the remote side detects its own platform.
# root: the remote toolkit folder, or "" for none. Returns a list of strings.
def _remote_env(root: str) -> list[str]:
    # PYTHONPATH finds the toolkit folder; it is left out when the remote Python has speechkit-narrate installed
    return ["env", *([f"PYTHONPATH={root}"] if root else []), "SPEECHKIT_PLATFORM=", "SPEECHKIT_DELEGATION_DEPTH=1"]


# Advice for a failed delegated run whose Python could not import speechkit_narrate although no toolkit folder was sent.
# root: the remote toolkit folder sent ("" for none); lines: the last lines the remote run wrote to stderr.
# Returns text to append to the error message, or "" when the advice does not apply.
def _missing_package_hint(root: str, lines: list[str]) -> str:
    if root:  # a toolkit folder was sent, so a missing package means that folder is wrong, not an install problem
        return ""  # no advice for that case
    # Python words the error as No module named 'speechkit_narrate' (older versions leave the quotes out)
    if not any("No module named 'speechkit_narrate'" in s or "No module named speechkit_narrate" in s for s in lines):
        return ""  # some other failure
    # point at both fixes: an installed package in the delegate's Python, or a toolkit folder
    return (" (the delegate's Python has no speechkit_narrate package: set SPEECHKIT_DELEGATE_ROOT to the checkout inside"
            " WSL (Set-NarrationBackend Auto does), or pip install the checkout into that Python)")


# Wraps a log function so the last lines it receives are remembered as well as passed on. log: the function to wrap.
# Returns (keep, tail): keep is the function to hand to _stream, and tail is the list that holds the last 30 lines.
def _tee(log) -> tuple:
    tail: list[str] = []  # filled by keep, read by the caller after the run

    def keep(line: str) -> None:  # relays one remote log line and remembers it
        tail.append(line)  # remember this line
        del tail[:-30]  # keep only the last 30, which is where a Python traceback ends
        log(line)  # relay it exactly as before
    return keep, tail  # the wrapper and the list it fills


# Run a delegated toolkit command and stream its log. args: the full command; log: called with each stderr line.
# Returns (exit code, the parsed final JSON line of stdout or {"raw": tail}, the full stdout text).
def _stream(args, log) -> tuple[int, dict, str]:
    import re  # regular expressions, for the timestamp prefix
    import threading  # stdout is read on a second thread
    # both pipes captured; text mode decodes as UTF-8
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace")  # invalid bytes are replaced instead of raising
    out = []  # collects the whole stdout text (one element)
    # reading stdout in parallel keeps a full stdout pipe from blocking the child
    t = threading.Thread(target=lambda: out.append(p.stdout.read()), daemon=True)
    t.start()  # begin draining stdout in the background before stderr is read
    for line in p.stderr:  # stderr carries the remote run's log, line by line as it arrives
        # drop the remote "[HH:MM:SS] " prefix because the local logger adds its own
        log(re.sub(r"^\[\d\d:\d\d:\d\d\] ", "", line.rstrip("\n")))
    p.wait()  # wait for the child to exit
    t.join()  # wait for the stdout reader to finish
    text = (out[0] if out else "").strip()  # everything the remote run printed on stdout
    try:  # --json mode prints exactly one JSON object as the last line
        res = json.loads(text.splitlines()[-1]) if text else {}  # parse that last line
    except json.JSONDecodeError:  # stdout was not JSON (a crash before the result)
        res = {"raw": text[-2000:]}  # keep the last 2000 characters for the error message
    return p.returncode, res, text  # exit code, parsed result, raw text


# Run a standalone tool through SPEECHKIT_DELEGATE (used when the tool cannot run on this platform, like speak on
# Windows). t: the Tool; params: its arguments; log: progress logger. Path parameters (schema "format": "path")
# are translated to the delegate's view and "files" in the result back. Returns the tool's result dict.
def delegate_tool(t: Tool, params: dict, log=print) -> dict:
    """Run a standalone tool through SPEECHKIT_DELEGATE; path parameters are translated there and back."""
    cmd, root, py = _delegate_cmd()  # delegate prefix, toolkit root there, Python there
    sent = dict(params)  # a copy, so the caller's dict is not modified
    for k, sch in t.params.items():  # each declared parameter with its schema
        if k in sent and sch.get("format") == "path" and sent[k]:  # given, declared as a path, and not empty
            sent[k] = remote_path(cmd, Path(sent[k]).resolve())  # resolve locally first, then translate
    # The Linux `env` command sets variables for the remote process: PYTHONPATH finds the toolkit (left out when the
    # remote Python has speechkit-narrate installed), and an empty SPEECHKIT_PLATFORM clears any override that reaches the
    # remote side, so it detects its own platform and runs the tool.
    args = cmd + _remote_env(root) + [py, "-m", "speechkit_narrate", "--json", "tool", t.name,
                                      "--params", json.dumps(sent)]  # the tool's arguments as one JSON string
    log(f"{t.name}: delegated to {os.environ['SPEECHKIT_DELEGATE']}")  # say where the work is going
    keep, tail = _tee(log)  # the log wrapper, and the last remote lines it collects for the advice below
    code, res, text = _stream(args, keep)  # run it and stream the log
    if code != 0 or not res.get("ok", False):  # non-zero exit or {"ok": false}
        # prefer the toolkit's own error, else the tail of the output; advice is added when the package is missing
        raise RuntimeError(f"delegated tool {t.name} failed (exit {code}): {res.get('error') or text[-1500:]}"
                           # the install advice, when the package is the thing that is missing
                           f"{_missing_package_hint(root, tail)}")
    res.pop("ok", None)  # remove the envelope fields the local caller adds itself
    res.pop("tool", None)  # the tool name, re-added by cmd_tool
    if isinstance(res.get("files"), list):  # the tool reported the files it produced, as remote paths
        res["files"] = [local_path(cmd, f) for f in res["files"]]  # translate each remote path back
    return res  # the remote tool's result


# Run one stage of this project through SPEECHKIT_DELEGATE (e.g. narrate in WSL) and stream its log into ctx.log.
# project: the Project; st: the Stage; ctx: its Ctx (options, logger); accept_draft: passed on. Returns the
# stage's result from the remote run. Raises RuntimeError when the remote run fails.
def delegate(project, st: Stage, ctx: Ctx, accept_draft: bool) -> dict:
    """Run one stage through SPEECHKIT_DELEGATE (e.g. WSL) and stream its log."""
    cmd, root, py = _delegate_cmd()  # delegate prefix, remote toolkit root, remote Python
    opts = ctx.options.get(st.name, {})  # only the options addressed to this stage are forwarded
    # PYTHONPATH (when there is a toolkit folder) and an empty SPEECHKIT_PLATFORM, as in delegate_tool
    remote_env = _remote_env(root)
    token = lock_token(project.root)
    if token:
        remote_env.append(f"{DELEGATE_TOKEN}={token}")
    args = cmd + remote_env + [py, "-m", "speechkit_narrate", "--json", "run",
                                      # --only and --force make the remote run exactly this stage; --no-adopt
                                      # prevents it from merely recording existing outputs; the project folder is
                                      # passed in its remote form.
                                      remote_path(cmd, project.root), "--only", st.name, "--force", st.name,
                                      "--no-adopt"]
    if accept_draft:  # forward the review-stop override
        args.append("--accept-draft")  # --accept-draft for the remote check
    if opts:  # any options for this stage
        args += ["--options", json.dumps({st.name: opts})]  # passed as JSON keyed by the stage name
    ctx.log(f"{st.name}: delegated to {os.environ['SPEECHKIT_DELEGATE']}")  # say where the stage is going
    keep, tail = _tee(ctx.log)  # the log wrapper, and the last remote lines it collects for the advice below
    code, res, text = _stream(args, keep)  # run and stream the remote log
    if code != 0 or not res.get("ok", False):  # the remote run failed or said ok: false
        # prefer the toolkit's own error text; advice is added when the package is missing
        raise RuntimeError(f"delegated {st.name} failed (exit {code}): {res.get('error') or text[-1500:]}"
                           # the install advice, when the package is the thing that is missing
                           f"{_missing_package_hint(root, tail)}")
    # the step of this stage in the remote run's report
    ran = [s for s in res.get("steps", []) if s.get("stage") == st.name]
    # its result, or a marker saying it was delegated
    return (ran[0].get("result") if ran else None) or {"delegated": os.environ["SPEECHKIT_DELEGATE"]}


# Bring the selected stages up to date: plan(), then run each "run" step in order, record state after each, and
# stop early when a stage returns {"stop": why}. Parameters as for plan(), plus options ({stage: {key: value}}),
# accept_draft, log (one-message logger), dry_run (return the plan only). Returns {"ok", "steps", "stopped",
# "ran"}. Side effects: runs stage code, may delegate, writes state.json. Re-raises a stage's exception.
@locked_project
def run(project, reg: Registry, targets=None, force=(), only=None, skip=(), from_stage=None, options=None,
        # adopt and dry_run are passed straight to plan / handled below
        accept_draft=False, log=print, adopt=True, dry_run=False) -> dict:
    project.__init__(project.root)
    steps = plan(project, reg, targets, force, only, skip, from_stage, adopt, options)  # the decision per stage
    # steps that cannot run on this platform without a delegate
    blocked = [s for s in steps if s["action"] == "blocked"]
    if dry_run:  # --dry-run: return the plan without running or recording anything
        return {"ok": not blocked, "dry_run": True, "steps": steps}  # ok is false when something would be blocked
    if blocked:  # refuse before running anything
        # list every blocked stage with its last reason (the fix)
        raise RegistryError("cannot run here: " + "; ".join(f"{s['stage']}: {s['reasons'][-1]}" for s in blocked))
    state = read_state(project)  # current state, updated as stages finish
    stopped = None  # set when a stage asks the pipeline to halt
    # the plan as events see it: each stage with its planned action, so a status page can show what is still to come
    log_event(project, "run-start", steps=[{"stage": s["stage"], "action": s["action"]} for s in steps])
    for step in steps:  # walk the plan in dependency order
        st = reg.stages[step["stage"]]  # the Stage behind this step
        if step["action"] == "adopt":  # existing outputs from before tracking are recorded instead of rebuilt
            record(project, st, state, options=options, adopted=True)  # recorded as current without running
            write_state(project, state)  # persist the adoption immediately so a later failure cannot lose it
            log_event(project, "adopt", stage=st.name)  # adopted, so it will not run
            continue  # adopted steps need no further work
        if step["action"] != "run":  # skip and blocked steps
            continue  # nothing to execute for them, so go on to the next planned step
        # If the only reasons are "after <upstream>", the upstream stages have now run: when this stage's inputs and
        # outputs come out exactly as recorded (same fingerprint, nothing missing), running it again would be wasted
        # work.
        if all(r.startswith("after ") for r in step["reasons"]) and st.name in state and \
                "fingerprint" in state[st.name] and not missing_outputs(project, st) and \
                state[st.name]["fingerprint"].get("all") == fingerprint(project, st, options)["all"]:  # upstream changed nothing
            # downgraded to a skip, with the reason
            step["action"], step["reasons"] = "skip", ["inputs unchanged after upstream ran"]
            log_event(project, "skip", stage=st.name, reasons=step["reasons"])  # skipped after all, with the reason
            continue  # move on without running it
        log(f"run: {st.name} ({'; '.join(step['reasons'])})")  # announce the stage and why it runs
        # the same announcement as an event, with where it runs ("here" or "delegate")
        log_event(project, "start", stage=st.name, reasons=step["reasons"], where=step["where"])
        ctx = Ctx(reg, log, options, accept_draft, st.name)  # a fresh context with this stage's options
        t0 = time.time()  # start time for the duration
        # Mark the stage as running before it touches anything, and save that at once: if this process is killed or
        # dies before it can record a result, the next plan sees the marker and rebuilds instead of adopting.
        state.setdefault("_running", {})[st.name] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": os.getpid()}
        write_state(project, state)
        try:  # any exception is recorded before it propagates
            if step["where"] == "delegate":  # another platform: run it there
                # run this stage remotely through SPEECHKIT_DELEGATE, for example narrate in WSL
                res = delegate(project, st, ctx, accept_draft)
            else:  # runs on this machine
                res = st.run(project, ctx)
            if not (isinstance(res, dict) and res.get("partial")):
                incomplete = missing_outputs(project, st)
                if incomplete and not (st.satisfied and st.satisfied(project)):
                    raise RegistryError(f"{st.name} returned without valid outputs: {', '.join(incomplete)}")  # the stage function itself
        except BaseException as e:  # the stage raised (or was interrupted): record the failure, then re-raise
            state.pop(st.name, None)  # forget its record so the next plan runs it again
            state.get("_running", {}).pop(st.name, None)  # the error record below now says what happened
            # keep the error for `status`
            state.setdefault("_errors", {})[st.name] = {"error": f"{type(e).__name__}: {e}",
                                                        # with the local time it happened
                                                        "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            write_state(project, state)  # save the error now, since the raise below ends this run
            # mark the step for whoever reads the steps list
            step["action"], step["error"] = "failed", f"{type(e).__name__}: {e}"
            # the failure as an event, with how long the stage ran before it failed
            log_event(project, "failed", stage=st.name, seconds=round(time.time() - t0, 1), error=step["error"])
            log_event(project, "run-end", ok=False, failed=st.name)  # the run ends here
            raise  # the CLI and MCP turn it into an error result
        state.get("_running", {}).pop(st.name, None)  # finished: the interruption marker goes
        project.__init__(project.root)                       # a stage may have changed project.json
        step["seconds"] = round(time.time() - t0, 1)  # wall-clock seconds, 0.1 s resolution
        step["result"] = _jsonable(res)  # results are trimmed and made JSON-safe
        state.get("_errors", {}).pop(st.name, None)  # a success clears an earlier error
        if isinstance(res, dict) and res.get("partial"):  # a partial run such as a render test clip
            state[st.name] = {"partial": True, "finished": time.strftime("%Y-%m-%dT%H:%M:%S")}   # e.g. a test clip
            write_state(project, state)  # save the partial marker so status shows the clip was made
            step["action"] = "ran"  # it did run, but stays "to do"
            log_event(project, "done", stage=st.name, seconds=step["seconds"], partial=True)  # finished, but partial
            continue  # partial results never trigger refreshes or a stop
        # up to date now; seconds and impl are kept for reports
        record(project, st, state, options=options, seconds=step["seconds"], impl=st.impl)
        for other in st.refreshes:  # stages whose outputs this one rewrites (autofix rewrites the script and narration)
            o = reg.stages.get(other)  # the refreshed stage, if registered
            # refresh the record only when every required output of that stage exists
            if o and not missing_outputs(project, o):
                record(project, o, state, options=options, refreshed_by=st.name)  # re-record so it does not look stale next time
        write_state(project, state)  # save after every stage, so a crash later keeps this progress
        step["action"] = "ran"  # the stage completed and its record is saved
        log(f"done: {st.name} in {step['seconds']} s")  # said as soon as it finishes, not only in the final summary
        log_event(project, "done", stage=st.name, seconds=step["seconds"])  # the same as an event
        if isinstance(res, dict) and res.get("stop"):  # the stage asked to halt (check on a fresh draft)
            stopped = {"stage": st.name, "why": res["stop"]}  # remember where and why
            log(f"run: stopped after {st.name}: {res['stop']}")  # explain in the log why the run ended early
            break  # remaining steps keep their planned action "run" although they did not run
    # the end of the run as an event: the stages that ran, and where and why it stopped early, if it did
    log_event(project, "run-end", ok=True, ran=[s["stage"] for s in steps if s["action"] == "ran"], stopped=stopped)
    return {"ok": True, "steps": steps, "stopped": stopped,  # ok, the steps with their final actions, and the stop
            "ran": [s["stage"] for s in steps if s["action"] == "ran"]}  # stage names that actually ran


# Make a stage's return value safe to put in JSON and small enough to report: nested containers are limited to
# 6 levels, 200 keys per dict and 500 items per list, Paths and unknown objects become text. Returns the copy.
def _jsonable(x, depth=0):
    if depth > 6:  # deeper than six levels
        return str(x)  # too deep: the rest of the structure is reduced to its text form
    if isinstance(x, dict):  # mappings keep their keys, turned into strings for JSON
        # string keys, first 200 entries, each value converted recursively
        return {str(k): _jsonable(v, depth + 1) for k, v in list(x.items())[:200]}
    if isinstance(x, (list, tuple, set)):  # lists, tuples and sets all become lists
        # only the first 500 items are kept, each converted recursively
        return [_jsonable(v, depth + 1) for v in list(x)[:500]]
    if isinstance(x, Path):  # filesystem paths are not a JSON type
        return str(x)  # so they are written as plain path strings
    # values JSON can hold directly: text, numbers, booleans and null
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x  # passed through as they are
    return str(x)  # anything else as text


# What is done in a project, for `speechkit-narrate status` and MCP project_status. project: a Project; reg: the
# Registry. Returns {"project", "title", "stages": [...]} with, per stage, done, partial, finish time, adopted,
# missing outputs and the last error.
def status(project, reg: Registry) -> dict:
    state = read_state(project)  # the recorded fingerprints, finish times, adoptions and errors
    out = []  # one row per stage
    for st in reg.topo():  # every registered stage in dependency order, as the pipeline runs them
        rec = state.get(st.name)  # its record, or None
        # done means a full recorded run (a fingerprint); partial means a test clip.
        out.append({"stage": st.name, "done": bool(rec and "fingerprint" in rec), "partial": bool((rec or {}).get("partial")),
                    "finished": (rec or {}).get("finished"),  # when it last finished
                    # adopted rather than run; outputs still missing
                    "adopted": bool((rec or {}).get("adopted")), "missing": missing_outputs(project, st),
                    # the last error recorded for this stage, if any
                    "error": state.get("_errors", {}).get(st.name, {}).get("error")})
    return {"project": str(project.root), "title": project.title, "stages": out}  # project folder, title and the rows


# True when `name` matches any of the shell-style patterns (fnmatch rules: *, ?, [...]). Not called anywhere in
# the toolkit at present.
def glob_match(name: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)  # any() stops at the first matching pattern
