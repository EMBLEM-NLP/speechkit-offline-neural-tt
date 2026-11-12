"""MCP server (speechkit_narrate/mcp_server.py), driven through the MCP SDK's own stdio client.

    python3 -m unittest tests.narration.test_mcp        (from the repository root)

Each test class starts one `python -m speechkit_narrate mcp` process (cwd = the repository, PYTHONPATH set, a
throwaway SPEECHKIT_CACHE) and talks to it with mcp.ClientSession through an anyio blocking portal, so the
tests stay plain unittest. Needs the MCP Python SDK >= 2.3 (the tests are skipped without it). Background jobs of a
chosen length come from the "nap" tool of the test plug-in tests/narration/fixtures/nap_plugin.py.
"""
from __future__ import annotations  # annotation hints such as "dict | None" stay unevaluated strings

# Standard-library imports; the MCP SDK and the toolkit are imported further down inside a try block.
import contextlib  # asynccontextmanager for the connect helper
import functools  # partial binds arguments for the blocking portal's call()
import json  # compares a tool result's text with its structured content
import os  # environment for the server process; os.name picks the liveness check
import re  # reads the State: line of /proc/<pid>/status
import shutil  # rmtree removes each class's temporary folder
import subprocess  # tasklist on Windows in alive()
import sys  # sys.executable starts the server with the same interpreter
import tempfile  # mkdtemp / TemporaryDirectory give each server its own folder
import time  # deadlines and polling intervals for background jobs
import unittest  # the standard test framework: TestCase classes, assertions and skip decorators
from pathlib import Path  # paths for fixtures, project folders and log files

# The repository root (this file is tests/narration/test_mcp.py); it becomes the server's working folder and PYTHONPATH.
REPO = Path(__file__).resolve().parents[2]  # resolve() makes it absolute whatever the current folder is
sys.path.insert(0, str(REPO))  # import this checkout's speechkit-voice-tts-offline-pipeline package, not an installed copy

# The MCP SDK is optional for the toolkit. Without it (or with an SDK older than 2.3) every server test is
# skipped instead of failing at import.
try:  # a missing package raises ImportError at one of these lines
    # runs an anyio event loop in a helper thread so plain test code can call async functions
    from anyio.from_thread import start_blocking_portal
    from mcp import ClientSession  # the client side of one MCP connection
    # starts the server as a child process and talks JSON-RPC over its stdin/stdout
    from mcp.client.stdio import StdioServerParameters, stdio_client
    # raised by the client when the server answers with a protocol-level error
    from mcp.shared.exceptions import MCPError
    # the protocol versions the SDK speaks: the initialize handshake ones and the newer per-request ones
    from mcp.types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

    from speechkit_narrate import mcp_server  # the server module, used here only for its SDK check
    # _sdk_problem() returns None when the installed SDK has the 2.x server API the server needs
    HAVE_MCP = mcp_server._sdk_problem() is None
except ImportError:  # the SDK or anyio is not installed
    HAVE_MCP = False  # the skipUnless decorators below read this flag

# The test plug-in with a stage and a tool, given relative so the test proves SPEECHKIT_PLUGINS paths resolve against
# the server's cwd.
STATS_PLUGIN = "tests/narration/fixtures/reading_stats.py"          # relative: resolved against the server's cwd
# The test plug-in whose "nap" tool waits as long as it is told, then writes a file: a job of a chosen length.
NAP_PLUGIN = REPO / "tests" / "narration" / "fixtures" / "nap_plugin.py"
# A three-chapter text in the toolkit's markup (title line, "# n. ..." headings, paragraphs), long enough for the
# stats plug-in to count words, short enough for extract, check and script to finish in a second or two.
BOOK = """The Small Lantern

# 1. The Harbour

The harbour wakes before the town does. Gulls argue over the first catch, and the ropes creak as the tide turns.

Three boats leave at dawn, painted blue and green, and the old keeper waves from the lighthouse steps.

# 2. The Market

By noon the boats return, and the counting begins on long wooden tables under the striped awnings.

Children run between the stalls while the bell in the square rings twelve times.

# 3. The Evening

When the lamps are lit along the quay, the keeper climbs the stairs again and trims the small lantern.
"""
# Source template of a plug-in tool written to disk by PluginServer. str.format fills {name} (the tool's name and
# the greeting word); the doubled braces become literal braces in the written file. The tool requires one string
# parameter "name" and returns {"files": [], "greeting": "<name> <given name>"}.
# A project plug-in (written to <project>/plugins) whose stage "slow" takes a few seconds, so a second job on the same
# project is certain to find the project busy.
SLOW_STAGE = '''import time
from speechkit_narrate.registry import stage


@stage("slow", outputs=["out/slow.txt"])
def slow(project, ctx):
    time.sleep(3)
    (project.root / "out").mkdir(exist_ok=True)
    (project.root / "out" / "slow.txt").write_text("x", encoding="utf-8")
    return {"slept": 3}
'''
LIVE_PLUGIN = '''from speechkit_narrate.registry import tool


@tool("{name}", params={{"name": {{"type": "string"}}}}, required=["name"], description="Say hello")
def hello(name, log=print):
    return {{"files": [], "greeting": "{name} " + name}}
'''


