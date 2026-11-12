"""The code-comment audit (emblem_rsi/code): line analysis per language, comment maps, and the sealed loop."""
# Postponed annotations, as in the rest of the package's modules.
from __future__ import annotations

import json  # measurement files written by the loop are read back as JSON
import shutil  # finds pwsh and node, and removes the temporary run folders
import subprocess  # runs the command-line interface as a real child process
import sys  # the current interpreter and the import search path
import tempfile  # temporary folders hold throwaway repositories and map files
import textwrap  # dedent lets sample sources be indented inside the test code
import unittest  # the standard library test framework used by every suite here
from pathlib import Path  # builds paths to the repository, run folder and map files

REPO = Path(__file__).resolve().parents[2]  # repository root: this test file lives in tests/narration/, two levels below
sys.path.insert(0, str(REPO))  # make the emblem_rsi package importable when run from anywhere

from emblem_rsi import loop  # noqa: E402
from emblem_rsi.code import cmap, langs  # noqa: E402
from emblem_rsi.code import measure as cm  # noqa: E402

HAVE_PWSH = shutil.which("pwsh") is not None  # PowerShell tests are skipped on machines without pwsh
HAVE_NODE = shutil.which("node") is not None  # JavaScript and HTML script tests need node on the PATH


# Compact per-line flags for assertions, in the style `code show` prints: 'R' or '-' for required, then '^' and '>' when
# a comment may go above or at the end. a is an Analysis; returns {line number: flag string}.
def flags(a: langs.Analysis) -> dict:
    # required marker, then the above and end placement markers
    return {ln.n: ("R" if ln.required else "-") + ("^" if ln.above else " ") + (">" if ln.trail else " ")
            for ln in a.lines}  # one entry for every line of the analysed file


# Explain every required line of a source with a generated comment, the way a map author would. a is the source's
# Analysis, text the source and template whether template rules apply. Returns the commented text and fails the test if
# any entry is refused.
def comment_everything(a: langs.Analysis, text: str, template=False) -> str:
    ents = []  # generated map entries, one per required line
    for ln in a.lines:  # visit every line of the analysed source
        if ln.required:  # only lines that need an explanation get an entry
            # above when allowed, otherwise at the end; the text names the line
            ents.append(cmap.Entry(ln.n, "^" if ln.above else ">", f"explains what line {ln.n} does and why", "t"))
    out, _, errs = cmap.apply(text, a, ents, template=template)  # insert the comments exactly as the real loop would
    assert not errs, errs  # every generated entry must be accepted by the applier
    return out  # the fully commented source for further assertions


# Python line analysis: docstrings, existing trailing comments, bracket and backslash continuations, closing brackets,
# and the identity after comments are added.
class Python(unittest.TestCase):
    # Sample module covering the interesting cases; the doubled backslash becomes a real line-continuation backslash in
    # the sample text.
    SRC = textwrap.dedent('''\
        """Module docstring
        on two lines."""
        import os  # already explained

        def f(x,
              y):
            msg = ("a long message "
                   "continued")
            total = x + \\
                y
            return [
                total,
            ]
        ''')  # end of the sample Python module source

    # Checks the per-line flags of the sample and its coverage count before any comment is added.
    def test_lines(self):
        a = langs.analyze("m.py", self.SRC)  # analyse the sample as if it were a file named m.py
        fl = flags(a)  # flags for every line, keyed by line number
        self.assertEqual(fl[1][0], "-")              # docstring: string literal only
        self.assertEqual(fl[2][0], "-")              # inside the docstring
        self.assertFalse(a.lines[1].above)  # line 2 lies inside the docstring, so nothing may be inserted above it
        self.assertEqual(fl[3], "R^ ")              # import with its own trailing comment: covered, nothing to append
        self.assertEqual(fl[5], "R^>")  # the def line needs a comment and accepts one either way
        self.assertEqual(fl[8][0], "-")              # string continuation line
        self.assertEqual(fl[9], "R^ ")              # ends with a backslash: no trailing comment
        self.assertEqual(fl[10], "R >")             # continues the backslash line: nothing above it
        self.assertEqual(fl[13][0], "-")             # closing bracket only
        cov, req, miss = a.coverage()  # explained lines, required lines and the uncovered list
        self.assertEqual((cov, req), (1, 8))  # eight lines need a comment; only the import is already explained
        self.assertIn(5, miss)  # the function header is among the uncovered lines

    # Commenting every required line must keep the code identity and reach full coverage, while a real code change must
    # alter the identity.
    def test_comment_everything_keeps_identity(self):
        a = langs.analyze("m.py", self.SRC)  # analyse the untouched sample first
        out = comment_everything(a, self.SRC)  # add a generated comment for every required line
        b = langs.analyze("m.py", out)  # analyse the commented version of the sample
        self.assertTrue(b.ok)  # the commented module must still parse
        self.assertEqual(a.identity, b.identity)  # same identity: only comments were added
        cov, req, miss = b.coverage()  # coverage figures of the commented version
        self.assertEqual(miss, [])  # no required line is left without a comment
        # flipping plus to minus is a code change and must be detected
        self.assertNotEqual(langs.analyze("m.py", self.SRC.replace("x + \\", "x - \\")).identity, a.identity)


