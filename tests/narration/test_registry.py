"""Stage registry, planner, plug-ins, delegation, standalone tools, JSON CLI, and Windows-portability guards."""
from __future__ import annotations  # annotations stay strings, so hints like "-> Project" cost nothing at import

# Standard-library imports only; the toolkit itself is imported below, after its folder is on sys.path.
import ast  # parses the toolkit's own source for the encoding guard in Portability
import json  # reads the one JSON line the CLI prints in --json mode
import os  # environment variables (SPEECHKIT_PLUGINS, SPEECHKIT_PLATFORM, ...) set around calls
import re  # counts words the way the wordcount plug-in does
import shutil  # rmtree for fresh scratch folders
import subprocess  # runs `python -m speechkit_narrate` as a separate process, the way scripts and PowerShell do
import sys  # sys.executable is the interpreter running the tests; sys.path is extended below
import tempfile  # mkdtemp gives the module its scratch folder
import textwrap  # dedent lets plug-in source be written indented inside a test method
import unittest  # plain unittest: python3 -m unittest tests.narration.test_registry
from pathlib import Path  # object paths for folders and files

# The repository root: this file is tests/narration/test_registry.py, so parents[2] is the folder holding the
# speechkit_narrate package and tests/.
REPO = Path(__file__).resolve().parents[2]  # resolve() first so a relative __file__ still gives an absolute root
sys.path.insert(0, str(REPO))  # put the checkout first on the import path, ahead of any installed speechkit-narrate

# The registry module is imported as R because the tests call many of its functions (load, plan, run, ...).
from speechkit_narrate import registry as R  # noqa: E402
# Project wraps one project folder; create() makes a new one from a text file.
from speechkit_narrate.project import Project, create  # noqa: E402

# Scratch area for every test in this file: a fresh temporary folder made in setUpModule and removed in
# tearDownModule. SPEECHKIT_TEST_SCRATCH chooses the parent folder (e.g. off a slow or small drive).
SCRATCH = Path(".")  # replaced in setUpModule
FAKE_WSL = REPO / "tests" / "narration" / "fixtures" / "fake_wsl.py"  # stands in for wsl.exe in the Delegation tests

# Test plug-in with a "stats" stage (needs script) and a "wordcount" tool.
STATS = REPO / "tests" / "narration" / "fixtures" / "reading_stats.py"
# A plug-in that replaces the built-in script stage with one that does the same work and says so in its result.
SCRIPT_SWAP_SRC = '''
from speechkit_narrate.registry import stage

@stage("script", replaces=True)
def script(project, ctx):
    from speechkit_narrate.text import docmodel
    r = docmodel.build(project)
    r["by"] = "plug-in"
    return r
'''

# A tiny text in the toolkit's markup: a title line, three "# n. ..." chapter headings (h1), plain paragraphs,
# a quoted sentence and a bullet list, so extract/check/script have real structure to work on while every
# stage still finishes in a fraction of a second.
BOOK = """The Lantern Keeper

# 1. The first night

The keeper climbed the stairs at dusk and lit the lamp. The sea was calm, and the boats came home early.

# 2. The storm

By midnight the wind had turned. "Keep it burning," she said, and wound the clockwork again.

- check the oil
- trim the wick
- clean the glass

# 3. Morning

The harbour woke to a quiet sky.
"""


def quiet(*_):
    """A log function that discards every message."""


# Creates a fresh project folder for one test.
# name: sub-folder under SCRATCH (str). Any old folder of that name is deleted first, the BOOK text is written to
# lantern.txt, and speechkit_narrate.project.create() copies it into <name>/project/input and writes project.json.
# Returns the Project object. Side effects: files under SCRATCH only.
def make_project(name: str) -> Project:  # one call gives each test its own clean project
    d = SCRATCH / name  # the per-test folder; the project itself goes one level below it
    if d.exists():  # a previous test may have left this folder behind
        shutil.rmtree(d)  # start clean so no old state.json or outputs leak into the test
    d.mkdir(parents=True)
    src = d / "lantern.txt"  # the source text lives outside the project, like a user's own file
    src.write_text(BOOK, encoding="utf-8")  # UTF-8 written explicitly so the test behaves the same on Windows
    # create() copies the input into project/input and fills title and subtitle in project.json
    return create(d / "project", src, title="The Lantern Keeper", subtitle="A short test book")