# True while the process with this pid is still running (an exited but unreaped zombie counts as gone).
# pid: an operating-system process id. Used by the Shutdown test to see whether a job outlived the server.
# Starts tasklist on Windows; reads /proc on Linux; otherwise probes with signal 0.
def alive(pid: int) -> bool:
    if os.name == "nt":  # Windows has no /proc and no signal 0 probe
        # list only the task with this PID, without the header line
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False,
                           encoding="utf-8", errors="replace")
        return str(pid) in r.stdout  # the PID appears in the output only if the task exists
    status = Path(f"/proc/{pid}/status")  # where Linux keeps the process's state
    if Path("/proc/self/status").exists():  # /proc exists for this process, so it exists for others too
        try:  # reading fails once the process has been reaped
            # A zombie ("State: Z") has finished and only waits for its parent to collect its exit status.
            return not re.search(r"^State:\s+Z", status.read_text(encoding="utf-8"), re.MULTILINE)      # a zombie has exited
        except OSError:  # no status file: the process is gone
            return False  # alive() answers False: the job process no longer exists
    try:  # fallback for other Unix systems (macOS)
        os.kill(pid, 0)  # signal 0 checks existence and permission without sending anything
        return True  # kill(pid, 0) succeeded, so a process with that id exists
    except ProcessLookupError:  # the kernel reports no process with that id
        return False  # the pid is free: the process ended
    except PermissionError:  # it exists but belongs to another user
        return True  # a permission error still proves the process exists


# Async context manager that starts the server, opens a client session on its stdio and performs the opening
# exchange. params: StdioServerParameters (command, args, cwd, env); errlog: open text file for the server's
# stderr; modern: True for server/discover (2026-07-28 protocol), False for the initialize handshake.
# Yields (session, info) where info is the server's initialize or discover result.
@contextlib.asynccontextmanager  # turns the generator into an async context manager
async def _connect(params, errlog, modern: bool):  # runs inside the portal's event loop thread
    # both contexts close in reverse order when the caller exits, which stops the server
    async with stdio_client(params, errlog=errlog) as (read, write), ClientSession(read, write) as session:
        # discover() or initialize(): either way the server's name, version, capabilities and instructions come back
        info = await (session.discover() if modern else session.initialize())
        yield session, info  # hand both to the caller until it exits the context