# The PowerShell suite runs only where pwsh is installed.
@unittest.skipUnless(HAVE_PWSH, "pwsh not installed")
# PowerShell line analysis: comment-based help, here-strings, a pipeline continued on the next line, backtick
# continuations, and the identity after comments are added.
class PowerShell(unittest.TestCase):
    # Sample function with comment-based help, a here-string, a pipeline split after '|' and a backtick continuation.
    SRC = textwrap.dedent('''\
        <#
        .SYNOPSIS
        Says hello.
        #>
        function Say-Hello {
            param([string]$Name)
            $text = @"
        Hello $Name
        "@
            Get-ChildItem . |
                Where-Object { $_.Length -gt 0 }
            Write-Output $text `
                -NoEnumerate
        }
        ''')  # end of the sample PowerShell function source

    # Checks the placement flags, then that commenting everything keeps the identity while a real change does not.
    def test_lines_and_identity(self):
        a = langs.analyze("s.ps1", self.SRC)  # tokenize and analyse the sample through pwsh
        self.assertTrue(a.ok, a.error)  # the sample must parse; show the parser error otherwise
        fl = flags(a)  # flags for every line of the sample function
        self.assertFalse(a.lines[4].above)          # comment-based help stays directly above `function`
        self.assertEqual(fl[8][0], "-")              # inside the here-string
        self.assertFalse(a.lines[7].above)  # line 8 is here-string text, so nothing may go above it
        self.assertFalse(a.lines[10].above)          # directly after a pipe at the end of the line before
        self.assertFalse(a.lines[11].trail)          # ends with a backtick continuation
        self.assertFalse(a.lines[12].above)  # the line continuing the backtick cannot take a comment above
        out = comment_everything(a, self.SRC)  # comment every required line of the PowerShell sample
        b = langs.analyze("s.ps1", out)  # analyse the commented PowerShell version
        self.assertTrue(b.ok, b.error)  # the commented script must still parse cleanly
        self.assertEqual(a.identity, b.identity)  # only comments changed, so the identity matches
        self.assertEqual(b.coverage()[2], [])  # every required line is now explained
        # a changed comparison value must change the identity
        self.assertNotEqual(langs.analyze("s.ps1", self.SRC.replace("-gt 0", "-gt 1")).identity, a.identity)


# The JavaScript suite needs node to run the acorn helper.
@unittest.skipUnless(HAVE_NODE, "node not installed")
# JavaScript line analysis: a template literal spanning lines, a closing brace, and the identity after comments are
# added.
class JavaScript(unittest.TestCase):
    # Sample module with a multi-line template literal and a small function.
    SRC = textwrap.dedent('''\
        const a = 1;
        const html = `<div>
          ${a}
        </div>`;
        function g(x) {
          return x + a;
        }
        ''')  # end of the sample JavaScript module source

    # Checks template-literal handling and the closing-brace exemption, then that added comments use '//' and keep the
    # identity.
    def test_lines_and_identity(self):
        a = langs.analyze("x.mjs", self.SRC)  # analyse the sample as an ES module (.mjs)
        self.assertTrue(a.ok, a.error)  # acorn must accept the sample module
        fl = flags(a)  # flags for every line of the sample module
        self.assertEqual(fl[1], "R^>")  # a plain declaration takes comments above and at the end
        self.assertFalse(a.lines[1].trail)           # the template string continues past the line end
        self.assertEqual(fl[3][0], "-")              # inside the template string
        self.assertEqual(fl[7][0], "-")              # closing brace only
        out = comment_everything(a, self.SRC)  # comment every required line of the JavaScript sample
        b = langs.analyze("x.mjs", out)  # analyse the commented JavaScript version
        self.assertEqual(a.identity, b.identity)  # only comments changed, so the token identity matches
        self.assertEqual(b.coverage()[2], [])  # every required JavaScript line is now explained
        self.assertIn("// explains", out)  # comments were written with JavaScript double-slash syntax


