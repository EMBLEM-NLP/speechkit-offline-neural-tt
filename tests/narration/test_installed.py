"""A pip-installed copy of speechkit-narrate sends no toolkit folder to other Pythons: delegation, the audit, MCP children."""
from __future__ import annotations  # annotations stay strings, so hints cost nothing at import

# Standard-library imports only; the toolkit itself is imported below, after its folder is on sys.path.
import os  # os.environ is patched around every test, and os.pathsep joins PYTHONPATH entries
import sys  # sys.path is extended below and restored by the audit test
import tempfile  # a scratch folder per test stands in for a site-packages or toolkit folder
import types  # SimpleNamespace stands in for the project, stage and context objects of a delegated stage
import unittest  # plain unittest: python3 -m unittest tests.narration.test_installed
from pathlib import Path  # object paths for the scratch folders
from unittest import mock  # patch.object / patch.dict swap env.REPO, os.environ and the process runner

# The repository root: this file is tests/narration/test_installed.py, so parents[2] is the folder holding the
# speechkit_narrate package.
REPO = Path(__file__).resolve().parents[2]  # resolve() first so a relative __file__ still gives an absolute root
sys.path.insert(0, str(REPO))  # put the checkout first on the import path, ahead of any installed speechkit-narrate

# env decides "installed or not", registry builds the delegated command, mcp_server and audit act on the answer.
from speechkit_narrate import audit, env, mcp_server, registry as R  # noqa: E402

# Variables that steer delegation; every test starts without them so the host's settings cannot leak in.
DELEGATE_VARS = ("SPEECHKIT_DELEGATE", "SPEECHKIT_DELEGATE_ROOT", "SPEECHKIT_DELEGATE_PATHMAP", "SPEECHKIT_DELEGATE_PYTHON")


# Base class: a scratch folder that plays the part of env.REPO, either as a toolkit folder or as a site-packages
# folder that holds a speechkit-narrate-<version>.dist-info next to the package, as pip leaves it.
class _Layout(unittest.TestCase):
    # Before each test: clean delegation variables, a fresh scratch folder, and env.REPO pointed at it.
    def setUp(self):
        patcher = mock.patch.dict(os.environ)  # restores os.environ afterwards, whatever the test changes
        patcher.start()  # take the snapshot now
        self.addCleanup(patcher.stop)  # and put it back when the test ends
        for key in DELEGATE_VARS:  # the host may have set any of them
            os.environ.pop(key, None)  # remove it for this test
        tmp = tempfile.TemporaryDirectory()  # an empty folder that is deleted again afterwards
        self.addCleanup(tmp.cleanup)  # deleted even when the test fails
        self.dir = Path(tmp.name)  # the folder playing env.REPO
        repo = mock.patch.object(env, "REPO", self.dir)  # env.installed() and the registry read env.REPO at call time
        repo.start()  # from here on the code under test looks at the scratch folder
        self.addCleanup(repo.stop)  # the real REPO comes back after the test

    # Makes the scratch folder look like site-packages after `pip install speechkit-narrate`.
    def install(self):
        (self.dir / "speechkit-narrate-9.9.9.dist-info").mkdir()  # the folder pip writes beside the package


# env.installed(): the one question every other change depends on.
class Detect(_Layout):
    # A plain toolkit folder has no dist-info, so it is not an installed copy.
    def test_toolkit_folder_is_not_installed(self):
        self.assertFalse(env.installed())  # a folder with no package metadata in it is not an installed copy

    # A speechkit-narrate-<version>.dist-info beside the package is what pip leaves, and it means installed.
    def test_dist_info_means_installed(self):
        self.install()  # make the scratch folder look like site-packages after pip
        self.assertTrue(env.installed())  # and now it counts as an installed copy

    # pip and setuptools write the metadata folder under the normalized distribution name, in which "-" becomes "_"
    # (the binary distribution format: speechkit-narrate installs speechkit_narrate-<version>.dist-info), so that
    # spelling must count as installed too. This is the folder a real `pip install speechkit-narrate` leaves.
    def test_normalized_dist_info_means_installed(self):
        (self.dir / "speechkit_narrate-9.9.9.dist-info").mkdir()  # the name pip actually writes
        self.assertTrue(env.installed())

    # The metadata of some other package does not count.
    def test_other_packages_dist_info_is_ignored(self):
        (self.dir / "numpy-2.1.0.dist-info").mkdir()  # the metadata of some other package (numpy)
        self.assertFalse(env.installed())  # does not make the folder an installed copy

    # A folder that cannot be listed answers "toolkit folder", the behaviour before installed() existed.
    def test_unlistable_folder_counts_as_toolkit_folder(self):
        # listing the folder fails with a permission error
        with mock.patch.object(Path, "glob", side_effect=OSError("denied")):
            self.assertFalse(env.installed())  # so installed() answers as it did before it existed: a toolkit folder

    # The checkout these tests run from is a toolkit folder.
    def test_this_checkout_is_not_installed(self):
        with mock.patch.object(env, "REPO", REPO):  # undo the scratch folder for this one test
            self.assertFalse(env.installed())  # the real checkout has no dist-info folder


