"""Windows paths for a toolkit that runs inside WSL (speechkit_narrate/winpaths.py), alone and through the MCP server.

    python3 -m unittest tests.narration.test_winpaths        (from the repository root)

The first classes test the translation functions with the host kept out of the way: the kernel banner and wsl.conf
the module reads are replaced by files in a scratch folder, and the WSL variables are cleared, so the answers are
the same on Windows, macOS, Linux and a real WSL. The last classes start `python -m speechkit_narrate mcp` the way
test_mcp does, with SPEECHKIT_WINPATHS_ROOT pointing at a scratch "drives" folder, and send Windows paths to it.
"""
from __future__ import annotations  # annotation hints stay strings, as in the other test files

# Standard-library imports first; the toolkit is imported after its folder is on sys.path.
import os  # environment variables, chmod and the platform name
import shutil  # rmtree for scratch folders
import stat  # file mode bits for the fake wslpath
import sys  # sys.path is extended below
import tempfile  # scratch folders
import unittest  # the test framework
from pathlib import Path  # object paths for the scratch folders
from unittest import mock  # patch.object replaces the module's file locations during a test

REPO = Path(__file__).resolve().parents[2]  # the folder holding the speechkit_narrate package and tests/
sys.path.insert(0, str(REPO))  # use this checkout, not an installed copy

from speechkit_narrate import mcp_server, winpaths  # noqa: E402 - imported after sys.path is set
from tests.narration.test_mcp import BOOK, NAP_PLUGIN, _ServerCase  # noqa: E402 - the server harness

# the environment variables these tests set; each test starts with them removed and ends with them restored
KEYS = ("SPEECHKIT_WINPATHS", "SPEECHKIT_WINPATHS_ROOT", "WSL_DISTRO_NAME", "WSL_INTEROP")


# Base class: a scratch folder, a clean environment and stand-ins for the two files the module reads. Has no tests.
class _Isolated(unittest.TestCase):
    # Clears the variables, points PROC_VERSION and WSL_CONF at scratch files and forgets what was learned.
    def setUp(self):
        saved = {k: os.environ.get(k) for k in KEYS}  # what the variables held before the test

        # Puts the variables back and clears the module's memory (registered to run after the test).
        def restore():
            for key, value in saved.items():  # each variable the test may have changed
                if value is None:  # it was not set before
                    os.environ.pop(key, None)  # so it must not be set afterwards
                else:  # it had a value
                    os.environ[key] = value  # put the value back
            winpaths.reset()  # forget cached translations and the "Windows path seen" flag
        self.addCleanup(restore)  # runs even when the test fails
        for key in KEYS:  # start every test with none of them set
            os.environ.pop(key, None)  # removes the variable if present
        self.tmp = Path(tempfile.mkdtemp(prefix="speechkit-narrate-winpaths-"))  # this test's scratch folder
        self.addCleanup(shutil.rmtree, self.tmp, True)  # removed afterwards, errors ignored
        self.banner = self.tmp / "version"  # stands in for /proc/version
        self.banner.write_text("Linux version 6.1.0 (builder@host) #1 SMP", encoding="utf-8")  # an ordinary kernel
        self.conf = self.tmp / "wsl.conf"  # stands in for /etc/wsl.conf; absent until a test writes it
        # the two files the module reads, and the translator it would run: a real WSL's must not answer these tests
        for name, value in (("PROC_VERSION", self.banner), ("WSL_CONF", self.conf), ("WSLPATH", "speechkit-narrate-no-wslpath")):
            patcher = mock.patch.object(winpaths, name, value)  # swap the module's constant for the stand-in
            patcher.start()  # in effect from here on
            self.addCleanup(patcher.stop)  # undone after the test
        winpaths.reset()  # start without memory

    # Makes the module think it runs inside WSL, as the environment variable WSL sets would.
    def in_wsl(self, distro: str = "Ubuntu-24.04"):
        os.environ["WSL_DISTRO_NAME"] = distro  # the variable WSL sets for every process in a distribution