# Writes a plug-in source text to SCRATCH/<name>.py and returns its path.
def plugin_file(name: str, src: str) -> Path:
    f = SCRATCH / f"{name}.py"
    f.write_text(textwrap.dedent(src), encoding="utf-8")
    return f


# Runs `python -m speechkit_narrate --json <args>` as a separate process and returns (result, completed process).
# args: command words; Paths and other objects are turned into strings. env: extra environment variables laid
# over the current ones. check: when True, a result without "ok": true fails the test with the tail of stdout and
# stderr. This is the exact contract PowerShell and the MCP server rely on: one JSON object as the last stdout line.
def cli(*args, env=None, check=True):
    # The child sees this checkout (PYTHONPATH) and decodes and encodes text as UTF-8 (PYTHONUTF8=1), whatever
    # the host's default code page. Caller-supplied variables come last so they win.
    e = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONUTF8": "1", **(env or {})}
    r = subprocess.run([sys.executable, "-m", "speechkit_narrate", "--json", *map(str, args)], capture_output=True,
                       text=True, encoding="utf-8", env=e, cwd=str(REPO), timeout=60)
    # The result is the last stdout line parsed as JSON; an empty stdout (a crash before any result) gives {}.
    out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    if check and not out.get("ok"):  # a failed command is a test failure unless the caller expects it
        raise AssertionError(f"{args}: exit {r.returncode}: {r.stdout[-800:]} {r.stderr[-1500:]}")
    return out, r  # callers inspect both the JSON and r.stderr / r.returncode


# unittest calls this once before any test in the module: a fresh scratch folder.
def setUpModule():
    global SCRATCH
    parent = os.environ.get("SPEECHKIT_TEST_SCRATCH") or None
    if parent:
        Path(parent).mkdir(parents=True, exist_ok=True)
    SCRATCH = Path(tempfile.mkdtemp(prefix="speechkit-narrate-test-registry-", dir=parent)).resolve()


# ... and this once after the last test: the scratch folder goes.
def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)


