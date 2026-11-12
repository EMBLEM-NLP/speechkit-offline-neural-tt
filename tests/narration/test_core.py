"""Fast tests for the toolkit core (no narration, no rendering)."""
import json  # reads the generated docmodel and request files and writes project.json
import tempfile  # throwaway folders that are deleted after each test
import unittest  # standard library test framework (python -m unittest)
from pathlib import Path  # Path objects for the temporary project folders

from speechkit_narrate.audit.autofix import arpabet_to_voice, r_variants  # phoneme helpers of the pronunciation autofix
# project loading, its error type and project creation
from speechkit_narrate.project import Project, ProjectError, create
from speechkit_narrate.text import docmodel, markup  # the book.md parser and the docmodel/request builder

# A miniature book.md that uses every block kind once: a numbered chapter title, a paragraph with curly quotes and a
# dash, a quote, two verse lines, a bullet, a numbered item, a table header and row, a note and a section. The last
# paragraph is full of spoken-form edge cases: a scripture reference, a clock time, contractions, "2 Kings" and capitals
# used for emphasis.
BOOK = """# 1. First chapter

A paragraph with “quotes” — and a dash.

> A quoted line.

| Verse line one,

| verse line two.

- A bullet

* 1. A numbered item

TABLE Month | Task

ROW March | Feed the soil.

NOTE A note.

## A section

Psalm 23:1 says it. The meeting is at 10:30. I’ll go; we’ll see. 2 Kings is a book. NOT now. SHOUTING words.
"""


# Create a minimal project inside the temporary folder td, with BOOK as work/book.md and a short project.json. extra is
# an optional dict merged into the settings. Returns the loaded Project; files are written only inside td.
def make_project(td, extra=None):
    root = Path(td) / "p"  # project folder inside the temporary directory
    (root / "work").mkdir(parents=True)  # work/ is the folder where book.md lives
    (root / "work" / "book.md").write_text(BOOK, encoding="utf-8")  # the test book, saved as UTF-8
    # settings: title, subtitle, no input file, plus any extras
    cfg = {"title": "My Test Book", "subtitle": "A subtitle", "input": "", **(extra or {})}
    # project.json; json.dumps escapes non-ASCII, so the default encoding is safe here
    (root / "project.json").write_text(json.dumps(cfg))
    return Project(root)  # loading the project fills in every default setting


# Tests of the Project class: defaults, derived values, the missing-project error and project creation. Each test works
# in its own temporary folder.
class ProjectTests(unittest.TestCase):
    # A project with only a title and subtitle gets a slug and every default section. Paths come from the PATHS table.
    def test_defaults_and_derived(self):
        with tempfile.TemporaryDirectory() as td:  # fresh temporary folder for this test
            p = make_project(td)  # build the miniature project from the test book
            self.assertEqual(p.slug, "my-test-book")  # slug: the title in lowercase with hyphens
            self.assertEqual(p.subtitle, "A subtitle")
            self.assertEqual(p["voice"]["profile"], "ljspeech-high")  # a default section filled in
            self.assertEqual(p["export"]["format"], "wav")
            # named paths are resolved relative to the project root
            self.assertEqual(p.p("book"), p.root / "work" / "book.md")

    # A folder without project.json must raise ProjectError. The CLI reports that error as a usage problem instead of a
    # traceback.
    def test_missing_project(self):
        with tempfile.TemporaryDirectory() as td:  # an empty temporary folder without project.json
            with self.assertRaises(ProjectError):  # expect exactly this error type
                Project(td)  # loading a folder that has no settings file

    # create() copies the input file into input/ and records it in project.json. The new project is therefore
    # self-contained.
    def test_create_copies_input(self):
        with tempfile.TemporaryDirectory() as td:  # temporary area for source and project
            src = Path(td) / "book.txt"  # a one-line plain-text book outside the project
            src.write_text("Hello.\n")  # plain ASCII, so the default encoding is fine
            p = create(Path(td) / "proj", src, title="Hello Book")  # make a new project from it in a subfolder
            self.assertEqual(p.input_kind(), "text")  # a .txt input is treated as text, not as a request
            # the input was copied into the project's input folder
            self.assertTrue((p.root / "input" / "book.txt").exists())


# Tests of the book.md parser and validator. They need no project folder, only text.
class MarkupTests(unittest.TestCase):
    # Every block kind is recognized and the cover is block 0. The book validates without problems, and dump() followed
    # by parse() gives the same blocks (a lossless round trip).
    def test_parse_kinds(self):
        kinds = [b["kind"] for b in markup.parse(BOOK, "T")]  # block kinds in parse order, cover first
        # cover first, then one block per book.md block
        self.assertEqual(kinds, ["cover", "h1", "p", "quote", "verse", "verse", "bullet", "num", "th", "tr", "note", "h2", "p"])
        self.assertEqual(markup.validate(BOOK), [])  # no problems reported for the test book
        # dump writes the blocks back as book.md text; parsing that again must give identical blocks
        self.assertEqual(markup.parse(markup.dump(markup.parse(BOOK, "T")), "T"), markup.parse(BOOK, "T"))

    # A ROW block without a TABLE header before it must be reported. The message has to mention ROW so the author can
    # find it.
    def test_validate_row_without_header(self):
        # some problem message names the ROW block
        self.assertTrue(any("ROW" in p for p in markup.validate("# A\n\nROW a | b\n")))