# registry._delegate_cmd and the command lines built from it.
class Delegation(_Layout):
    # Before each test: a WSL delegate and a path map, so no wslpath process is ever started.
    def setUp(self):
        super().setUp()  # scratch folder and clean variables
        os.environ["SPEECHKIT_DELEGATE"] = "wsl.exe -d Ubuntu-24.04"  # delegate_prefix adds --exec
        os.environ["SPEECHKIT_DELEGATE_PATHMAP"] = "C:\\=/mnt/c/"  # C:\x becomes /mnt/c/x without asking WSL

    # A toolkit folder is translated and sent, as before.
    def test_checkout_sends_its_folder(self):
        with mock.patch.object(env, "REPO", Path("C:\\tk")):  # a Windows-style toolkit folder
            cmd, root, py = R._delegate_cmd()  # the command, the folder to send and the delegate's Python
        self.assertEqual(root, "/mnt/c/tk")  # the Windows-style folder came back translated through the path map
        self.assertEqual(py, "python3")  # the delegate's default Python
        self.assertEqual(cmd[-1], "--exec")  # the bare wsl delegate got --exec appended

    # An installed copy sends nothing: its folder is site-packages.
    def test_installed_copy_sends_no_folder(self):
        self.install()  # an installed copy
        cmd, root, py = R._delegate_cmd()  # the same question for it
        self.assertEqual(root, "")  # it sends no folder

    # SPEECHKIT_DELEGATE_ROOT still wins, installed or not.
    def test_explicit_root_wins(self):
        self.install()  # an installed copy
        os.environ["SPEECHKIT_DELEGATE_ROOT"] = "/srv/toolkit"  # but the variable names a folder
        self.assertEqual(R._delegate_cmd()[1], "/srv/toolkit")  # and the variable wins

    # An empty SPEECHKIT_DELEGATE_ROOT counts as unset (the behaviour before this change).
    def test_empty_explicit_root_counts_as_unset(self):
        os.environ["SPEECHKIT_DELEGATE_ROOT"] = ""  # the variable is set, but to nothing
        with mock.patch.object(env, "REPO", Path("C:\\tk")):  # the toolkit folder is a Windows-style path
            self.assertEqual(R._delegate_cmd()[1], "/mnt/c/tk")  # computed from the checkout
        self.install()  # now an installed copy
        self.assertEqual(R._delegate_cmd()[1], "")  # nothing for an installed copy

    # The delegate Python still comes from SPEECHKIT_DELEGATE_PYTHON.
    def test_delegate_python_is_kept(self):
        # the variable names the delegate's Python
        os.environ["SPEECHKIT_DELEGATE_PYTHON"] = "/opt/speechkit_narrate/venv/bin/python"
        self.install()  # an installed copy
        self.assertEqual(R._delegate_cmd()[2], "/opt/speechkit_narrate/venv/bin/python")  # and the command uses that Python

    def test_empty_delegate_python_uses_the_default(self):
        # PowerShell/.NET callers can leave an inherited variable present but empty.
        os.environ["SPEECHKIT_DELEGATE_PYTHON"] = ""
        self.assertEqual(R._delegate_cmd()[2], "python3")

    # The words before the Python command: PYTHONPATH only when there is a folder.
    def test_remote_env_words(self):
        # a folder gives PYTHONPATH, and the platform variable is blanked
        self.assertEqual(R._remote_env("/mnt/c/tk"), ["env", "PYTHONPATH=/mnt/c/tk", "SPEECHKIT_PLATFORM=", "SPEECHKIT_DELEGATION_DEPTH=1"])
        # no folder: only the platform variable is blanked
        self.assertEqual(R._remote_env(""), ["env", "SPEECHKIT_PLATFORM=", "SPEECHKIT_DELEGATION_DEPTH=1"])

    # The advice appears only for "No module named speechkit_narrate" with no folder sent.
    def test_missing_package_hint(self):
        # the traceback that a missing package leaves in a log
        lines = ["Traceback (most recent call last):", "ModuleNotFoundError: No module named 'speechkit_narrate'"]
        self.assertIn("SPEECHKIT_DELEGATE_ROOT", R._missing_package_hint("", lines))  # the advice names the way out
        # old wording
        self.assertIn("SPEECHKIT_DELEGATE_ROOT", R._missing_package_hint("", ["No module named speechkit_narrate"]))
        self.assertEqual(R._missing_package_hint("/mnt/c/tk", lines), "")  # a folder was sent: no install advice
        # other module
        self.assertEqual(R._missing_package_hint("", ["ModuleNotFoundError: No module named 'numpy'"]), "")
        self.assertEqual(R._missing_package_hint("", []), "")  # nothing was logged

    # _tee relays every line and keeps only the last 30.
    def test_tee_keeps_the_last_thirty_lines(self):
        seen = []  # what the wrapped log function received
        keep, tail = R._tee(seen.append)  # wraps a log function; keep relays a line, tail holds the last ones
        for n in range(45):  # more lines than the buffer holds
            keep(f"line {n}")  # one line through the wrapper
        self.assertEqual(len(seen), 45)  # nothing was dropped on the way through
        self.assertEqual(tail[0], "line 15")  # the oldest kept line
        self.assertEqual(tail[-1], "line 44")  # the newest

    # Runs delegate_tool with a stand-in process runner that logs the given lines and ends with the given outcome.
    # Returns (the command line that would have started, the log lines, the exception or None).
    def run_tool(self, lines, code=1, res=None):
        started = []  # the argument list handed to the process runner

        def fake_stream(args, log):  # replaces R._stream: no process is started
            started.append(list(args))  # remember the command line
            for line in lines:  # the remote log, line by line
                log(line)  # relay the line to the log function
            return code, ({} if res is None else res), ""  # exit code, parsed JSON, raw stdout
        logged = []  # lines relayed to the caller's log
        tool = R.Tool(name="probe", run=lambda **k: {}, params={})  # a tool with no parameters
        error = None  # set when delegate_tool raises
        with mock.patch.object(R, "_stream", fake_stream):  # replaces the process runner for the call below
            try:  # a failure is expected, so catch it
                R.delegate_tool(tool, {}, log=logged.append)  # the stand-in plays the remote log and the outcome
            except RuntimeError as e:  # the failure the test wants to read
                error = e  # keep the error so the test can read it
        return started[0], logged, error  # the command line, the relayed log and the error

    # An installed copy's command line has no PYTHONPATH; a checkout's has.
    def test_tool_command_line(self):
        self.install()  # an installed copy
        args, _, _ = self.run_tool([], code=0, res={"ok": True})  # a tool that succeeds with no output at all
        self.assertNotIn("PYTHONPATH", " ".join(args))  # nothing about a toolkit folder
        # the two words after env: the command itself and the blanked platform variable
        self.assertEqual(args[args.index("env"):args.index("env") + 2], ["env", "SPEECHKIT_PLATFORM="])
        (self.dir / "speechkit-narrate-9.9.9.dist-info").rmdir()  # back to a toolkit folder
        with mock.patch.object(env, "REPO", Path("C:\\tk")):  # the toolkit folder is a Windows-style path
            args, _, _ = self.run_tool([], code=0, res={"ok": True})  # the same tool again, now with a folder to send
        self.assertIn("PYTHONPATH=/mnt/c/tk", args)  # the checkout is sent as before

    # A failed run of an installed copy is explained, and its log is still relayed in full.
    def test_failed_tool_run_names_the_fix(self):
        self.install()  # an installed copy
        # the traceback that a missing package leaves in a log
        lines = ["Traceback (most recent call last):", "ModuleNotFoundError: No module named 'speechkit_narrate'"]
        _, logged, error = self.run_tool(lines)  # the run fails after logging it
        self.assertEqual(logged[0], "probe: delegated to wsl.exe -d Ubuntu-24.04")  # the toolkit's own first line
        self.assertEqual(logged[1:], lines)  # the remote log is relayed unchanged after it
        self.assertIsNotNone(error)  # the run failed
        self.assertIn("delegated tool probe failed (exit 1)", str(error))  # the old message is still there
        self.assertIn("SPEECHKIT_DELEGATE_ROOT", str(error))  # and now says what to do

    # A failure that is not a missing package gets no install advice.
    def test_other_failures_get_no_advice(self):
        self.install()  # an installed copy
        # a failure that has nothing to do with a missing package
        _, _, error = self.run_tool(["ValueError: bad page range"])
        self.assertNotIn("SPEECHKIT_DELEGATE_ROOT", str(error))  # its message has no install advice

    # The stage path (R.delegate) builds its command the same way and explains a missing package the same way.
    def test_stage_run_uses_the_same_rules(self):
        self.install()  # an installed copy
        project = types.SimpleNamespace(root=Path("C:\\books\\one"))  # only .root is read
        stage = types.SimpleNamespace(name="narrate")  # only .name is read
        logged = []  # the context's log
        # the context: the stage's options and a log that collects into the list above
        ctx = types.SimpleNamespace(options={"narrate": {"voice": "x"}}, log=logged.append)
        started = []  # command lines handed to the runner

        def fake_stream(args, log):  # fails with the missing-package traceback
            started.append(list(args))  # remember the command line
            log("ModuleNotFoundError: No module named 'speechkit_narrate'")  # the remote log says that the package is missing
            return 1, {}, ""  # exit code 1, no JSON result and no output
        with mock.patch.object(R, "_stream", fake_stream):  # the stand-in replaces the process runner
            with self.assertRaises(RuntimeError) as caught:  # the stage fails as a whole
                R.delegate(project, stage, ctx, False)  # the stage goes through the same delegation as a tool
        self.assertNotIn("PYTHONPATH", " ".join(started[0]))  # installed copy: no folder
        self.assertIn("/mnt/c/books/one", started[0])  # the project folder was translated
        self.assertIn("SPEECHKIT_DELEGATE_ROOT", str(caught.exception))  # the error carries the install advice
        self.assertEqual(logged[1:], ["ModuleNotFoundError: No module named 'speechkit_narrate'"])  # after "delegated to ..."