# HTML templates with style and script regions, plus CSS, XML, TOML and requirements files.
class Markup(unittest.TestCase):
    # Inline scripts are tokenized by node, so this test is skipped without it.
    @unittest.skipUnless(HAVE_NODE, "node not installed")
    # A template page with placeholders, a style block, a tag split over two lines and a script: checks the comment
    # syntax per region, frozen lines and the identity.
    def test_html_template_regions(self):
        # Sample template: placeholders in the title, a CSS value and a script, plus a tag whose attributes span two
        # lines.
        src = textwrap.dedent('''\
            <title>{{title}}</title>
            <style>
            body { color: {{ink}}; }
            </style>
            <div class="a"
                 id="b">text</div>
            <script>
            var CFG = {{cfg_json}};
            </script>
            ''')  # end of the sample HTML template source
        # analyse as a template file, with line 1 frozen
        a = langs.analyze("speechkit_narrate/web/template/x.html", src, {"speechkit_narrate/web/template/x.html": [1]})
        self.assertTrue(a.ok, a.error)  # the template must parse, placeholder substitutions included
        self.assertEqual(a.lines[2].ctx_start, "css")  # line 3 sits inside the style block, so CSS comments
        self.assertEqual(a.lines[7].ctx_start, "js")  # line 8 sits inside the script, so JavaScript comments
        self.assertFalse(a.lines[5].above)           # attribute continuation inside a tag
        self.assertFalse(a.lines[0].required)        # frozen
        out = comment_everything(a, src, template=True)  # comment every required line, applying template rules
        self.assertIn("/* explains what line 3", out)  # the CSS line received a slash-star comment
        self.assertIn("// explains what line 8", out)  # the script line received a double-slash comment
        self.assertIn("<!-- explains what line 5", out)  # the markup line received an HTML comment
        # re-analyse the commented template with the same frozen line
        b = langs.analyze("speechkit_narrate/web/template/x.html", out, {"speechkit_narrate/web/template/x.html": [1]})
        self.assertEqual(a.identity, b.identity)  # only comments changed, so the combined identity matches
        self.assertEqual(b.coverage()[2], [])  # every required template line is now explained
        # a changed CSS property name must change the identity
        self.assertNotEqual(langs.analyze("t.html", src.replace("color", "colour")).identity, a.identity)

    # Round trip for the smaller formats: each sample parses, commenting every required line keeps its identity and
    # gives full coverage, and a small real change alters the identity.
    def test_css_xml_toml_req(self):
        for name, src, change in (  # sample name, source and a code change, one per format
                ("a.css", "a {\n  color: red;\n}\n", ("red", "blue")),  # CSS rule; changing the colour must be detected
                # PowerShell format XML with a declaration; a text change detected
                ("a.ps1xml", "<?xml version=\"1.0\"?>\n<A>\n  <B x=\"1\">t</B>\n</A>\n", ("t</B>", "u</B>")),
                # TOML table and array; a changed array value detected
                ("pyproject.toml", "[p]\nname = \"x\"\nlist = [\n  \"a\",\n]\n", ('"a"', '"b"')),
                # requirements file; swapping a package name detected
                ("requirements.txt", "numpy\npillow>=9\n", ("numpy", "scipy"))):
            a = langs.analyze(name, src)  # analyse the untouched sample of this format
            self.assertTrue(a.ok, (name, a.error))  # the sample must parse; show name and error otherwise
            out = comment_everything(a, src)  # comment every required line of the sample
            b = langs.analyze(name, out)  # analyse the commented version of this sample
            self.assertTrue(b.ok, (name, b.error, out))  # the commented sample must still parse
            self.assertEqual(a.identity, b.identity, (name, out))  # comments alone leave the identity untouched
            self.assertEqual(b.coverage()[2], [], (name, out))  # every required line of the sample is explained
            # the deliberate code change produces a different identity
            self.assertNotEqual(langs.analyze(name, src.replace(*change)).identity, a.identity, name)