# Declarations: the registry itself, without running stages. Checks the built-in stages and tools, the rules for
# a plug-in replacing a stage, plug-in loading (fresh source every time, errors reported as JSON) and the
# identity string that goes into each stage's fingerprint.
class Declarations(unittest.TestCase):
    # Proves the toolkit ships its nine stages in a valid dependency order, the two standalone tools, and the
    # platform and default flags the planner and the PowerShell module depend on.
    def test_builtin_stages_and_order(self):
        reg = R.load()  # built-in stages and tools only: no project, no SPEECHKIT_PLUGINS
        names = [s.name for s in reg.topo()]  # topo() is the run order (dependencies first)
        self.assertEqual(names, ["extract", "check", "script", "prevoice", "narrate", "autofix", "audit", "report",
                                 "export"])
        # Every stage must come after each stage it needs; otherwise the planner would run it on stale inputs.
        for s in reg.topo():
            for d in s.needs:  # each declared prerequisite of this stage
                self.assertLess(names.index(d), names.index(s.name), f"{s.name} before its need {d}")
        # the exact tool set: a new or renamed tool must update this test (and the PowerShell commands)
        self.assertEqual(set(reg.tools), {"speak", "quick"})
        # the voice stage is Linux-only, which is what triggers delegation on Windows
        self.assertEqual(reg.stages["narrate"].platforms, ("linux",))
        self.assertEqual(reg.tools["speak"].platforms, ("linux",))
        self.assertFalse(reg.stages["audit"].default)  # audit is not part of a plain `run`; it is only run on request
        info = reg.describe()  # describe() is what `speechkit-narrate stages` and the MCP server publish
        # every stage entry carries its options and platforms for the generated parameters
        self.assertTrue(all("options" in s and "platforms" in s for s in info["stages"]))

    # Proves the replace rules in Registry.add_stage: a same-named stage needs replaces=True, replaces=True needs an
    # existing stage, a replacement inherits the original's undeclared fields, and a dependency cycle is caught.
    def test_replace_rules(self):
        reg = R.load()  # fresh registry, so the additions below do not affect other tests
        with self.assertRaises(R.RegistryError):  # adding a second "export" without replaces=True must be refused
            reg.add_stage(R.Stage("export", run=lambda p, c: None, source="x"))
        with self.assertRaises(R.RegistryError):  # replaces=True for a stage that does not exist is also an error
            reg.add_stage(R.Stage("nosuch", run=lambda p, c: None, source="x"), replaces=True)
        # the accepted replacement: fields left at their defaults are copied from the original
        st = reg.add_stage(R.Stage("export", run=lambda p, c: None, source="x"), replaces=True)
        self.assertEqual(st.needs, ("narrate",))                    # inherited declarations
        reg.add_stage(R.Stage("loopy", run=lambda p, c: None, needs=("loopy2",), source="x"))
        # adding them works; the cycle is only found when the order is computed
        reg.add_stage(R.Stage("loopy2", run=lambda p, c: None, needs=("loopy",), source="x"))
        with self.assertRaises(R.RegistryError):  # topo() must raise rather than loop forever
            reg.topo()  # raises RegistryError "stage cycle: loopy -> loopy2 -> loopy"

    # Proves load_plugin compiles a .py plug-in from its current source on every load. A bytecode cache would trust a
    # .pyc whose source has the same size and modification second, and would silently run the old code.
    def test_plugin_edit_is_picked_up_at_once(self):
        """Edited twice within a second at the same size: the new source must load, not a cached compile."""
        plug = SCRATCH / "flip.py"  # a scratch plug-in file outside any project
        body = ('from speechkit_narrate.registry import tool\n@tool("flip", params={})\ndef flip(log=print):\n'
                '    return {"files": [], "v": "%s"}\n')
        plug.write_text(body % "A", encoding="utf-8")
        self.assertEqual(R.load(extra_plugins=[plug]).tools["flip"].run()["v"], "A")
        plug.write_text(body % "B", encoding="utf-8")  # version B, written within the same second at the same size
        self.assertEqual(R.load(extra_plugins=[plug]).tools["flip"].run()["v"], "B")

    # Proves that a plug-in which fails on import makes the CLI print a JSON error (ok false, the message) and exit 2,
    # instead of a Python traceback that a script or the PowerShell module could not parse.
    def test_broken_plugin_gives_json_error(self):
        bad = plugin_file("broken", "raise RuntimeError('boom')\n")
        out, r = cli("stages", env={"SPEECHKIT_PLUGINS": str(bad)}, check=False)
        self.assertFalse(out["ok"])  # the CLI reports failure in the JSON object
        self.assertIn("boom", out["error"])  # the original exception text is carried through
        self.assertEqual(r.returncode, 2)  # 2 is the CLI's exit code for registry and project errors

    # Proves SPEECHKIT_PLUGINS is honoured by R.load() and that impl, the implementation identity in each fingerprint,
    # includes a source hash for plug-ins but not for built-in stages.
    def test_plugin_env_and_impl_identity(self):
        os.environ["SPEECHKIT_PLUGINS"] = str(STATS)
        try:  # always remove the variable again, even if load fails
            reg = R.load()  # loads built-ins, then the plug-in named in the environment
        finally:
            del os.environ["SPEECHKIT_PLUGINS"]  # later tests must not see the plug-in
        self.assertIn("stats", reg.stages)  # the plug-in's stage was registered
        self.assertIn("wordcount", reg.tools)  # and its standalone tool too
        self.assertIn("#", reg.stages["stats"].impl)               # plug-ins: source hash in the identity
        self.assertNotIn("#", reg.stages["script"].impl)           # built-ins: name + version only