# Tests of the docmodel builder: spoken forms of tokens and pronunciation overrides. Both build a real docmodel in a
# temporary project.
class DocmodelTests(unittest.TestCase):
    # Build the docmodel for the miniature book and check the spoken form of tricky tokens. Also checks that the two
    # verse lines are joined into one narration segment.
    def test_spoken_forms(self):
        with tempfile.TemporaryDirectory() as td:  # temporary project area for this test
            p = make_project(td)  # fresh project with the test book
            info = docmodel.build(p)  # writes work/docmodel.json and work/request.json and returns counts
            # read back; no encoding given, so on Windows this UTF-8 file would be decoded with the ANSI code page
            dm = json.loads(p.p("docmodel").read_text())
            # display text -> spoken text for every token (duplicate display texts collapse, harmless here)
            sp = {t["disp"]: t["spoken"] for t in dm["tokens"]}
            # Each assertion pairs a displayed token with the text the voice is given.
            self.assertEqual(sp["23:1"], "23 verse 1")         # after a Bible book name
            self.assertEqual(sp["10:30."], "10:30.")           # a time stays a time
            self.assertEqual(sp["I’ll"], "I'll")               # apostrophe kept
            self.assertEqual(sp["“quotes”"], "quotes,")        # quotes stripped, dash -> comma pause
            # the em dash itself is silent; its pause went to the previous word as a comma
            self.assertEqual(sp["—"], "")
            self.assertEqual(sp["2"], "Second")  # "2" before "Kings" is read as the ordinal "Second"
            self.assertEqual(sp["NOT"], "not")  # the emphasis word NOT is read as a word, not spelled out
            # ALL-CAPS words of five or more letters are lowercased so they are read normally
            self.assertEqual(sp["SHOUTING"], "shouting")
            req = json.loads(p.p("request").read_text())  # the narration request, with the same encoding caveat
            # the segment whose display text starts with the first verse line
            verse = [s for s in req["segments"] if s["text"].startswith("Verse")][0]
            self.assertIn("verse line two.", verse["spokenText"])   # verse lines joined into one sentence
            # the build reports one count per narration segment
            self.assertEqual(info["segments"], len(req["segments"]))

    # Pronunciation overrides from work/pronunciations.json: a word override with raw [[phonemes]] and a token override
    # by id. The spoken form changes while spoken_ref keeps the reference reading the audit compares against.
    def test_overrides(self):
        with tempfile.TemporaryDirectory() as td:  # temporary area for the override test
            p = make_project(td)  # fresh project with the test book
            # word override: "paragraph" gets Piper raw phonemes
            p.p("pronunciations").write_text(json.dumps({"overrides": {"paragraph": "[[pˈaɹəɡɹaf]]"},
                                                         # token override for token 2; ids are JSON object keys, hence
                                                         # the string "2"
                                                         "tokens": {"2": "Ay"}}))
            docmodel.build(p)  # rebuild the docmodel with the overrides in place
            dm = json.loads(p.p("docmodel").read_text())  # read the rebuilt docmodel back
            t = {x["disp"]: x for x in dm["tokens"]}  # display text -> token record
            # raw phonemes are used exactly as written, without capitalization
            self.assertEqual(t["paragraph"]["spoken"], "[[pˈaɹəɡɹaf]]")
            # spoken_ref keeps what a listener should hear, for the audit
            self.assertEqual(t["paragraph"]["spoken_ref"], "paragraph")
            # token 2 is "Book" in the cover title (tokens 0 to 2 are "My Test Book"), now spoken "Ay"
            self.assertEqual(dm["tokens"][2]["spoken"], "Ay")


# Tests of the ARPAbet conversion and the r-free variants used by the autofix. They are pure functions, so no files are
# involved.
class AutofixHelpers(unittest.TestCase):
    # CMU ARPAbet converts to the voice's British espeak symbols. There is no r before a consonant or at the end of a
    # word, unstressed ER becomes schwa, and stress marks precede vowels.
    def test_arpabet_to_voice(self):
        # "doorkeeper": AO1 R becomes ˈɔː without r, and the final ER0 becomes ə
        self.assertEqual(arpabet_to_voice("D AO1 R K IY2 P ER0"), "dˈɔːkˌiːpə")
        # Boaz: stressed OW1 gives ˈəʊ, secondary AE2 gives ˌa
        self.assertEqual(arpabet_to_voice("B OW1 AE2 Z"), "bˈəʊˌaz")
        # "star": AA before R is the long ɑː, and the final R is dropped
        self.assertEqual(arpabet_to_voice("S T AA1 R"), "stˈɑː")

    # r_variants offers the final schwa as ɐ and the long ɑː (not before ɹ) as short ɒ. Both are spellings the voice
    # does not colour with an r.
    def test_r_variants(self):
        self.assertIn("ˈɑːnɐ", r_variants("ˈɑːnə"))  # final ə replaced by ɐ, so "Ana" is no longer heard as "Arna"
        self.assertIn("sˈɒm", r_variants("sˈɑːm"))  # long ɑː replaced by short ɒ in a word ending in m


if __name__ == "__main__":  # run the tests when the file is executed directly
    unittest.main()  # unittest discovers the TestCase classes in this module