# One running `python -m speechkit_narrate mcp` server with a connected client session, wrapped so ordinary synchronous
# unittest code can call it. Each instance owns a server process, a blocking portal (an event loop in a helper
# thread) and a stderr log file; close() releases all three.
class McpServer:
    """One `python -m speechkit_narrate mcp` process with a connected ClientSession, usable from plain threads."""

    # Starts the server and connects. tmp: a private folder for the server's cache and stderr log; plugins:
    # SPEECHKIT_PLUGINS entries for the server (paths or module names); modern: use server/discover instead of
    # initialize. Side effects: starts a process, opens tmp/server-stderr.log.
    def __init__(self, tmp: Path, plugins=(), modern: bool = False):
        # drop any plug-ins from the developer's own environment so tool lists are predictable
        env = {k: v for k, v in os.environ.items() if k != "SPEECHKIT_PLUGINS"}
        # the checkout first, then whatever PYTHONPATH already held (filter drops an unset one)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), os.environ.get("PYTHONPATH")]))
        env["SPEECHKIT_CACHE"] = str(tmp / "cache")  # job logs go to a throwaway cache instead of the real one
        if plugins:  # only when the test class asks for plug-ins
            # joined with os.pathsep, the separator the registry splits on
            env["SPEECHKIT_PLUGINS"] = os.pathsep.join(map(str, plugins))
        self.cache = tmp / "cache"  # kept so tests can check where job logs land
        # The server command: this interpreter running `-m speechkit_narrate mcp` with cwd at the repository root, so relative
        # plug-in paths and the toolkit's own files resolve there.
        # what stdio_client will start
        self.params = StdioServerParameters(command=sys.executable, args=["-m", "speechkit_narrate", "mcp"], cwd=str(REPO),
                                            # the prepared environment: PYTHONPATH, a throwaway cache and the plug-in
                                            # list
                                            env=env)
        # The server's stderr (its log lines) goes to a file in tmp, readable after a failure.
        self.errlog = open(tmp / "server-stderr.log", "w", encoding="utf-8")      # noqa: SIM115 - closed in close()
        self._portal_cm = start_blocking_portal()  # creates the portal; the event loop thread starts on __enter__
        self.portal = self._portal_cm.__enter__()  # start the loop thread and keep the portal for calls
        try:  # undo the portal and the log file if connecting fails
            # a synchronous wrapper around the async _connect context
            self._session_cm = self.portal.wrap_async_context_manager(_connect(self.params, self.errlog, modern))
            # starts the server, handshakes, and keeps the session and the server's info
            self.session, self.info = self._session_cm.__enter__()
        except BaseException:  # also catches KeyboardInterrupt during startup
            self._portal_cm.__exit__(None, None, None)  # stop the event loop thread
            self.errlog.close()  # close the stderr log file
            raise  # report the original failure

    # Calls one MCP tool and blocks until the result arrives. name: tool name; args: its arguments (None means {});
    # timeout: seconds the client waits for the response before giving up. Returns the SDK's CallToolResult.
    def call(self, name: str, args: dict | None = None, timeout: float = 120):
        # portal.call runs the coroutine in the loop thread and returns its value here
        return self.portal.call(functools.partial(self.session.call_tool, name, args or {},
                                                  # read_timeout_seconds bounds this one request
                                                  read_timeout_seconds=timeout))

    # Lists the server's tools (tools/list); the server rebuilds the list from the registry on every call.
    def list_tools(self):
        # the result's tools list, each with name, schema, description and annotations
        return self.portal.call(self.session.list_tools).tools

    # Shuts the session (which stops the server process), then the portal, then the log file, even if a step fails.
    def close(self):
        try:  # the portal and the log are released in finally
            # leaving the session context closes stdin; the server exits and stops its jobs
            self._session_cm.__exit__(None, None, None)
        finally:  # runs even if closing the session raised, so the loop thread and the log never leak
            self._portal_cm.__exit__(None, None, None)  # then the portal's loop thread
            self.errlog.close()  # release the stderr log