# Pipeline: a real project run through the planner in three ordered steps that share one project folder (test_1_,
# test_2_ and test_3_ sort in that order). Covers the first-run review stop, change tracking by settings and
# inputs, dry runs, project plug-ins that replace a stage, partial runs and adoption of existing outputs.
class Pipeline(unittest.TestCase):
    @classmethod  # one project for the whole class
    def setUpClass(cls):
        cls.p = make_project("pipeline")
        cls.reg = R.load(cls.p)  # registry including that project's own plug-ins (none yet)

    # Proves the planner end to end: the first run stops after check for review, the second run builds the script,
    # nothing runs when nothing changed, a changed setting re-runs only the stages that read it, a changed input
    # re-runs everything downstream, and a dry run changes nothing.
    def test_1_gate_then_script_then_tracking(self):
        p, reg = self.p, self.reg
        r = R.run(p, reg, targets=["script"], log=quiet)
        self.assertEqual(r["stopped"]["stage"], "check")           # first time: stop for review of the draft
        self.assertTrue(p.p("book").exists())  # the review copy exists now
        r = R.run(p, reg, targets=["script"], log=quiet)  # book.md exists, so the review gate no longer stops it
        self.assertEqual(r["ran"], ["script"])  # extract and check are current, so only script runs
        self.assertTrue(p.p("docmodel").exists() and p.p("request").exists())
        acts = {s["stage"]: s["action"] for s in R.plan(p, reg, targets=["script"])}
        self.assertEqual(acts, {"extract": "skip", "check": "skip", "script": "skip"})
        p.save_settings({"spoken": {"years": True}})  # deep-merges spoken.years into project.json and reloads p.cfg
        steps = {s["stage"]: s for s in R.plan(p, reg, targets=["script"])}
        self.assertEqual(steps["script"]["action"], "run")  # script declares the "spoken" setting
        self.assertIn("setting spoken changed", steps["script"]["reasons"])
        self.assertEqual(steps["check"]["action"], "skip")  # check does not read spoken settings
        R.run(p, reg, targets=["script"], log=quiet)
        with open(p.p("book"), "a", encoding="utf-8") as f:  # change the text itself (the input of check and script)
            f.write("\nOne more line at the end.\n")
        steps = {s["stage"]: s for s in R.plan(p, reg, targets=["script"])}
        self.assertEqual([steps[k]["action"] for k in ("check", "script")], ["run", "run"])
        self.assertIn("input book changed", steps["check"]["reasons"])
        dry = R.run(p, reg, targets=["script"], dry_run=True, log=quiet)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(R.plan(p, reg, targets=["script"])[1]["action"], "run")   # dry run changed nothing

    # Proves project plug-ins: a file in <project>/plugins that replaces script is loaded with the project, the swap
    # is reported, it re-runs script because the implementation changed, and removing it makes script run again.
    def test_2_project_plugins_replace_and_restore(self):
        p = self.p
        R.run(p, R.load(p), targets=["script"], accept_draft=True, log=quiet)  # up to date with the built-in first
        (p.root / "plugins").mkdir(exist_ok=True)  # the folder the registry scans for project plug-ins
        (p.root / "plugins" / "script_swap.py").write_text(textwrap.dedent(SCRIPT_SWAP_SRC), encoding="utf-8")
        reg = R.load(p)  # reload: the project's plugins/ folder is read at load time
        self.assertEqual(reg.replaced, {"script": "speechkit-narrate"})
        steps = {s["stage"]: s for s in R.plan(p, reg, targets=["script"])}
        self.assertIn("implementation changed", steps["script"]["reasons"])
        r = R.run(p, reg, targets=["script"], log=quiet)
        self.assertIn("script", r["ran"])
        self.assertEqual(r["steps"][-1]["result"]["by"], "plug-in")  # the plug-in's code ran
        (p.root / "plugins" / "script_swap.py").unlink()  # take the plug-in away again
        steps = {s["stage"]: s for s in R.plan(p, R.load(p), targets=["script"])}
        self.assertEqual(steps["script"]["action"], "run")  # the recorded impl is the plug-in's

    # Proves partial runs and adoption: a stage that returns {"partial": True} stays due, a full run makes it current,
    # and outputs that exist without a state record are adopted rather than rebuilt.
    def test_3_partial_and_adopt(self):
        p = self.p
        plug = p.root / "plugins" / "partial_stage.py"
        plug.parent.mkdir(exist_ok=True)
        plug.write_text(textwrap.dedent('''
            from speechkit_narrate.registry import stage
            @stage("clip", needs=["script"], outputs=["out/clip.txt"], options={"part": {"type": "boolean"}})
            def clip(project, ctx):
                (project.root / "out").mkdir(exist_ok=True)
                (project.root / "out" / "clip.txt").write_text("x", encoding="utf-8")
                return {"partial": True} if ctx.opt("part") else {"full": True}
        '''), encoding="utf-8")
        try:
            reg = R.load(p)
            R.run(p, reg, targets=["clip"], options={"clip": {"part": True}}, accept_draft=True, log=quiet)
            st = {s["stage"]: s for s in R.plan(p, reg, targets=["clip"])}["clip"]
            self.assertEqual(st["action"], "run")  # a partial run leaves the stage still to do
            self.assertIn("partial", st["reasons"][0])
            R.run(p, reg, targets=["clip"], log=quiet)  # a full run without the option
            self.assertEqual({s["stage"]: s for s in R.plan(p, reg, targets=["clip"])}["clip"]["action"], "skip")
            state = R.read_state(p)  # imitate outputs made before tracking began
            state.pop("clip")
            R.write_state(p, state)
            self.assertEqual({s["stage"]: s for s in R.plan(p, reg, targets=["clip"])}["clip"]["action"], "adopt")
        finally:
            plug.unlink()  # otherwise every later load of this project would include clip