# Which situations switch the translation on.
class Modes(_Isolated):
    # Proves the spellings of SPEECHKIT_WINPATHS and that anything unknown means auto.
    def test_mode_spellings(self):
        self.assertEqual(winpaths.mode(), "auto")  # unset
        for text in ("1", "on", "TRUE", " yes "):  # spellings of "always"
            os.environ["SPEECHKIT_WINPATHS"] = text  # set the variable
            self.assertEqual(winpaths.mode(), "on", text)  # all mean on
        for text in ("0", "off", "False", "no"):  # spellings of "never"
            os.environ["SPEECHKIT_WINPATHS"] = text  # set the variable
            self.assertEqual(winpaths.mode(), "off", text)  # all mean off
        os.environ["SPEECHKIT_WINPATHS"] = "sometimes"  # not a mode
        self.assertEqual(winpaths.mode(), "auto")  # falls back to auto

    # Proves the two WSL signals: the environment, and the kernel banner when the environment was cleaned.
    def test_in_wsl_signals(self):
        self.assertFalse(winpaths.in_wsl())  # ordinary Linux banner and no variables
        os.environ["WSL_INTEROP"] = "/run/WSL/1_interop"  # the second variable WSL sets
        self.assertTrue(winpaths.in_wsl())  # the environment is enough
        os.environ.pop("WSL_INTEROP")  # back to nothing
        self.banner.write_text("Linux version 5.15.153.1-microsoft-standard-WSL2", encoding="utf-8")  # a WSL 2 kernel
        self.assertTrue(winpaths.in_wsl())  # the banner is enough
        self.banner.write_text("Linux version 4.4.0-19041-Microsoft", encoding="utf-8")  # a WSL 1 kernel
        self.assertTrue(winpaths.in_wsl())  # capital M counts as well
        self.banner.unlink()  # no /proc/version at all (macOS)
        self.assertFalse(winpaths.in_wsl())  # not WSL

    # Proves when translation is active: auto only inside WSL, on always, off never.
    def test_active(self):
        self.assertFalse(winpaths.active())  # auto, not WSL
        self.in_wsl()  # now inside WSL
        self.assertTrue(winpaths.active())  # auto, WSL
        os.environ["SPEECHKIT_WINPATHS"] = "off"  # forced off
        self.assertFalse(winpaths.active())  # off wins even inside WSL
        os.environ.pop("WSL_DISTRO_NAME")  # outside WSL again
        os.environ["SPEECHKIT_WINPATHS"] = "on"  # forced on
        self.assertTrue(winpaths.active())  # on wins outside WSL

    # Proves the folder holding the drives: default, wsl.conf, the override and the odd spellings.
    def test_mount_root(self):
        self.assertEqual(winpaths.mount_root(), "/mnt")  # no wsl.conf: WSL's default
        self.conf.write_text("[automount]\nroot = /windir/\noptions = metadata\n", encoding="utf-8")  # moved drives
        self.assertEqual(winpaths.mount_root(), "/windir")  # read from wsl.conf, trailing slash dropped
        self.conf.write_text('[automount]\nroot = "/quoted/"\n', encoding="utf-8")  # a quoted value
        self.assertEqual(winpaths.mount_root(), "/quoted")  # quotes dropped too
        self.conf.write_text("this is not an ini file\n", encoding="utf-8")  # damaged
        self.assertEqual(winpaths.mount_root(), "/mnt")  # falls back to the default
        os.environ["SPEECHKIT_WINPATHS_ROOT"] = "/data/drives/"  # the explicit override
        self.assertEqual(winpaths.mount_root(), "/data/drives")  # wins over wsl.conf, trailing slash dropped
        os.environ["SPEECHKIT_WINPATHS_ROOT"] = "drives"  # written without a leading slash
        self.assertEqual(winpaths.mount_root(), "/drives")  # made absolute
        os.environ["SPEECHKIT_WINPATHS_ROOT"] = "/"  # the drives sit directly under the root
        self.assertEqual(winpaths.mount_root(), "")  # an empty prefix, so C: becomes /c