# The Pythons that the MCP server and the audit start.
class Children(_Layout):
    # An installed copy leaves PYTHONPATH exactly as the server found it.
    def test_mcp_children_of_an_installed_copy(self):
        self.install()  # an installed copy
        os.environ["PYTHONPATH"] = "/keep/me"  # the server was started with one entry on the path
        e = mcp_server.child_env()  # the environment the server hands to a child process
        self.assertEqual(e["PYTHONPATH"], "/keep/me")  # untouched
        self.assertEqual(e["PYTHONUTF8"], "1")  # the other settings still apply
        del os.environ["PYTHONPATH"]  # remove the entry again
        self.assertNotIn("PYTHONPATH", mcp_server.child_env())  # and none is invented

    # A toolkit folder still goes first, with the existing entries after it.
    def test_mcp_children_of_a_toolkit_folder(self):
        # the path holds an entry and also the toolkit folder
        os.environ["PYTHONPATH"] = os.pathsep.join(["/keep/me", str(mcp_server.ROOT)])
        parts = mcp_server.child_env()["PYTHONPATH"].split(os.pathsep)  # the path of the child, split into entries
        self.assertEqual(parts, [str(mcp_server.ROOT), "/keep/me"])  # ROOT first, listed once

    # The audit puts a toolkit folder on PYTHONPATH and sys.path, and an installed copy touches neither.
    def test_audit_exposes_only_a_toolkit_folder(self):
        saved = list(sys.path)  # sys.path is restored below
        # the original path is put back when the test ends
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), saved))
        os.environ["PYTHONPATH"] = "/keep/me"  # the path already holds one entry
        self.install()  # an installed copy
        audit._expose_repo()  # the audit adds the toolkit folder only where it is needed
        self.assertEqual(os.environ["PYTHONPATH"], "/keep/me")  # unchanged
        self.assertNotIn(str(self.dir), sys.path)  # and the folder is not added to sys.path either
        (self.dir / "speechkit-narrate-9.9.9.dist-info").rmdir()  # a toolkit folder again
        audit._expose_repo()  # expose again, now as a toolkit folder
        self.assertEqual(os.environ["PYTHONPATH"], os.pathsep.join([str(self.dir), "/keep/me"]))  # folder first
        self.assertEqual(sys.path[0], str(self.dir))  # and importable here


if __name__ == "__main__":  # also runnable directly: python3 tests/narration/test_installed.py
    unittest.main()  # run the tests of this file when it is started directly
