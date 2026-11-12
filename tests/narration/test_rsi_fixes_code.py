# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Regression tests for review fixes in emblem_rsi.loop and emblem_rsi.code, plus package-wide hygiene checks."""
import argparse  # builds the namespace the loop command handlers take
import contextlib  # captures the handlers' standard error
import io  # in-memory text stream for the captured output
import json  # writes rubric files
import re  # matches hard-coded home paths
import shutil  # removes the temporary folders
import subprocess  # runs the command line end to end
import sys  # the interpreter for the command line run
import tempfile  # temporary run folders and repositories
import unittest  # standard library test framework
from pathlib import Path  # paths for runs, repositories and package files

from emblem_rsi import loop  # rubric validation and the seal/status handlers
from emblem_rsi.code import langs, measure as cm  # XML analysis and the code-comment audit

REPO = Path(__file__).resolve().parents[2]  # repository root: tests/narration is two levels below it
PKG = REPO / "emblem_rsi"  # the package checked by the hygiene tests


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)


# A rubric without a name is refused before sealing, with a one-line error and exit status 2.
class RubricName(TmpCase):
    def rubric(self, **extra):
        r = {"schema": loop.RUBRIC_SCHEMA, "metrics": {"x": {"op": "min", "value": 1}}, **extra}
        p = self.tmp / "r.json"
        p.write_text(json.dumps(r), encoding="utf-8")
        return p

    def test_validate_requires_name(self):
        with self.assertRaisesRegex(loop.LoopError, "name"):
            loop.validate_rubric(json.loads(self.rubric().read_text()))
        loop.validate_rubric(json.loads(self.rubric(name="ok").read_text()))  # accepted with a name

    def test_cmd_seal_one_line_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = loop.cmd_seal(argparse.Namespace(rubric=str(self.rubric()), dir=str(self.tmp / "run")))
        self.assertEqual(code, 2)
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertFalse((self.tmp / "run" / "rubric.sealed.json").exists())  # nothing sealed

    def test_cli_seal_exit_status(self):
        r = subprocess.run([sys.executable, "-m", "emblem_rsi", "loop", "seal", "--dir", str(self.tmp / "run"),
                            "--rubric", str(self.rubric())], cwd=str(REPO), capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_status_of_old_unnamed_seal(self):
        run = self.tmp / "run"
        run.mkdir()
        rub = {"schema": loop.RUBRIC_SCHEMA, "metrics": {"x": {"op": "min", "value": 1}}}  # sealed before names
        sealed = {"schema": loop.SEALED_SCHEMA, "sealed_at": loop.now(),
                  "sha256": loop.sha256_bytes(loop.canonical(rub)), "rubric": rub}
        (run / "rubric.sealed.json").write_text(json.dumps(sealed), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(loop.cmd_status(argparse.Namespace(dir=str(run))), 0)


# code/measure.py: discover, apply with no files, trailing_text.
class CodeMeasure(TmpCase):
    def make_repo(self):
        repo = self.tmp / "repo"
        for rel, text in {"a.py": "x = 1\n", "fixtures/page.html": "<p>x</p>\n", "fixtures/f.py": "y = 2\n",
                          "pkg/fixtures/page.html": "<p>y</p>\n"}.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text, encoding="utf-8")
        return repo

    def test_discover_skips_top_level_fixtures(self):
        self.assertEqual(cm.discover(self.make_repo()), ["a.py", "fixtures/f.py"])

    def test_apply_with_no_files(self):
        run = self.tmp / "run"
        cm.seal_run(run, self.make_repo(), log=lambda *a: None)
        self.assertIsNone(cm.apply(run, [], log=lambda *a: None))  # no FileNotFoundError
        self.assertIsNone(cm.apply(run, log=lambda *a: None))  # no maps at all: the same
        self.assertFalse((run / "snapshots").exists())  # no empty snapshot for revert to pop

    def test_trailing_text(self):
        self.assertEqual(cm.trailing_text("x = 1  # one", "py"), "one")
        self.assertEqual(cm.trailing_text("x=1#tight", "py"), "tight")
        self.assertEqual(cm.trailing_text("x = 1", "py"), "")  # no marker: empty, not the code


# code/langs.py analyze_xml: the declaration row (and blank rows before it) refuse a comment above; others do not.
class XmlDeclaration(unittest.TestCase):
    def flags(self, text):
        a = langs.analyze_xml("t.xml", text)
        self.assertTrue(a.ok, a.error)
        return [ln.above for ln in a.lines]

    def test_declaration_rows(self):
        self.assertEqual(self.flags('<?xml version="1.0"?>\n<a>\n</a>\n')[:2], [False, True])
        self.assertEqual(self.flags('\n\n<?xml version="1.0"?>\n<a>\n</a>\n')[:4], [False, False, False, True])
        self.assertEqual(self.flags('<a>\n<b/>\n</a>\n')[:2], [True, True])  # no declaration: row 1 is open

    def test_allowed_insertions_still_parse(self):
        for text in ('\n\n<?xml version="1.0"?>\n<a>\n<b/>\n</a>\n', '<?xml version="1.0"?>\n<a>\n</a>\n'):
            rows = text.split("\n")
            for k, ok in enumerate(self.flags(text)):
                if ok:  # a comment line inserted above this row must leave a parseable document
                    t = "\n".join(rows[:k] + ["<!-- c -->"] + rows[k:])
                    self.assertTrue(langs.analyze_xml("t.xml", t).ok, (k, t))


# Package hygiene: licence header on every Python file and no hard-coded home folders.
class Hygiene(unittest.TestCase):
    def py_files(self):
        return [p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts]

    def test_spdx_headers(self):
        for p in self.py_files():
            lines = p.read_text(encoding="utf-8").splitlines()
            if lines and lines[0].startswith("#!"):
                lines = lines[1:]
            with self.subTest(file=str(p.relative_to(REPO))):
                self.assertEqual(lines[:2], ["# SPDX-License-Identifier: GPL-3.0-or-later",
                                             "# Copyright (C) 2026 EMBLEM-NLP"])

    def test_no_home_paths(self):
        for p in self.py_files() + list(PKG.glob("rubrics/*.json")):
            with self.subTest(file=str(p.relative_to(REPO))):
                self.assertIsNone(re.search(r"/home/\w", p.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