# Windows path to Linux path.
class ToLocal(_Isolated):
    # Starts every test inside WSL with the standard mount root (the cases below do not need a real wslpath).
    def setUp(self):
        super().setUp()  # scratch folder and clean environment
        self.in_wsl()  # inside WSL

    # Proves the drive forms: both slashes, any letter case, a bare drive, the extended prefix, doubled slashes,
    # a trailing slash, Explorer's quotes and stray spaces.
    def test_drive_forms(self):
        cases = {  # each Windows form, and the Linux path it must become
            r"C:\Users\me\Book": "/mnt/c/Users/me/Book",  # the usual form
            "C:/Users/me/Book": "/mnt/c/Users/me/Book",  # forward slashes
            r"c:\users\me": "/mnt/c/users/me",  # lower-case drive, the rest keeps its case
            "C:\\": "/mnt/c",  # a drive's root
            "C:": "/mnt/c",  # a bare drive
            r"\\?\D:\data\x.pdf": "/mnt/d/data/x.pdf",  # extended-length prefix
            "C:\\\\Users\\\\me": "/mnt/c/Users/me",  # doubled backslashes from over-escaped JSON
            "C:\\Users\\me\\": "/mnt/c/Users/me/",  # a trailing slash stays
            '"C:\\Users\\me"': "/mnt/c/Users/me",  # Explorer's "Copy as path" wraps the path in quotes
            '  C:\\Users\\me  ': "/mnt/c/Users/me",  # spaces around it
            "C:\\My Books\\a b.pdf": "/mnt/c/My Books/a b.pdf",  # spaces and no shell involved
        }
        for given, expected in cases.items():  # each form
            self.assertEqual(winpaths.to_local(given), expected, given)  # translated as listed

    # Proves what is left alone: relative paths, drive-relative forms, Linux paths, other types and other distributions.
    def test_left_alone(self):
        # text that must come back unchanged (the list goes on below)
        for text in ("Users\\me", "C:file", "/home/me", "~/Books", "", "relative/dir", "http://x/y",
                     "\\\\server\\share\\x"):
            self.assertEqual(winpaths.to_local(text), text, text)  # unchanged
        for value in (None, 5, ["C:\\x"], {"a": 1}):  # not text
            self.assertEqual(winpaths.to_local(value), value)  # returned as it came
        # another distro
        self.assertEqual(winpaths.to_local("\\\\wsl$\\Debian\\home\\me"), "\\\\wsl$\\Debian\\home\\me")

    # Proves the paths inside this distribution's own file system, in both spellings, and the other-distribution case.
    def test_wsl_unc(self):
        self.assertEqual(winpaths.to_local("\\\\wsl$\\Ubuntu-24.04\\home\\me\\x"), "/home/me/x")  # the old spelling
        self.assertEqual(winpaths.to_local("\\\\wsl.localhost\\Ubuntu-24.04\\home\\me"), "/home/me")  # the new one
        self.assertEqual(winpaths.to_local("//wsl.localhost/ubuntu-24.04/tmp"), "/tmp")  # forward slashes, any case
        self.assertEqual(winpaths.to_local("\\\\wsl$\\Ubuntu-24.04"), "/")  # the distribution's root
        os.environ.pop("WSL_DISTRO_NAME")  # name unknown (forced on elsewhere)
        os.environ["SPEECHKIT_WINPATHS"] = "on"  # translate anyway
        self.assertEqual(winpaths.to_local("\\\\wsl$\\Anything\\opt"), "/opt")  # any distribution name is accepted

    # Proves the mount root is honoured, including a root of "/".
    def test_mount_root_used(self):
        os.environ["SPEECHKIT_WINPATHS_ROOT"] = "/data/drives"  # drives somewhere else
        self.assertEqual(winpaths.to_local("D:\\x"), "/data/drives/d/x")  # under that folder
        os.environ["SPEECHKIT_WINPATHS_ROOT"] = "/"  # drives directly under the root
        self.assertEqual(winpaths.to_local("D:\\x"), "/d/x")  # no doubled slash

    # Proves nothing is translated when switched off or outside WSL.
    def test_inactive(self):
        os.environ["SPEECHKIT_WINPATHS"] = "off"  # forced off
        self.assertEqual(winpaths.to_local("C:\\x"), "C:\\x")  # unchanged
        os.environ.pop("SPEECHKIT_WINPATHS")  # auto again
        os.environ.pop("WSL_DISTRO_NAME")  # but not in WSL
        self.assertEqual(winpaths.to_local("C:\\x"), "C:\\x")  # unchanged

    # Proves WSL's own wslpath is preferred when it exists, asked once per path, and that a failing wslpath falls back
    # to the rewrite.
    @unittest.skipIf(os.name == "nt", "the fake wslpath is a shell script")
    def test_wslpath(self):  # uses a fake wslpath program that logs its calls
        log = self.tmp / "calls.log"  # the fake records its arguments here
        script = self.tmp / "fake-wslpath"  # stands in for wslpath
        # a shell script: log the call, fail on request, otherwise print a fixed answer
        script.write_text('#!/bin/sh\necho "$@" >> "$FAKE_LOG"\n[ "$FAKE_FAIL" = 1 ] && exit 1\n'
                          # the second half of the script: the fixed answer it prints
                          'echo /fake/translated\n', encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)  # make it executable
        # two patches for the block: the fake program, and the log it writes to
        with mock.patch.object(winpaths, "WSLPATH", str(script)), \
                mock.patch.dict(os.environ, {"FAKE_LOG": str(log)}):  # use the fake, and tell it where to log
            self.assertEqual(winpaths.to_local("C:\\x"), "/fake/translated")  # the fake's answer wins over the rewrite
            self.assertEqual(winpaths.to_local("C:\\x"), "/fake/translated")  # asked again
            self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["-u C:\\x"])  # but wslpath ran only once
            os.environ["FAKE_FAIL"] = "1"  # now it fails
            self.assertEqual(winpaths.to_local("C:\\y"), "/mnt/c/y")  # the pure rewrite takes over

    # Proves wslpath is not used outside WSL even when the program exists.
    @unittest.skipIf(os.name == "nt", "the fake wslpath is a shell script")
    def test_wslpath_only_inside_wsl(self):  # the fake program could answer, but must not be asked outside WSL
        script = self.tmp / "fake-wslpath"  # stands in for wslpath
        script.write_text("#!/bin/sh\necho /fake/translated\n", encoding="utf-8")  # always answers
        script.chmod(script.stat().st_mode | stat.S_IXUSR)  # executable
        os.environ.pop("WSL_DISTRO_NAME")  # not inside WSL
        os.environ["SPEECHKIT_WINPATHS"] = "on"  # forced on
        with mock.patch.object(winpaths, "WSLPATH", str(script)):  # the fake exists and could answer
            self.assertEqual(winpaths.to_local("C:\\x"), "/mnt/c/x")  # but the rewrite is used, not the fake