# Comment-map parsing and application: syntax errors, out-of-range lines, wrapping, refused text and long trailing
# comments.
class Maps(unittest.TestCase):
    # Map syntax errors, an out-of-range entry, wrapping of a long comment, origin tracking and refusal of unsafe text.
    def test_errors_and_wrapping(self):
        src = "x = 1\ny = (2 +\n     3)\n"  # two statements, the second spread over two lines
        a = langs.analyze("m.py", src)  # analysis giving the placement rules for the sample
        ents, errs = cmap.read_entries([])  # an empty file list yields no entries and no errors
        self.assertEqual(ents, [])  # nothing at all is returned for an empty file list
        with tempfile.TemporaryDirectory() as td:  # a throwaway folder for the map file
            f = Path(td) / "m.py.cmt"  # the map file, named as the loop would name it
            # Map with a 300-character comment, an out-of-range line, a valid end comment, a malformed line and an
            # author note.
            f.write_text("1^ " + "word " * 60 + "\n9^ out of range\n3> closes the sum\nnot an entry\n;; a note\n",
                         encoding="utf-8")  # written as UTF-8 text like real comment maps
            ents, errs = cmap.read_entries([f])  # parse the map from the temporary file
        self.assertEqual(len(ents), 3)  # three valid entries; the note is ignored
        self.assertEqual(len(errs), 1)  # the malformed line is the single syntax error
        out, origin, aerrs = cmap.apply(src, a, ents)  # insert the parsed entries into the sample source
        self.assertEqual(len(aerrs), 1)              # line 9 is outside the file
        lines = out.split("\n")  # the output split into lines for inspection
        self.assertTrue(all(len(x) <= cmap.WIDTH for x in lines))  # wrapping keeps every line within the width limit
        self.assertGreater(sum(1 for x in lines if x.startswith("# word")), 1)    # wrapped
        self.assertEqual(origin[lines.index("x = 1")], 1)  # the original first line still maps to baseline line 1
        # a double hyphen in an XML comment must be refused
        bad = cmap.apply("<a>\n</a>\n", langs.analyze("t.xml", "<a>\n</a>\n"), [cmap.Entry(1, "^", "x -- y", "t")])
        self.assertTrue(bad[2])  # the XML refusal is reported as an apply error
        # a placeholder in a template comment must be refused too
        bad = cmap.apply(src, a, [cmap.Entry(1, "^", "uses {{title}}", "t")], template=True)
        self.assertTrue(bad[2])  # the template marker refusal is reported as well

    # An end comment that would push its line past 120 characters is placed above the line instead.
    def test_long_trailing_moves_above(self):
        src = "value = compute_something(argument_one, argument_two)\n"  # a single statement of about fifty characters
        a = langs.analyze("m.py", src)  # analysis giving that line's placement rules
        # a trailing comment of over one hundred characters
        out, _, errs = cmap.apply(src, a, [cmap.Entry(1, ">", "a long explanation " * 6, "t")])
        self.assertFalse(errs)  # moving the comment is not treated as an error
        self.assertTrue(out.startswith("# a long explanation"))  # the comment now comes first, above the code