# JsonContract: what a --json command writes to stdout. The PowerShell module and the MCP server parse the last
# stdout line as the result, so nothing else may ever appear there.
class JsonContract(unittest.TestCase):
    # Proves CLI protect_stdout(): a stage that prints, and a child process it starts that prints, both end up on
    # stderr, and stdout carries exactly one line, the JSON result.
    def test_stdout_carries_only_the_result(self):
        p = make_project("noisy")
        (p.root / "plugins").mkdir()
        (p.root / "plugins" / "noisy.py").write_text(textwrap.dedent('''
            import subprocess, sys
            from speechkit_narrate.registry import stage
            @stage("noisy", outputs=["out/noisy.txt"])
            def noisy(project, ctx):
                print("stage print")
                subprocess.run([sys.executable, "-c", "print('child print')"], check=True)
                (project.root / "out").mkdir(exist_ok=True)
                (project.root / "out" / "noisy.txt").write_text("x", encoding="utf-8")
                return {"done": True}
        '''), encoding="utf-8")
        out, r = cli("run", p.root, "noisy")
        self.assertEqual(len(r.stdout.strip().splitlines()), 1, r.stdout)
        self.assertEqual(out["ran"], ["noisy"])
        self.assertIn("stage print", r.stderr)
        self.assertIn("child print", r.stderr)