# Linux path back to Windows path, and when results are rewritten.
class ToWindows(_Isolated):
    # Proves the whole-string rewrite and what it leaves alone.
    def test_to_windows(self):
        cases = {  # each Linux path, and the Windows path it must become
            "/mnt/c/Users/me/x.mp3": "C:\\Users\\me\\x.mp3",  # a file below a drive
            "/mnt/c": "C:\\",  # the drive itself
            "/mnt/c/": "C:\\",  # with a trailing slash
            "/mnt/d/a b/c": "D:\\a b\\c",  # spaces
            "/mnt/wsl/x": "/mnt/wsl/x",  # a folder that is not a one-letter drive
            "/mnt/cd/x": "/mnt/cd/x",  # a two-letter name
            "/mnt/C/x": "/mnt/C/x",  # drive folders are lower case
            "/home/me/x": "/home/me/x",  # inside the distribution
            "saved /mnt/c/x ok": "saved /mnt/c/x ok",  # a sentence that mentions a path
            "": "",  # empty
        }
        for given, expected in cases.items():  # each case
            self.assertEqual(winpaths.to_windows(given), expected, given)  # rewritten or not, as listed

    # Proves a different mount root is used for the rewrite.
    def test_root(self):
        os.environ["SPEECHKIT_WINPATHS_ROOT"] = "/data/drives"  # drives elsewhere
        self.assertEqual(winpaths.to_windows("/data/drives/c/x"), "C:\\x")  # rewritten
        self.assertEqual(winpaths.to_windows("/mnt/c/x"), "/mnt/c/x")  # the default root no longer applies

    # Proves results are rewritten only after the client used a Windows path (auto), always (on), never (off), that the
    # input is not changed, and that nested values, Path objects and non-text values are handled.
    def test_outbound(self):
        self.in_wsl()  # inside WSL, mode auto
        # a result with paths in a string, a list and a nested Path, and values that are not paths
        result = {"project": "/mnt/c/b", "files": ["/mnt/c/b/a.pdf", "/home/me/x"], "n": 3,
                  "deep": {"p": Path("/mnt/d/z")}, "none": None, "text": "see /mnt/c/b"}  # a typical result
        self.assertIs(winpaths.outbound(result), result)  # nothing sent yet: the very same object comes back
        self.assertFalse(winpaths.reporting())  # and the module is not reporting in the Windows form
        winpaths.to_local("C:\\b")  # the client sends a Windows path
        self.assertTrue(winpaths.reporting())  # now it is
        out = winpaths.outbound(result)  # the rewritten copy
        self.assertEqual(out["project"], "C:\\b")  # a drive path is rewritten
        self.assertEqual(out["files"], ["C:\\b\\a.pdf", "/home/me/x"])  # list items too; Linux-only paths stay
        self.assertEqual(out["deep"], {"p": "D:\\z"})  # a Path object inside a nested dict
        self.assertEqual((out["n"], out["none"], out["text"]), (3, None, "see /mnt/c/b"))  # the rest is untouched
        self.assertEqual(result["project"], "/mnt/c/b")  # the input was not modified
        self.assertEqual(winpaths.present("/mnt/c/b"), "C:\\b")  # messages use the same form
        os.environ["SPEECHKIT_WINPATHS"] = "off"  # switched off
        self.assertIs(winpaths.outbound(result), result)  # nothing is rewritten
        self.assertEqual(winpaths.present("/mnt/c/b"), "/mnt/c/b")  # nor messages

    # Proves "on" reports in the Windows form from the start, without any Windows path sent.
    def test_on_reports_always(self):
        os.environ["SPEECHKIT_WINPATHS"] = "on"  # forced on
        self.assertEqual(winpaths.outbound({"p": "/mnt/c/x"}), {"p": "C:\\x"})  # rewritten at once

    # Proves status() and note() describe the situation, and reset() forgets what was learned.
    def test_status_note_reset(self):
        self.assertEqual(winpaths.note(), "")  # outside WSL, no sentence
        # what status() reports outside WSL: translation inactive
        self.assertEqual(winpaths.status(), {"mode": "auto", "active": False, "wsl": False, "mount_root": "/mnt",
                                             "distro": None})  # nothing active
        self.in_wsl("Debian")  # inside a distribution called Debian
        self.assertIn("Windows paths", winpaths.note())  # the sentence for the instructions
        # and what it reports inside the Debian distribution: translation active
        self.assertEqual(winpaths.status(), {"mode": "auto", "active": True, "wsl": True, "mount_root": "/mnt",
                                             "distro": "Debian"})  # active
        winpaths.to_local("C:\\x")  # the client sends a Windows path
        self.assertTrue(winpaths.reporting())  # reporting now
        winpaths.reset()  # forget
        self.assertFalse(winpaths.reporting())  # no longer