# Base class for the server tests: one server per test class, started in setUpClass and stopped in tearDownClass.
# Subclasses set plugins (SPEECHKIT_PLUGINS for the server) and modern (protocol style), and may override
# prepare() to write files before the server starts. The skip decorator covers every subclass.
@unittest.skipUnless(HAVE_MCP, "MCP Python SDK >= 2.3 not installed")  # skip when the SDK is missing or too old
class _ServerCase(unittest.TestCase):  # has no test methods itself, so unittest runs nothing for it directly
    plugins: tuple = ()  # plug-ins handed to the server; none by default
    modern = False  # False: the initialize handshake
    server: McpServer  # type hint for the McpServer instance set in setUpClass

    @classmethod  # setUpClass receives the class, so all tests of a class share one server
    # Makes the class's temporary folder, lets the subclass prepare it, then starts the server.
    def setUpClass(cls):
        # a unique folder per class under the system temp directory
        cls.tmp = Path(tempfile.mkdtemp(prefix="speechkit-voice-tts-offline-pipeline-mcp-"))
        cls.prepare(cls.tmp)  # hook: PluginServer writes its live plug-in here
        cls.server = McpServer(cls.tmp, cls.plugins, cls.modern)  # the subclass's plugins and protocol choice apply

    @classmethod  # hook declared on the class
    # Hook for subclasses: write files or set class attributes in tmp before the server starts. Does nothing here.
    def prepare(cls, tmp: Path):
        pass  # no preparation by default

    @classmethod  # tearDownClass runs once, after the last test of the class
    # Stops the server and removes the temporary folder (projects, PDFs, cache, logs) even if stopping fails.
    def tearDownClass(cls):
        try:  # folder removal runs in finally
            cls.server.close()  # ends the session and the server
        finally:  # the temporary folder is removed even if closing the server raised
            # ignore_errors: a file still held open must not fail the teardown
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- helpers
    # Calls a tool and checks the shape of its result: is_error must match error (True for an expected failure),
    # structured_content must be a dict, and for a success the text content must be the same JSON. Returns the
    # structured content. The failure message shows the start of the text so a broken call explains itself.
    def call(self, name: str, args: dict | None = None, error: bool = False) -> dict:
        r = self.server.call(name, args)  # blocking call through the portal
        text = r.content[0].text if r.content else ""  # the first content item holds the human-readable text
        # an unexpected error, or an unexpected success, fails here with the text
        self.assertEqual(r.is_error, error, f"{name}: {text[:3000]}")
        self.assertIsInstance(r.structured_content, dict)  # every result, ok or error, carries a JSON object
        if not error:  # success: text and structured content must agree
            self.assertEqual(json.loads(text), r.structured_content)        # the text is the same JSON
        return r.structured_content  # tests inspect the dict

    # Polls job_status every 0.3 s until the job is no longer running, failing the test after timeout seconds.
    # job_id: the id a tool call returned. Returns the final CallToolResult, so callers can also check is_error.
    def wait_job(self, job_id: str, timeout: float = 90):
        end = time.time() + timeout  # the moment, in epoch seconds, after which waiting counts as a failure
        while True:  # poll until done or out of time
            # ask for the last 20 log lines with each poll
            r = self.server.call("job_status", {"job_id": job_id, "log_lines": 20})
            if r.structured_content["state"] != "running":  # done, failed or cancelled
                return r  # return the whole result, not only the dict
            if time.time() > end:  # the job is still running and the deadline has passed
                # fail with the last status for diagnosis
                self.fail(f"job {job_id} still running after {timeout} s: {r.structured_content}")
            time.sleep(0.3)  # short sleep so polling does not hammer the server

    # Makes a project through the project_new tool and returns its folder.
    # name: folder name under tmp; the book text goes to tmp/<name>.txt first. Asserts the reported folder is the one
    # asked for (resolved, since the server resolves paths).
    def new_project(self, name: str) -> Path:
        src = self.tmp / f"{name}.txt"  # the source book file
        src.write_text(BOOK, encoding="utf-8")  # UTF-8 explicitly, as the toolkit reads it
        # project_new with the book file; the title and author below go into project.json
        d = self.call("project_new", {"project": str(self.tmp / name), "input": str(src),
                                      "title": "The Small Lantern", "author": "Test Author"})
        # the server answers with the resolved absolute folder
        self.assertEqual(Path(d["project"]), (self.tmp / name).resolve())
        return Path(d["project"])  # the project folder as a Path