# Delegation: a Linux-only stage and tool on a simulated Windows host (SPEECHKIT_PLATFORM=windows). Without
# SPEECHKIT_DELEGATE they are blocked; with it set to fake_wsl.py they run through it, and paths and results come back.
class Delegation(unittest.TestCase):
    """A Linux-only stage on a (simulated) Windows host: blocked without SPEECHKIT_DELEGATE, run through it with."""

    def setUp(self):
        self.p = make_project("delegate")
        (self.p.root / "plugins").mkdir()
        (self.p.root / "plugins" / "linux_only.py").write_text(textwrap.dedent('''
            import sys
            from speechkit_narrate.registry import current_platform, stage
            @stage("penguin", needs=[], outputs=["out/penguin.txt"], platforms=["linux"],
                   options={"word": {"type": "string", "default": "hi"}})
            def penguin(project, ctx):
                (project.root / "out").mkdir(exist_ok=True)
                (project.root / "out" / "penguin.txt").write_text(ctx.opt("word") + " from " + current_platform(),
                                                                   encoding="utf-8")
                ctx.log("penguin says " + ctx.opt("word"))
                return {"platform": current_platform()}
        '''), encoding="utf-8")
        self.log = SCRATCH / "fake_wsl.log"  # fake_wsl.py appends each forwarded command line here
        self.log.unlink(missing_ok=True)

    # Proves the stage path: blocked on "windows" without a delegate, then run through the delegate with its option,
    # its log relayed once, its result returned, and the run recorded locally so the next plan skips it.
    def test_blocked_then_delegated(self):
        env = {"SPEECHKIT_PLATFORM": "windows"}
        out, r = cli("plan", self.p.root, "penguin", env=env)
        self.assertEqual(out["steps"][0]["action"], "blocked")
        out, r = cli("run", self.p.root, "penguin", env=env, check=False)
        self.assertFalse(out["ok"])
        self.assertIn("SPEECHKIT_DELEGATE", out["error"])
        env.update(SPEECHKIT_DELEGATE=f'"{sys.executable}" "{FAKE_WSL}"', FAKE_WSL_LOG=str(self.log),
                   SPEECHKIT_DELEGATE_PYTHON="")
        out, r = cli("penguin", self.p.root, "--word", "hello", env=env)
        self.assertEqual(out["result"], {"platform": "linux"})
        self.assertEqual((self.p.root / "out" / "penguin.txt").read_text(encoding="utf-8"), "hello from linux")
        self.assertIn("penguin says hello", r.stderr)
        self.assertNotIn("] [", r.stderr)                                # one timestamp per relayed line
        cmd = self.log.read_text(encoding="utf-8")
        self.assertIn("--only penguin --force penguin", cmd)
        self.assertIn('"penguin": {"word": "hello"}', cmd)
        out, _ = cli("plan", self.p.root, "penguin", "--options", '{"penguin":{"word":"hello"}}', env=env)
        self.assertEqual(out["steps"][0]["action"], "skip")             # recorded locally after the delegated run

    # Proves the tool path: a Linux-only standalone tool is refused without a delegate and runs through it with one,
    # its path parameters sent there and its result files mapped back.
    def test_linux_only_tool_is_delegated(self):
        plug = plugin_file("linux_tool", '''
            from pathlib import Path
            from speechkit_narrate.registry import current_platform, tool
            @tool("shout", params={"file": {"type": "string", "format": "path"}, "out": {"type": "string",
                  "format": "path"}}, required=["file", "out"], platforms=["linux"])
            def shout(file, out, log=print):
                Path(out).write_text(Path(file).read_text(encoding="utf-8").upper() + current_platform(),
                                     encoding="utf-8")
                log("shouted")
                return {"files": [str(out)]}
        ''')
        src = SCRATCH / "quiet.txt"
        src.write_text("hello ", encoding="utf-8")
        env = {"SPEECHKIT_PLATFORM": "windows", "SPEECHKIT_PLUGINS": str(plug)}
        out, _ = cli("tool", "shout", "--file", src, "--out", SCRATCH / "loud.txt", env=env, check=False)
        self.assertFalse(out["ok"])
        self.assertIn("SPEECHKIT_DELEGATE", out["error"])
        env.update(SPEECHKIT_DELEGATE=f'"{sys.executable}" "{FAKE_WSL}"', FAKE_WSL_LOG=str(self.log),
                   SPEECHKIT_DELEGATE_PYTHON="")
        out, r = cli("tool", "shout", "--file", src, "--out", SCRATCH / "loud.txt", env=env)
        self.assertEqual((SCRATCH / "loud.txt").read_text(encoding="utf-8"), "HELLO linux")
        self.assertEqual(out["files"], [str(SCRATCH / "loud.txt")])
        self.assertIn("shouted", r.stderr)
        self.assertIn("tool shout --params", self.log.read_text(encoding="utf-8"))

    def test_missing_delegate_executable_preserves_stderr_in_error(self):
        missing = "/speechkit-test/no-such-python"
        env = {"SPEECHKIT_PLATFORM": "windows", "SPEECHKIT_DELEGATE": f'"{sys.executable}" "{FAKE_WSL}"',
               "SPEECHKIT_DELEGATE_PYTHON": missing}
        out, result = cli("run", self.p.root, "penguin", env=env, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exit 127", out["error"])
        self.assertIn(missing, out["error"])

    # Proves registry.remote_path: a SPEECHKIT_DELEGATE_PATHMAP rule maps a Windows path prefix to its WSL form, and
    # without a map only a command named wsl or wsl.exe is asked to translate paths.
    def test_path_mapping(self):
        os.environ["SPEECHKIT_DELEGATE_PATHMAP"] = r"C:\=/mnt/c/;D:\=/mnt/d/"
        try:
            self.assertEqual(R.remote_path(["wsl"], Path(r"C:\Users\me\Book")), "/mnt/c/Users/me/Book")
        finally:
            del os.environ["SPEECHKIT_DELEGATE_PATHMAP"]
        os.environ["FAKE_WSL_PREFIX"] = "/mnt/x"  # a wrong call to wslpath would show as this prefix
        try:
            self.assertEqual(R.remote_path([sys.executable, str(FAKE_WSL)], Path("/b")), "/b")   # not wsl: unchanged
        finally:
            del os.environ["FAKE_WSL_PREFIX"]


# Tools: the standalone tools on plain files, called through the CLI's flag parsing, including the error results and
# the quick tool that runs any stage on any file.
class Tools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = SCRATCH / "tools"
        cls.d.mkdir(parents=True, exist_ok=True)
        cls.txt = cls.d / "lantern.txt"
        cls.txt.write_text(BOOK, encoding="utf-8")

    # Proves the CLI's `tool` command: schema-generated flags work for a plug-in tool, a tool error and a missing
    # project give "ok": false with a non-zero exit, and `tools --plugin` lists a plug-in's tools.
    def test_cli_tool_and_errors(self):
        out, _ = cli("tool", "wordcount", "--file", self.txt, "--plugin", STATS)
        self.assertEqual(out["words"], len(re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*", BOOK)))
        out, r = cli("tool", "quick", "--out", self.d / "not-yet", check=False)  # a new project needs an input
        self.assertFalse(out["ok"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("input", out["error"])
        out, r = cli("plan", self.d / "not-a-project", check=False)  # a folder without project.json
        self.assertFalse(out["ok"])
        self.assertIn("project.json", out["error"])
        out, _ = cli("tools", "--plugin", STATS)  # --plugin loads the plug-in for this one call
        self.assertIn("wordcount", [t["name"] for t in out["tools"]])

    # Proves the quick tool: it creates a project from any file and runs the planner for the requested targets,
    # accepting the draft by default; run again it reuses the project and runs nothing.
    def test_quick_any_stage_on_any_file(self):
        out, _ = cli("tool", "quick", "--input", self.txt, "--out", self.d / "quick", "--targets", "script",
                     "--title", "The Lantern Keeper")
        self.assertEqual(out["ran"], ["extract", "check", "script"])  # the review gate did not stop it
        self.assertTrue((self.d / "quick" / "work" / "request.json").exists())
        out, _ = cli("tool", "quick", "--out", self.d / "quick", "--targets", "script")
        self.assertEqual(out["ran"], [])


# Portability: guards for running the same code on Windows. Text I/O must name its encoding, and Linux-only pieces
# must report themselves.
class Portability(unittest.TestCase):
    # Proves every read_text/write_text call, and every subprocess run/Popen call with text=True, in speechkit_narrate/
    # and emblem_rsi/ passes encoding=. Python on Windows otherwise uses the ANSI code page and garbles non-ASCII text.
    def test_every_text_io_names_its_encoding(self):
        """Windows defaults to cp1252: every read_text/write_text and text-mode subprocess call must say UTF-8."""
        bad, seen = [], 0
        for root in ("speechkit_narrate", "emblem_rsi"):  # the toolkit and the audit loop it embeds
            for f in (REPO / root).rglob("*.py"):
                seen += 1
                for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                    if not isinstance(node, ast.Call):
                        continue
                    fn = node.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                    kws = {k.arg for k in node.keywords}
                    text_mode = any(k.arg == "text" and getattr(k.value, "value", None) is True for k in node.keywords)
                    if (name in ("read_text", "write_text") or (name in ("run", "Popen") and text_mode)) \
                            and "encoding" not in kws:
                        bad.append(f"{f.relative_to(REPO)}:{node.lineno} {name}")
        self.assertGreater(seen, 20)  # the scan really found the packages
        self.assertEqual(bad, [])

    # Proves the voice-runtime helpers report Windows correctly: the runtime's python path uses the Windows venv layout
    # on Windows, and with the platform forced to windows, voice_here() is False and doctor shows a Linux-only row.
    def test_windows_runtime_paths(self):
        from speechkit_narrate import env
        want = (".venv", "Scripts", "python.exe") if env.WINDOWS else (".venv", "bin", "python")
        self.assertEqual(env.runtime_python().parts[-3:], want)
        os.environ["SPEECHKIT_PLATFORM"] = "windows"
        try:
            self.assertFalse(env.voice_here())
            rows = {n: (ok, d) for n, ok, d in env.checks() if n.startswith("voice runtime")}
            self.assertIn("voice runtime (Linux-only)", rows)
        finally:
            del os.environ["SPEECHKIT_PLATFORM"]


if __name__ == "__main__":
    unittest.main()