# The server's own helpers, called directly.
class ServerHelpers(_Isolated):
    # Proves _path, _path_arg and project_dir translate, and that the error names the folder the way the client wrote
    # it.
    def test_helpers(self):
        drives = self.tmp / "drives"  # a stand-in for /mnt
        (drives / "c" / "books" / "lantern").mkdir(parents=True)  # C:\books\lantern
        os.environ["SPEECHKIT_WINPATHS"] = "on"  # translate here
        os.environ["SPEECHKIT_WINPATHS_ROOT"] = str(drives.resolve())  # the scratch drives
        want = (drives / "c" / "books" / "lantern").resolve()  # what C:\books\lantern means here
        self.assertEqual(mcp_server._path("C:\\books\\lantern", "project"), want)  # translated and resolved
        self.assertEqual(mcp_server._path_arg("C:\\books\\lantern"), str(want))  # the same, as text
        # lists
        self.assertEqual(mcp_server._path_arg(["C:\\books", "/x"]), [str((drives / "c" / "books").resolve()), "/x"])
        self.assertEqual(mcp_server._path_arg(7), 7)  # other types pass through
        self.assertEqual(mcp_server._path_arg("~"), os.path.expanduser("~"))  # ~ is still expanded
        with self.assertRaises(mcp_server.ToolError) as raised:  # a folder without project.json
            mcp_server.project_dir("C:\\books\\lantern")  # exists, but is no project
        self.assertIn("C:\\books\\lantern is not a speechkit-narrate project", str(raised.exception))  # named the client's way
        self.assertIn("no project.json", str(raised.exception))  # and why