# ServerBasics: one server with only the nap test plug-in. Covers the handshake, the generated tool catalogue,
# describe, a standalone tool as a finished job and as a background job, cancelling, a full project round trip and
# the error results.
class ServerBasics(_ServerCase):
    plugins = (str(NAP_PLUGIN),)  # the nap tool gives jobs of a chosen length

    # Proves the initialize handshake: a supported protocol version, the server's name, the tools capability, and
    # instructions that tell a client where to start and how jobs work.
    def test_initialize(self):
        info = self.server.info
        self.assertIn(info.protocol_version, HANDSHAKE_PROTOCOL_VERSIONS)
        self.assertEqual(info.server_info.name, "speechkit-voice-tts-offline-pipeline")
        self.assertIsNotNone(info.capabilities.tools)
        self.assertIn("narrate_describe", info.instructions)
        self.assertIn("job_status", info.instructions)

    # Proves the tool catalogue built from the registry: the fixed tools, a stage_ tool per stage and a tool_ tool per
    # standalone tool, valid names, object schemas, annotations, and per-tool schema details clients depend on.
    def test_tool_list(self):
        tools = {t.name: t for t in self.server.list_tools()}
        stages = ("extract", "check", "script", "prevoice", "narrate", "autofix", "audit", "report", "export")
        for n in ("narrate_describe", "project_new", "project_status", "project_settings", "project_plan",
                  "project_run", "tool_speak", "tool_quick", "tool_nap", "job_status", "job_list", "job_cancel",
                  *(f"stage_{s}" for s in stages)):
            self.assertIn(n, tools)
        # exactly the nine stages and the built-in tools plus the nap plug-in: nothing removed comes back
        self.assertEqual(sorted(n for n in tools if n.startswith("stage_")), sorted(f"stage_{s}" for s in stages))
        self.assertEqual(sorted(n for n in tools if n.startswith("tool_")), ["tool_nap", "tool_quick", "tool_speak"])
        self.assertNotIn("stage_stats", tools)                         # the stats plug-in is not loaded here
        for n, t in tools.items():
            self.assertRegex(n, r"^[A-Za-z0-9_-]{1,64}$")  # MCP tool names
            self.assertEqual(t.input_schema["type"], "object")
            self.assertTrue(t.description)
        for n in ("narrate_describe", "project_status", "project_plan", "job_status", "job_list"):
            self.assertTrue(tools[n].annotations.read_only_hint, n)
        for n in ("project_run", "stage_script", "tool_quick", "job_cancel"):
            self.assertFalse(tools[n].annotations.read_only_hint, n)
        new = tools["project_new"].input_schema  # project_new takes settings (no music or cover any more)
        self.assertEqual(sorted(new["required"]), ["input", "project"])
        self.assertEqual(new["properties"]["settings"]["type"], "object")
        self.assertFalse({"music", "cover"} & set(new["properties"]))
        sp = tools["stage_script"].input_schema
        self.assertEqual(sp["required"], ["project"])
        self.assertIn("accept_draft", sp["properties"])
        self.assertEqual(sp["properties"]["wait_seconds"]["default"], 30)
        # the export stage's own options become top-level properties of stage_export
        self.assertEqual(tools["stage_export"].input_schema["properties"]["format"]["enum"], ["wav", "mp3", "m4a"])
        self.assertIn("workers", tools["stage_narrate"].input_schema["properties"])
        self.assertEqual(tools["stage_autofix"].input_schema["properties"]["takes"]["default"], 3)
        self.assertEqual(tools["project_run"].input_schema["properties"]["wait_seconds"]["default"], 20)
        # tool_speak's schema is the tool's own schema with wait_seconds added
        spk = tools["tool_speak"].input_schema
        self.assertEqual(spk["required"], ["out"])
        self.assertEqual(spk["properties"]["text_file"]["format"], "path")
        self.assertIn("wait_seconds", spk["properties"])
        self.assertEqual(tools["tool_quick"].input_schema["properties"]["out"]["format"], "path")

    # Proves narrate_describe returns the registry: stages in dependency order, the tools, this platform and the
    # delegate key (None unless SPEECHKIT_DELEGATE is set).
    def test_describe(self):
        d = self.call("narrate_describe")
        names = [s["name"] for s in d["stages"]]
        self.assertLess(names.index("script"), names.index("narrate"))
        self.assertLess(names.index("narrate"), names.index("export"))
        self.assertEqual(sorted(t["name"] for t in d["tools"]), ["nap", "quick", "speak"])
        self.assertIn(d["platform"], ("linux", "windows", "macos"))
        self.assertIn("delegate", d)

    # Proves a standalone tool run that finishes within wait_seconds: the result comes back inline, every listed file
    # exists, and the job log was written under the server's SPEECHKIT_CACHE.
    def test_tool_finishes_within_the_call(self):
        d = self.call("tool_nap", {"out": str(self.tmp / "nap" / "done.txt"), "wait_seconds": 60})
        self.assertEqual(d["state"], "done", d)
        res = d["result"]
        self.assertEqual(res["tool"], "nap")  # the CLI adds the tool name to the result
        self.assertEqual(res["files"], [str(self.tmp / "nap" / "done.txt")])
        for f in res["files"]:
            self.assertTrue(Path(f).is_file(), f)
        self.assertTrue(Path(d["log_file"]).is_file())  # each job writes a log file
        self.assertTrue(Path(d["log_file"]).resolve().is_relative_to((self.server.cache / "mcp-jobs").resolve()))

    # Proves the quick tool through MCP: a project made from a text file and brought up to date to the script stage.
    def test_tool_quick(self):
        src = self.tmp / "quick.txt"
        src.write_text(BOOK, encoding="utf-8")
        d = self.call("tool_quick", {"input": str(src), "out": str(self.tmp / "quick"), "targets": ["script"],
                                     "wait_seconds": 60})
        self.assertEqual(d["state"], "done", d)
        self.assertEqual(d["result"]["ran"], ["extract", "check", "script"])
        self.assertTrue((self.tmp / "quick" / "work" / "request.json").is_file())

    # Proves the background path: with wait_seconds 0 the call returns at once with a job id, job_list shows the job,
    # and polling job_status ends with a done job whose result, log and timing fields are filled in.
    def test_background_job(self):
        d = self.call("tool_nap", {"seconds": 1, "out": str(self.tmp / "bg" / "B.txt"), "wait_seconds": 0})
        self.assertEqual(d["state"], "running")
        self.assertIsNone(d["result"])
        jid = d["job_id"]
        listed = self.call("job_list")
        self.assertIn(jid, [j["job_id"] for j in listed["jobs"]])
        r = self.wait_job(jid)
        st = r.structured_content
        self.assertFalse(r.is_error, st)
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["exit_code"], 0)
        self.assertTrue(all(Path(f).is_file() for f in st["result"]["files"]))
        self.assertIsInstance(st["log"], list)
        self.assertGreaterEqual(st["seconds"], 0)
        self.assertTrue(st["started"])

    # Proves job_cancel: a running job is stopped and reported as cancelled, a second cancel reports that nothing was
    # stopped, and job_status keeps showing the cancelled state.
    def test_cancel(self):
        d = self.call("tool_nap", {"seconds": 60, "out": str(self.tmp / "cancel.txt"), "wait_seconds": 0})
        self.assertEqual(d["state"], "running")
        c = self.call("job_cancel", {"job_id": d["job_id"]})
        self.assertTrue(c["cancelled"])
        self.assertEqual(c["state"], "cancelled")
        again = self.call("job_cancel", {"job_id": d["job_id"]})
        self.assertFalse(again["cancelled"])
        self.assertEqual(self.call("job_status", {"job_id": d["job_id"]})["state"], "cancelled")
        self.assertFalse((self.tmp / "cancel.txt").exists())  # stopped before it could finish

    # Proves the project round trip: project_new (with settings), project_settings, project_run to script,
    # project_plan showing it up to date, project_status, a settings change making script due, a stage_script job
    # with the reshaped result, and the busy guard that refuses a second job on the same project.
    def test_project_new_run_plan(self):
        src = self.tmp / "lantern.txt"
        src.write_text(BOOK, encoding="utf-8")
        d = self.call("project_new", {"project": str(self.tmp / "lantern"), "input": str(src),
                                      "title": "The Small Lantern", "settings": {"spoken": {"years": True}}})
        proj = Path(d["project"])
        st = self.call("project_settings", {"project": str(proj)})
        self.assertEqual(st["settings"]["title"], "The Small Lantern")
        self.assertIs(st["settings"]["spoken"]["years"], True)  # the settings given to project_new were saved
        d = self.call("project_run", {"project": str(proj), "targets": ["script"], "accept_draft": True,
                                      "wait_seconds": 60})
        self.assertEqual(d["state"], "done", d)
        res = d["result"]
        self.assertEqual(res["ran"], ["extract", "check", "script"])
        self.assertIsNone(res["stopped"])
        self.assertTrue((proj / "work" / "docmodel.json").is_file())
        plan = self.call("project_plan", {"project": str(proj), "targets": ["script"]})
        self.assertEqual({s["stage"]: s for s in plan["steps"]}["script"]["action"], "skip")
        status = {s["stage"]: s for s in self.call("project_status", {"project": str(proj)})["stages"]}
        self.assertTrue(status["script"]["done"])
        self.assertFalse(status["narrate"]["done"])
        st = self.call("project_settings", {"project": str(proj), "set": {"spoken": {"ordinal_days": True}}})
        self.assertIs(st["settings"]["spoken"]["ordinal_days"], True)
        plan = self.call("project_plan", {"project": str(proj), "targets": ["script"]})
        self.assertEqual({s["stage"]: s for s in plan["steps"]}["script"]["action"], "run")
        d = self.call("stage_script", {"project": str(proj), "wait_seconds": 60})
        self.assertEqual(d["state"], "done", d)
        self.assertEqual(d["result"]["stage"], "script")  # stage jobs return a reshaped result (_stage_post)
        self.assertEqual(d["result"]["action"], "ran")
        self.assertGreater(d["result"]["result"]["segments"], 0)  # the script stage's own counts
        self.assertTrue(any("script" in ln for ln in d["log"]))
        # the busy guard: a slow project plug-in stage as a background job, then a second job on the same project
        (proj / "plugins").mkdir()
        (proj / "plugins" / "slow.py").write_text(SLOW_STAGE, encoding="utf-8")
        d = self.call("project_run", {"project": str(proj), "targets": ["slow"], "wait_seconds": 0})
        self.assertEqual(d["state"], "running")
        busy = self.call("stage_script", {"project": str(proj)}, error=True)
        self.assertIn("busy", busy["error"])
        r = self.wait_job(d["job_id"])
        self.assertEqual(r.structured_content["state"], "done", r.structured_content)
        self.assertIn("slow", r.structured_content["result"]["ran"])

    # Proves the error paths: missing and non-project folders, unknown arguments, a missing or unsupported input file,
    # bad settings, a flag passed as a stage name, an unknown stage in a background job, an unknown job id, and an
    # unknown tool name.
    def test_errors(self):
        d = self.call("project_status", {"project": str(self.tmp / "no-such-project")}, error=True)
        self.assertIn("not a speechkit-voice-tts-offline-pipeline project", d["error"])
        self.call("project_plan", {"project": str(self.tmp)}, error=True)          # a folder, but no project.json
        self.call("stage_script", {"project": str(self.tmp), "no_such_option": 1}, error=True)
        d = self.call("project_new", {"project": str(self.tmp / "x")}, error=True)      # input missing
        self.assertIn("input", d["error"])
        d = self.call("project_new", {"project": str(self.tmp / "y"), "input": str(self.tmp / "missing.txt")},
                      error=True)
        self.assertIn("does not exist", d["error"])
        pdf = self.tmp / "book.pdf"  # PDF input was removed with the print and video toolkits
        pdf.write_bytes(b"%PDF-1.4\n")
        d = self.call("project_new", {"project": str(self.tmp / "z"), "input": str(pdf)}, error=True)
        self.assertIn("unsupported input type", d["error"])
        self.assertFalse((self.tmp / "z").exists())  # refused before the folder was made
        src = self.tmp / "errs.txt"
        src.write_text(BOOK, encoding="utf-8")
        d = self.call("project_new", {"project": str(self.tmp / "w"), "input": str(src), "settings": {"spoken": 5}},
                      error=True)  # a setting that would break the project
        self.assertIn("spoken", d["error"])
        self.assertFalse((self.tmp / "w" / "project.json").exists())
        proj = self.new_project("errs")
        d = self.call("project_plan", {"project": str(proj), "targets": ["--dry-run"]}, error=True)
        self.assertIn("not a stage name", d["error"])
        d = self.call("project_run", {"project": str(proj), "targets": ["no_such_stage"], "wait_seconds": 30},
                      error=True)
        self.assertEqual(d["state"], "failed")
        self.assertIn("unknown stage", d["error"])
        self.call("job_status", {"job_id": "nope-000000"}, error=True)
        with self.assertRaises(MCPError):  # an unknown tool is a protocol error, raised by the client
            self.server.call("no_such_tool", {})