# The whole sealed loop on a tiny temporary repository: seal, measure, decide, apply, revert and the command line.
class Loop(unittest.TestCase):
    # Create a throwaway repository with a Python module, a stylesheet, a package marker and a test script, plus the run
    # folder path and the test command.
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())  # fresh temporary folder for this test
        self.repo = self.td / "repo"  # the fake repository folder inside the temporary one
        (self.repo / "pkg").mkdir(parents=True)  # the package folder, parents included
        # a two-line function for the tests to comment
        (self.repo / "pkg" / "a.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        # a three-line stylesheet with one rule
        (self.repo / "pkg" / "b.css").write_text("p {\n  margin: 0;\n}\n", encoding="utf-8")
        # the repository's own test: fails if add() ever changes
        (self.repo / "test_ok.py").write_text("import pkg.a as m\nassert m.add(2, 3) == 5\n", encoding="utf-8")
        (self.repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")  # empty file that makes pkg a package
        self.run_dir = self.td / "run"  # run directory, created later by sealing
        self.tests = [[sys.executable, "test_ok.py"]]  # the one test command, run with this interpreter

    # Delete the temporary folder after each test.
    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)  # remove everything, ignoring files still in use

    # Write a comment map for one file of the fake repository. rel is the file's relative path and text the map content.
    def write_map(self, rel, text):
        p = self.run_dir / "maps" / (rel + ".cmt")  # map path mirroring the file path, with .cmt appended
        p.parent.mkdir(parents=True, exist_ok=True)  # create the map's folders as needed
        p.write_text(text, encoding="utf-8")  # write the map as UTF-8 text

    # Judge the latest measurement like `code decide`: apply the loop's rules, record the change and append to the log.
    # change describes the round; returns the log entry.
    def decide(self, change):
        sealed = loop.load_sealed(self.run_dir)  # the sealed rubric of this run, hash verified
        # the newest measurement file, decoded
        m = json.loads(sorted(self.run_dir.glob("measure-*.json"))[-1].read_text(encoding="utf-8"))
        e = loop.decide(sealed, m, loop.read_log(self.run_dir))  # the loop's verdict given the history so far
        e["change"] = change  # label the round with its change description
        loop.append_log(self.run_dir, e)  # record the decision in the run's log
        return e  # the verdict entry, returned for the assertions

    # Full cycle: seal, baseline measurement, a map that explains a.py, apply and promote; then a hand-made code change
    # that must be caught, vetoed and reverted.
    def test_seal_apply_measure_decide_revert(self):
        # seal the fake repository with its test command, silently
        man = cm.seal_run(self.run_dir, self.repo, tests=self.tests, log=lambda *a: None)
        self.assertEqual(sorted(f["path"] for f in man["files"]),  # all four code files were discovered and sealed
                         # the expected relative paths, sorted
                         ["pkg/__init__.py", "pkg/a.py", "pkg/b.css", "test_ok.py"])
        with self.assertRaises(loop.LoopError):  # a second seal of the same run folder must be refused
            cm.seal_run(self.run_dir, self.repo, log=lambda *a: None)          # never re-sealed
        cm.measure(self.run_dir, "baseline", log=lambda *a: None)  # baseline measurement before any comment is added
        # the first measurement is promoted as the reference
        self.assertEqual(self.decide("baseline")["decision"], "promote")
        # map: a block above line 1 and an end comment on line 2
        self.write_map("pkg/a.py", "1^ Add two numbers and give back their sum, for the tests to check.\n"
                                   "2> the sum of both arguments, whatever their numeric type\n")
        res = cm.check(self.run_dir, ["pkg/a.py"])["pkg/a.py"]  # dry run of that map for a.py, writing nothing
        # identity kept, no errors, nothing left uncovered
        self.assertTrue(res["identity_ok"] and not res["errors"] and not res["uncovered"])
        cm.apply(self.run_dir, log=lambda *a: None)  # write the commented a.py into the fake repository
        text = (self.repo / "pkg" / "a.py").read_text(encoding="utf-8")  # read back the commented module
        self.assertIn("# Add two numbers", text)  # the block comment was inserted above the def
        self.assertIn("return a + b  # the sum", text)  # the end comment was appended after two spaces
        # measure again after applying the map
        m = json.loads(cm.measure(self.run_dir, "a.py", log=lambda *a: None).read_text(encoding="utf-8"))
        self.assertEqual(m["metrics"]["identity_ratio"], 1.0)  # the code is unchanged in every sealed file
        self.assertEqual(m["metrics"]["tests_pass"], 1.0)  # the repository's test still passes
        self.assertGreater(m["metrics"]["coverage"], 0)  # some required lines are now explained
        self.assertEqual(self.decide("a.py")["decision"], "promote")  # coverage rose, so this round is promoted
        # a change to the code itself is caught and vetoed, and revert restores the last state
        self.write_map("pkg/b.css", "2^ No space around paragraphs, so the layout controls all spacing itself.\n")
        cm.apply(self.run_dir, ["pkg/b.css"], log=lambda *a: None)  # comment the stylesheet as a second applied round
        # sabotage: change the code of a.py by hand
        (self.repo / "pkg" / "a.py").write_text(text.replace("a + b", "a - b"), encoding="utf-8")
        # measure the broken state of the repository
        m = json.loads(cm.measure(self.run_dir, "broken", log=lambda *a: None).read_text(encoding="utf-8"))
        self.assertLess(m["metrics"]["identity_ratio"], 1.0)  # the identity check notices the changed code
        self.assertEqual(m["metrics"]["tests_pass"], 0.0)  # the repository's test now fails
        e = self.decide("broken")  # judge the broken measurement with the loop rules
        self.assertEqual(e["decision"], "veto")  # the round must be vetoed rather than promoted
        self.assertTrue(any("gate failed" in r for r in e["reasons"]))  # at least one gate failed, named in the reasons
        # undo the last apply, which replaced only b.css (a.py stays sabotaged)
        cm.revert(self.run_dir, log=lambda *a: None)
        # the stylesheet comment is gone again
        self.assertNotIn("/* No space", (self.repo / "pkg" / "b.css").read_text(encoding="utf-8"))

    # A map whose text would break the file must make apply refuse and leave the repository untouched.
    def test_apply_refuses_bad_maps(self):
        cm.seal_run(self.run_dir, self.repo, log=lambda *a: None)  # seal the fake repository without test commands
        # a stylesheet end comment containing the star-slash closer
        self.write_map("pkg/b.css", "2> ends with */ which would close the comment\n")
        with self.assertRaises(loop.LoopError):  # apply must raise instead of writing anything
            cm.apply(self.run_dir, log=lambda *a: None)  # attempt to apply every file that has a map
        # the stylesheet is byte-for-byte unchanged
        self.assertEqual((self.repo / "pkg" / "b.css").read_text(encoding="utf-8"), "p {\n  margin: 0;\n}\n")

    # The command line end to end: seal, show and check through `python -m emblem_rsi code`.
    def test_cli(self):
        env_py = [sys.executable, "-m", "emblem_rsi", "code"]  # the base command: this interpreter running the package
        # seal through the CLI, run from the repository root
        r = subprocess.run(env_py + ["seal", "--dir", str(self.run_dir), "--repo", str(self.repo)], cwd=str(REPO),
                           # capture output as UTF-8 text for assertions
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)  # sealing succeeds; show stderr otherwise
        # show the flags of a.py through the command line
        r = subprocess.run(env_py + ["show", "--dir", str(self.run_dir), "pkg/a.py"], cwd=str(REPO),
                           capture_output=True, text=True, encoding="utf-8")  # same capture settings as the seal call
        self.assertIn("    1 R^> | def add(a, b):", r.stdout)  # line 1 needs a comment and allows both placements
        # check a.py, which has no map yet, via the CLI
        r = subprocess.run(env_py + ["check", "--dir", str(self.run_dir), "pkg/a.py"], cwd=str(REPO),
                           capture_output=True, text=True, encoding="utf-8")  # captured as UTF-8 text once again
        self.assertEqual(r.returncode, 1)  # an incomplete check exits with code 1
        self.assertIn("line     1 needs a comment", r.stdout)  # the unexplained def line is listed


# The audit tool's own fixes, found when the tool's code was itself commented: the echo test, the long-line rule, the
# placeholder rule, the seal order and three identity gaps (regex literals, HTML entities, TOML dates).
class ToolFixes(unittest.TestCase):
    # An end-of-line comment is compared with the code before it, not with a line that already contains its own words.
    def test_trailing_comment_echo_uses_code_only(self):
        src = "total = price * count  # price times count gives the order total in cents\n"  # explains the unit
        a = langs.analyze("e.py", src)  # one code line with a trailing comment
        unit = cm.comment_units(a)[0]  # the trailing comment as a quality unit
        self.assertEqual(unit["target"], "total = price * count")  # the target is the code part only
        self.assertFalse(cm.is_echo(unit))  # "cents" and "order" are not in the code, so this is no echo
        # a comment made only of the line's own names is still an echo
        echo = cm.comment_units(langs.analyze("e.py", "total = price * count  # total price count\n"))[0]
        self.assertTrue(cm.is_echo(echo))  # every content word is a name from the code

    # Only lines a comment pushed past the width count; a code line that was already long does not.
    def test_long_lines_count_only_what_comments_caused(self):
        long_code = "x = " + "1 + " * 40 + "1\n"  # one statement well over 120 characters on its own
        base = langs.analyze("l.py", "y = 2\n" + long_code)  # baseline: a short line and the long line
        # commented version: the short line gets a 120-character comment, the long line a short one
        cur = langs.analyze("l.py", "y = 2  # " + "w" * 120 + "\n" + long_code.rstrip("\n") + "  # explains it\n")
        self.assertEqual(cm.made_long(base, cur), 1)  # only the short line that its comment pushed over counts

    # Upper-case tags are placeholders; prose that names the --todo option is not.
    def test_placeholder_rule_is_case_sensitive(self):
        self.assertTrue(cm.is_placeholder("TODO explain this later"))  # a real to-do tag
        self.assertFalse(cm.is_placeholder("--todo lists only the lines that still need a comment"))  # option name

    # A baseline that does not parse is refused before the rubric is sealed, so sealing can simply be retried.
    def test_seal_refuses_unparsable_baseline_without_sealing(self):
        td = Path(tempfile.mkdtemp())  # throwaway repository and run folder
        try:  # the folders are removed afterwards whatever happens
            (td / "repo").mkdir()  # repository with one broken Python file
            (td / "repo" / "bad.py").write_text("def broken(:\n", encoding="utf-8")  # a syntax error
            with self.assertRaises(loop.LoopError):  # the seal must be refused
                cm.seal_run(td / "run", td / "repo", log=lambda *a: None)  # seal the broken repository, silently
            self.assertFalse((td / "run" / "rubric.sealed.json").exists())  # and nothing was sealed
        finally:  # runs after the assertions, passed or failed
            shutil.rmtree(td, ignore_errors=True)  # remove the temporary folders

    # Scripts are tokenized by node, so this test is skipped without it.
    @unittest.skipUnless(HAVE_NODE, "node not installed")
    # A changed regular expression in a script changes the identity (acorn hands regex values over as objects).
    def test_regex_literals_enter_the_identity(self):
        a = langs.analyze("r.js", "var r = /ab+c/g;\n")  # a script with one regex literal
        b = langs.analyze("r.js", "var r = /ab*c/g;\n")  # the same script with a different pattern
        self.assertTrue(a.ok and b.ok, (a.error, b.error))  # both parse
        self.assertNotEqual(a.identity, b.identity)  # the pattern change is detected

    # Swapping one HTML entity for another changes the identity.
    def test_html_entities_enter_the_identity(self):
        a = langs.analyze("p.html", "<p>a &amp; b</p>\n")  # an ampersand entity
        b = langs.analyze("p.html", "<p>a &lt; b</p>\n")  # a less-than entity instead
        self.assertNotEqual(a.identity, b.identity)  # the entity swap is detected
        c = langs.analyze("p.html", "<p>a &#8212; b</p>\n")  # a numeric reference
        self.assertNotEqual(c.identity, langs.analyze("p.html", "<p>a &#8211; b</p>\n").identity)  # code point changed

    # TOML dates and times are valid data and must not make the file "not parse".
    def test_toml_dates_parse(self):
        a = langs.analyze("pyproject.toml", "[p]\nreleased = 2026-10-28\nat = 07:32:00\n")  # a date and a time
        self.assertTrue(a.ok, a.error)  # the file is accepted
        b = langs.analyze("pyproject.toml", "[p]\nreleased = 2026-10-29\nat = 07:32:00\n")  # one day later
        self.assertNotEqual(a.identity, b.identity)  # and the changed date is detected


if __name__ == "__main__":  # run the suites when the file is executed directly
    unittest.main()  # unittest discovers and runs every test class above