# One server with translation forced on and the drives in a scratch folder; every path goes in as a Windows path.
class WindowsClient(_ServerCase):
    plugins = (str(NAP_PLUGIN),)  # the nap test tool writes a file at a path it is given
    # Makes the scratch drives and switches the translation on before the server starts (the harness copies
    # os.environ when it starts the server).
    @classmethod
    def prepare(cls, tmp: Path):  # remembers the variables, then makes the folder that stands in for the drives
        cls.saved = {k: os.environ.get(k) for k in KEYS}  # restored in tearDownClass
        cls.drives = (tmp / "drives").resolve()  # stands in for /mnt
        (cls.drives / "c" / "books").mkdir(parents=True)  # C:\books
        os.environ["SPEECHKIT_WINPATHS"] = "on"  # translate in the server
        os.environ["SPEECHKIT_WINPATHS_ROOT"] = str(cls.drives)  # and find the drives here

    # Restores the variables, then stops the server as the base class does.
    @classmethod
    def tearDownClass(cls):  # restores the variables once the server has stopped
        try:  # the base class stops the server and removes the folder
            super().tearDownClass()  # the base class stops the server and removes its folder
        finally:  # the variables are restored even when that failed
            for key, value in cls.saved.items():  # each variable the class set
                if value is None:  # it was not set before
                    os.environ.pop(key, None)  # remove it
                else:  # it was set
                    os.environ[key] = value  # put it back

    # Proves the server tells the client about Windows paths: the instructions and narrate_describe.
    def test_announced(self):
        self.assertIn("Windows paths", self.server.info.instructions)  # the sentence is in the instructions
        d = self.call("narrate_describe")  # the registry description
        self.assertTrue(d["windows_paths"]["active"])  # translation is active
        self.assertEqual(d["windows_paths"]["mode"], "on")  # because it was forced on

    # Proves a project can be made, read, changed and run with Windows paths only, and that results come back in that
    # form.
    def test_project_with_windows_paths(self):
        (self.drives / "c" / "books" / "lantern.txt").write_text(BOOK, encoding="utf-8")  # the book, at C:\books
        # project_new, with the project folder and the book given as Windows paths
        d = self.call("project_new", {"project": "C:\\books\\lantern", "input": "C:\\books\\lantern.txt",
                                      "title": "The Small Lantern"})  # both arguments are Windows paths
        self.assertEqual(d["project"], "C:\\books\\lantern")  # the answer uses the client's form
        # created in the right place
        self.assertTrue((self.drives / "c" / "books" / "lantern" / "project.json").is_file())
        st = self.call("project_status", {"project": "C:\\books\\lantern"})  # read it back the Windows way
        self.assertEqual(st["project"], "C:\\books\\lantern")  # again in the Windows form
        also = self.call("project_status", {"project": str(self.drives / "c" / "books" / "lantern")})  # the Linux way
        self.assertEqual(also["project"], "C:\\books\\lantern")  # reported in the form the client uses
        # project_run builds the script
        d = self.call("project_run", {"project": "C:\\books\\lantern", "targets": ["script"], "accept_draft": True,
                                      "wait_seconds": 60})  # a job on a Windows-path project
        self.assertEqual(d["state"], "done", d)  # finished
        self.assertEqual(d["project"], "C:\\books\\lantern")  # the job record names the project in the Windows form
        miss = self.call("project_status", {"project": "C:\\books\\missing"}, error=True)  # a folder that is not there
        self.assertIn("C:\\books\\missing is not a speechkit-narrate project", miss["error"])  # named as the client wrote it

    # Proves a standalone tool: its path arguments are translated, and the files it reports come back as Windows paths.
    def test_tool_with_windows_paths(self):
        # tool_nap, with the output file given as a Windows path
        d = self.call("tool_nap", {"out": "C:\\books\\napped\\done.txt", "wait_seconds": 60})
        self.assertEqual(d["state"], "done", d)  # finished
        files = d["result"]["files"]  # what the tool wrote
        self.assertEqual(files, ["C:\\books\\napped\\done.txt"])  # reported as a Windows path
        self.assertTrue((self.drives / "c" / "books" / "napped" / "done.txt").is_file())  # and it really exists, here
        self.assertTrue(Path(winpaths.to_local(files[0])).is_file())
        # the quick tool: the input and the project folder as Windows paths, the project reported the same way
        (self.drives / "c" / "books" / "quick.txt").write_text(BOOK, encoding="utf-8")
        d = self.call("tool_quick", {"input": "C:\\books\\quick.txt", "out": "C:\\books\\quick",
                                     "targets": ["extract"], "wait_seconds": 60})
        self.assertEqual(d["state"], "done", d)
        self.assertEqual(d["result"]["project"], "C:\\books\\quick")
        self.assertTrue((self.drives / "c" / "books" / "quick" / "work" / "draft.md").is_file())