# PluginServer: a server started with SPEECHKIT_PLUGINS holding the stats test plug-in (relative path) and a live
# plug-in file in tmp. Covers plug-in tools and stages in the catalogue and in runs, and edits made while the
# server keeps running.
class PluginServer(_ServerCase):
    """SPEECHKIT_PLUGINS honoured: the stats plug-in (relative path) and a plug-in edited while running."""

    @classmethod
    def prepare(cls, tmp):
        cls.live = tmp / "live_plugin.py"
        cls.live.write_text(LIVE_PLUGIN.format(name="hello"), encoding="utf-8")
        cls.plugins = (STATS_PLUGIN, str(cls.live))  # relative plug-in first, then the absolute live file

    # Proves both plug-ins show up as MCP tools next to the built-ins, the stage's option schema keeps its default,
    # the description names the plug-in file, and describe lists the loaded plug-in.
    def test_plugin_tools_listed(self):
        tools = {t.name: t for t in self.server.list_tools()}
        for n in ("stage_stats", "tool_wordcount", "tool_hello", "stage_script", "tool_quick"):
            self.assertIn(n, tools)
        self.assertEqual(tools["stage_stats"].input_schema["properties"]["wpm"]["default"], 160)
        self.assertIn("reading_stats.py", tools["stage_stats"].description)
        d = self.call("narrate_describe")
        self.assertTrue(any(p["plugin"].endswith("reading_stats.py") for p in d["plugins"]))

    # Proves a plug-in tool runs as a job and returns its own result fields.
    def test_wordcount(self):
        f = self.tmp / "words.txt"
        f.write_text("One two three, four. Don't stop\n", encoding="utf-8")
        d = self.call("tool_wordcount", {"file": str(f)})
        self.assertEqual(d["state"], "done")
        self.assertEqual(d["result"]["words"], 6)  # the plug-in's regex keeps "Don't" as one word

    # Proves a plug-in stage runs in a project, and that a stage option given to stage_stats reaches the stage code.
    def test_plugin_stage(self):
        proj = self.new_project("stats")
        d = self.call("project_run", {"project": str(proj), "targets": ["stats"], "accept_draft": True,
                                      "wait_seconds": 60})
        self.assertEqual(d["state"], "done", d)
        self.assertIn("stats", d["result"]["ran"])
        stats = json.loads((proj / "out" / "stats.json").read_text(encoding="utf-8"))
        d = self.call("stage_stats", {"project": str(proj), "wpm": 50, "wait_seconds": 60})
        self.assertEqual(d["state"], "done", d)
        self.assertEqual(d["result"]["result"]["words"], stats["words"])
        again = json.loads((proj / "out" / "stats.json").read_text(encoding="utf-8"))
        self.assertEqual(again["reading_minutes"], round(stats["words"] / 50, 1))   # the option reached the stage

    # Proves plug-in changes reach a running server: an edited plug-in replaces its tool on the next tools/list and can
    # be called, and a plug-in with a syntax error leaves only the fixed tools while describe reports the error.
    # The finally block restores the original plug-in for the other tests of this class.
    def test_plugin_edit_without_restart(self):
        self.assertIn("tool_hello", [t.name for t in self.server.list_tools()])
        self.live.write_text(LIVE_PLUGIN.format(name="greetings"), encoding="utf-8")
        try:
            names = [t.name for t in self.server.list_tools()]
            self.assertIn("tool_greetings", names)
            self.assertNotIn("tool_hello", names)
            d = self.call("tool_greetings", {"name": "reader"})
            self.assertEqual(d["result"]["greeting"], "greetings reader")
            self.live.write_text("def broken(:\n", encoding="utf-8")
            names = [t.name for t in self.server.list_tools()]
            self.assertIn("narrate_describe", names)
            self.assertIn("job_status", names)
            self.assertNotIn("stage_script", names)  # stage tools need the registry, so they are gone
            d = self.call("narrate_describe", error=True)
            self.assertIn("SyntaxError", d["error"])
        finally:
            self.live.write_text(LIVE_PLUGIN.format(name="hello"), encoding="utf-8")