# A server with translation switched off: the same Windows path is not understood.
class TranslationOff(_ServerCase):
    # Switches the translation off before the server starts.
    @classmethod
    def prepare(cls, tmp: Path):  # remembers the variables, then switches the translation off
        cls.saved = {k: os.environ.get(k) for k in KEYS}  # restored in tearDownClass
        os.environ["SPEECHKIT_WINPATHS"] = "off"  # no translation

    # Restores the variables, then stops the server as the base class does.
    @classmethod
    def tearDownClass(cls):  # restores the variables once the server has stopped
        try:  # the base class stops the server and removes the folder
            super().tearDownClass()  # the base class stops the server and removes its folder
        finally:  # the variables are restored even when that failed
            for key, value in cls.saved.items():  # each variable the class set
                if value is None:  # it was not set before
                    os.environ.pop(key, None)  # remove it
                else:  # it was set
                    os.environ[key] = value  # put it back

    # Proves a Windows path is just a strange relative name when translation is off, and that describe says so.
    def test_off(self):
        miss = self.call("project_status", {"project": "C:\\books\\lantern"}, error=True)  # not translated
        self.assertIn("not a speechkit-narrate project", miss["error"])  # so the folder is not found
        self.assertNotIn("Windows paths", self.server.info.instructions)  # and the instructions do not promise it
        d = self.call("narrate_describe")  # the registry description
        self.assertFalse(d["windows_paths"]["active"])  # translation is inactive


if __name__ == "__main__":  # run directly: python3 tests/narration/test_winpaths.py
    unittest.main()  # discover and run the classes above