# ModernProtocol: the same server reached with the per-request protocol (server/discover), checking that the
# catalogue and calls work without the initialize handshake.
class ModernProtocol(_ServerCase):
    """The 2026-07-28 per-request protocol (server/discover instead of the initialize handshake)."""
    modern = True

    def test_discover_list_call(self):
        info = self.server.info
        self.assertIn(MODERN_PROTOCOL_VERSIONS[-1], info.supported_versions)
        self.assertIn("job_status", info.instructions)
        names = [t.name for t in self.server.list_tools()]
        self.assertIn("project_run", names)
        self.assertIn("stage_export", names)
        d = self.call("narrate_describe")
        self.assertIn("export", [s["name"] for s in d["stages"]])
        d = self.call("project_status", {"project": str(self.tmp / "missing")}, error=True)
        self.assertIn("not a speechkit-voice-tts-offline-pipeline project", d["error"])


# Shutdown: jobs must not outlive the server. A separate test class because it stops its own server mid-test.
@unittest.skipUnless(HAVE_MCP, "MCP Python SDK >= 2.3 not installed")
class Shutdown(unittest.TestCase):
    # Proves that closing the server stops a job it started: a running nap job's process is gone within 10 s.
    def test_jobs_stop_with_the_server(self):
        with tempfile.TemporaryDirectory(prefix="speechkit-voice-tts-offline-pipeline-mcp-") as td:
            tmp = Path(td)
            server = McpServer(tmp, plugins=(str(NAP_PLUGIN),))
            try:
                r = server.call("tool_nap", {"seconds": 60, "out": str(tmp / "out.txt"), "wait_seconds": 0})
                d = r.structured_content
                self.assertEqual(d["state"], "running")
                pid = d["pid"]
                self.assertTrue(alive(pid))
            finally:
                server.close()  # closing stdin ends the server, which stops its jobs
            end = time.time() + 10
            while alive(pid) and time.time() < end:
                time.sleep(0.2)
            self.assertFalse(alive(pid), "the job outlived the server")


if __name__ == "__main__":
    unittest.main()
