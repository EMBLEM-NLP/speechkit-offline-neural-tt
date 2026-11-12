"""Tests of the pronunciation audit page (speechkit_narrate/audit/report.py): its plain copy for viewers that run no scripts,
and the helpers that write it.

    python3 -m unittest tests.narration.test_audit_report        (from the repository root)

The page's script builds the intro, the tiles, the word list, the loop cards and the credits line from embedded data.
The iPhone and iPad Files app previews a page without running its scripts, so report.py also writes that content into
the page as plain HTML, character for character what the script would draw. These tests build the page from made-up
data, then a browser compares what the page shows with scripts off against what it shows with scripts on.
Set SPEECHKIT_SKIP_BROWSER=1 to skip the browser checks.
"""
# lets annotations stay unevaluated strings
from __future__ import annotations

import json  # reads the data back out of the built page
import os  # environment variable that can switch off the browser checks
import re  # finds the marks in the template and the script element in the built page
import sys  # puts the repository root on the import path
import tempfile  # a scratch folder for the built page
import unittest  # the built-in test framework: TestCase classes and assertions
from pathlib import Path  # builds the repository root path and the page's file path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the repository root, so `speechkit-voice-tts-offline-pipeline` can be imported
from speechkit_narrate.audit import report  # noqa: E402  the module under test

# Playwright checks run unless SPEECHKIT_SKIP_BROWSER is set to 1
BROWSER = os.environ.get("SPEECHKIT_SKIP_BROWSER") != "1"

# Report data with every kind of row and loop: four categories, one and many occurrences, none/one/three chapters,
# phonemes and none, a note and none, a kept row; a promoted loop and a vetoed one (with reasons, a whole-number score
# written as a float and no change text). The word error rates include 0.00125, whose percentage is an exact tie.
DATA = {
    "rows": [  # the report rows, one of every kind the page can show
        # row 1: a word both recognizers got wrong, seen 12 times in three chapters (the page shows two)
        {"cat": "heard", "word": "heron", "n": 12, "where": ["Morning at the harbour", "Nets", "A third chapter"],
         "problem": "Heard wrong by both recognizers", "cause": "heard as &ldquo;herring&rdquo;, &ldquo;heroin&rdquo;",
         "fix": "spelled &ldquo;hair-un&rdquo;", "fix_ipa": None,  # its fix is a respelling, with no phonemes
         "before": "1/12 and 0/12 heard right (&ldquo;herring&rdquo;)", "after": "12/12 and 12/12 heard right",
         "status": "fixed", "note": ""},
        # row 2: an r that is not in the spelling, fixed with phonemes, in one chapter
        {"cat": "r", "word": "idea", "n": 1, "where": ["Nets"], "problem": "An r that is not in the spelling",
         "cause": "F3 1980 Hz in 3 of 4", "fix": "phonemes", "fix_ipa": "aɪ dˈiə",
         "before": "F3 1980 Hz", "after": "F3 2650 Hz", "status": "better", "note": ""},
        # row 3: a reading that is wrong in context, found in no chapter
        {"cat": "context", "word": "close", "n": 2, "where": [], "problem": "Wrong reading for the sentence",
         "cause": "read as verb, the sentence needs the adjective: &ldquo;Stay close to the shore&rdquo;",
         "fix": "none yet", "fix_ipa": "", "before": "2 wrong", "after": "2 wrong", "status": "listen", "note": ""},
        # row 4: a dictionary word that was kept as it is, with a note
        {"cat": "dictionary", "word": "don't", "n": 4, "where": ["Nets"], "problem": "Wrong word spoken",
         # its cause, and a fix of "kept" with no phonemes
         "cause": "the apostrophe was dropped", "fix": "kept", "fix_ipa": None,
         "before": '<span class="ipa">/dˈoʊnt/</span>', "after": "-", "status": "kept",
         "note": "Dictionary: dˈoʊnt, dˈoʊn"},
    ],
    "loops": [  # the loop history: a promoted loop and a vetoed one
        # loop 1: promoted, with a change text and no reasons
        {"loop": 1, "decision": "promote", "score": 0.8731, "change": "baseline", "reasons": [],
         # its four measurements, one count written as a float
         "rows": {"asr_wer": 0.0258, "asr_confirmed_words": 12.0, "lexicon_flagged_words": 9}},
        # loop 2: vetoed, with a whole-number score written as a float and no change text
        {"loop": 2, "decision": "veto", "score": 1.0, "change": None,
         "reasons": ["word error rate rose", "unit tests &amp; browser check failed"],  # why it was vetoed
         # its measurements, with the error rate that is an exact tie
         "rows": {"asr_wer": 0.00125, "asr_confirmed_words": 3, "lexicon_flagged_words": 0}},
    ],
    "first": {"asr_confirmed_words": 12, "asr_wer": 0.0258},  # the figures of the first measurement
    # the figures of the last measurement, one count written as a float
    "last": {"asr_confirmed_words": 3.0, "asr_wer": 0.5},
    # the book's totals, and the first and last counts of wrong readings in context
    "words": 15441, "unique": 3200, "segments": 1234, "het_first": 7, "het_last": 0,
}


# The small formatting helpers, which must give what the page's script gives.
class Helpers(unittest.TestCase):
    # Numbers print the way JavaScript prints them inside a template string.
    def test_js_prints_numbers_like_the_script(self):
        self.assertEqual(report._js(12.0), "12")  # a whole number stored as a float loses its ".0"
        self.assertEqual(report._js(0.8731), "0.8731")  # other floats keep their digits
        self.assertEqual(report._js(7), "7")  # integers are unchanged
        self.assertEqual(report._js(None), "null")  # a missing value prints as null
        self.assertEqual(report._js("x"), "x")  # text is unchanged

    # Percentages: up to two decimals, trailing zeros and a bare point removed, halves rounded upward.
    def test_pct_rounds_and_trims(self):
        # the fractions and the percentage text the script gives for each
        cases = {0.0258: "2.58%", 0.5: "50%", 0.0: "0%", 1.0: "100%", 0.02: "2%", 0.025: "2.5%", 0.00005: "0.01%",
                 0.00125: "0.13%"}  # 0.125 is exact in binary, so the tie must go up (Python's format gives 0.12)
        for x, want in cases.items():  # each fraction and the text the script gives
            self.assertEqual(report._pct(x), want, x)  # the helper must give exactly that text
        self.assertEqual(report._pct(None), "0%")  # a missing measurement counts as zero, as in the script

    # Whole numbers get thousands separators; a missing number is a dash.
    def test_fmt_groups_thousands(self):
        # the numbers go through the helper, and the list below is what the script gives
        self.assertEqual([report._fmt(n) for n in (0, 999, 1000, 15441, 1234567)],
                         ["0", "999", "1,000", "15,441", "1,234,567"])  # grouped in threes
        self.assertEqual(report._fmt(None), "-")  # no number gives a dash

    # One word row: the count's noun, the first two chapters, optional phonemes and note, the label and the badge.
    def test_row_markup(self):
        many, one, plain, kept = (report._row(r) for r in DATA["rows"])  # heron, idea, close, don't
        self.assertIn("12 times &middot; Morning at the harbour; Nets</div>", many)  # plural, and only two chapters
        self.assertNotIn("A third chapter", many)  # the third chapter is cut
        self.assertIn("1 time &middot; Nets</div>", one)  # singular
        self.assertIn('<span class="ipa">/aɪ dˈiə/</span>', one)  # the fix's phonemes
        self.assertNotIn('class="ipa"', plain)  # an empty phoneme string adds nothing
        self.assertIn('<div class="sub">2 times</div>', plain)  # no chapters, so no separator
        self.assertNotIn('class="note"', many)  # an empty note adds nothing
        self.assertIn('<div class="note">Dictionary: ', kept)  # a note is shown under the cause
        self.assertIn('<span class="cat">Heard wrong</span>', many)  # the category label
        self.assertIn('<span class="badge s-fixed">Fixed</span>', many)  # the status badge
        self.assertIn('<span class="badge s-kept">Kept</span>', kept)  # the badge of a kept row
        # a count of zero, which the script writes in the plural as well
        zero = report._row({**DATA["rows"][1], "n": 0})
        # "0 times": only a count of exactly one is singular
        self.assertIn('<div class="sub">0 times &middot; Nets</div>', zero)

    # One loop card: number and decision, the change (empty when missing), four measurements and the reasons.
    def test_loop_markup(self):
        promoted, vetoed = (report._loop(lp) for lp in DATA["loops"])  # the two cards, in the order of the loop history
        self.assertIn('<div class="n">1<small class="promote">promote</small></div>', promoted)  # number and decision
        self.assertIn("<b>baseline</b>", promoted)  # the change text
        self.assertIn("<span>score <b>0.8731</b></span><span>WER <b>2.58%</b></span>", promoted)  # score and WER
        self.assertIn("misheard words <b>12</b>", promoted)  # a float 12.0 prints as 12
        self.assertNotIn("<ul>", promoted)  # a promoted loop has no reasons
        self.assertIn("<b></b>", vetoed)  # no change text
        self.assertIn("<span>score <b>1</b></span>", vetoed)  # a score of 1.0 prints as 1
        # the reasons are one bullet list, with the ampersand already escaped
        self.assertIn("<ul><li>word error rate rose</li><li>unit tests &amp; browser check failed</li></ul>", vetoed)


# The page built from the data.
class PageCopy(unittest.TestCase):
    # Builds the fragment and the standalone page once.
    @classmethod
    def setUpClass(cls):  # runs once, before the tests of this class
        cls.fragment = report.render_page(DATA, "Fish & Chips")  # the title needs escaping
        cls.page = report.standalone_page(cls.fragment)  # the page a browser opens from disk

    # The template holds one mark for each container the copy fills, and the copy has nothing else to fill.
    def test_template_marks_match_the_copy(self):
        text = report.TEMPLATE.read_text(encoding="utf-8")  # the shipped template
        marks = re.findall(r"<!--__STATIC_([A-Z]+)__-->", text)  # the marks, in page order
        self.assertEqual(sorted(marks), sorted(report.static_copy(DATA)))  # one mark per piece of copy, no extras

    # Nothing is left unfilled: no marks, no data placeholder, no title placeholder.
    def test_nothing_is_left_unfilled(self):
        for needle in ("__STATIC_", "/*__DATA__*/", "__TITLE"):  # the three kinds of placeholder
            self.assertNotIn(needle, self.fragment, needle)  # all are replaced
        self.assertIn("Fish &amp; Chips Pronunciation Audit</title>", self.fragment)  # the title is escaped
        # and so is the title in the page's heading
        self.assertIn("Fish &amp; Chips &middot; narration audit", self.fragment)

    # The script still gets the data, and it parses back to what was given.
    def test_script_data_is_intact(self):
        m = re.search(r"const D = (\{.*?\});\n", self.fragment, re.S)  # the object literal the script reads
        self.assertIsNotNone(m)  # found
        self.assertEqual(json.loads(m.group(1)), DATA)  # and it is the made-up data

    # Every word, every loop and every tile is in the copy, in the order of the data.
    def test_copy_has_every_row_and_loop(self):
        copy = report.static_copy(DATA)  # the copy of the full data
        # the place of each word's markup in the list
        positions = [copy["LIST"].index(f'<div class="word">{r["word"]}</div>') for r in DATA["rows"]]
        self.assertEqual(positions, sorted(positions))  # rows keep the report's order
        self.assertEqual(copy["LOOPS"].count('<div class="loop">'), 2)  # both loop cards
        self.assertEqual(copy["STATS"].count('<div class="stat">'), 6)  # six tiles
        # the totals of the intro, written with thousands separators
        self.assertIn("15,441 spoken words (3,200 different words, 1,234 narration segments)", copy["LEDE"])
        # three rows were wrong (the kept one does not count), in 12 + 1 + 2 places
        self.assertIn("3 words or readings were wrong in 15 places", copy["LEDE"])
        self.assertIn("<b>12 → 3</b>", copy["STATS"])  # first and last confirmed words, 3.0 printed as 3
        self.assertIn("<b>2.58% → 50%</b>", copy["STATS"])  # first and last word error rate
        self.assertIn("<b>7 → 0</b>", copy["STATS"])  # heteronym suspects before and after
        self.assertIn("<b>13</b><span>occurrences fixed</span>", copy["STATS"])  # the fixed and improved rows: 12 + 1

    # A report with no rows says so instead of showing a bare header; no loops give no cards.
    def test_empty_report(self):
        data = dict(DATA, rows=[], loops=[])  # nothing found, nothing looped
        copy = report.static_copy(data)  # the copy of the data without rows or loops
        # the list is the header and then the "No words match." message
        self.assertEqual(copy["LIST"], report.HEAD + '<div class="empty">No words match.</div>')
        self.assertEqual(copy["LOOPS"], "")  # no cards
        # the intro says that nothing was wrong
        self.assertIn("0 words or readings were wrong in 0 places", copy["LEDE"])

    # The standalone page has the copy in its body, and its head holds the style and the title.
    def test_standalone_page_puts_the_copy_in_the_body(self):
        head, body = self.page.split("<body>", 1)  # the two halves
        self.assertTrue(head.startswith("<!doctype html>"))  # a complete document
        self.assertIn("<title>Fish &amp; Chips Pronunciation Audit</title>", head)  # the title is in the head
        self.assertIn('<div class="word">heron</div>', body)  # the word list is in the body
        self.assertIn(report.CREDITS, body)  # so is the credits line


# What a browser shows.
@unittest.skipUnless(BROWSER, "browser checks skipped")  # skipped when SPEECHKIT_SKIP_BROWSER is set to 1
class PageInBrowser(unittest.TestCase):  # the checks that need a real browser
    # The page with scripts off shows what the script builds when scripts are on, and the formatting helpers agree
    # with the browser's own number formatting.
    def test_copy_equals_what_the_script_builds(self):
        from playwright.sync_api import sync_playwright  # the same browser driver the reader checks use
        # a script that returns the text of the five containers
        grab = """() => Object.fromEntries(["lede", "stats", "list", "loops", "foot"].map(
            id => [id, document.getElementById(id).innerHTML.replace(/\\s+/g, " ").trim()]))"""  # whitespace-normalised
        with tempfile.TemporaryDirectory() as tmp:  # a scratch folder for the page
            path = Path(tmp) / "Pronunciation_Audit.html"  # the file a browser opens
            # the standalone page, written to a file that a browser can open
            path.write_text(report.standalone_page(report.render_page(DATA, "Test Book")), encoding="utf-8")
            with sync_playwright() as pw:  # starts and later stops the Playwright driver
                br = pw.chromium.launch()  # the bundled Chromium, no options needed
                try:  # the browser is closed even when an assertion fails
                    size = {"width": 390, "height": 844}  # a phone viewport
                    # 1. scripts off, as in the iPhone Files preview
                    off = br.new_context(java_script_enabled=False, viewport=size)  # a context that runs no scripts
                    # no web fonts: the test is offline
                    off.route(re.compile(r"^https?://"), lambda route: route.abort())
                    page = off.new_page()  # one tab in it
                    page.goto(path.as_uri())  # nothing on the page can run
                    plain = page.evaluate(grab)  # what the containers hold
                    # the copy is on screen: the word list, the loop cards and the method cards below them
                    shown = page.evaluate("""() => {
                        const on = e => !!e && e.getClientRects().length > 0;
                        const q = s => document.querySelector(s);
                        return {rows: document.querySelectorAll('#list .row').length,
                                loops: document.querySelectorAll('#loops .loop').length,
                                tiles: document.querySelectorAll('#stats .stat').length,
                                list: on(q('#list .row:nth-child(2)')), method: on(q('.cols .card')),
                                chips: q('#chips').children.length, text: document.body.innerText};
                    }""")  # the end of the script that measures what the page shows
                    off.close()  # done with the first context
                    # 2. scripts on: the script draws its own version over the copy
                    ctx = br.new_context(viewport=size)  # a normal context
                    ctx.route(re.compile(r"^https?://"), lambda route: route.abort())  # offline here too
                    page = ctx.new_page()  # one tab in it
                    page.goto(path.as_uri())  # the script runs
                    page.wait_for_selector("#chips .chip")  # the filter buttons mean the script has finished
                    built = page.evaluate(grab)  # what the containers hold now
                    # the browser's own percentages and thousands separators for the helpers' test values
                    fractions = [0, 0.00005, 0.00125, 0.005, 0.0125, 0.02, 0.025, 0.0258, 0.1, 0.1234567, 0.375, 0.5, 1]
                    # the numbers that are formatted with thousands separators
                    numbers = [0, 7, 999, 1000, 15441, 1234567]
                    js_pct = page.evaluate(  # the browser's own version of the percentages
                        # the script that formats them, and the fractions it is given
                        "(xs) => xs.map(x => (100 * x).toFixed(2).replace(/\\.?0+$/, '') + '%')", fractions)
                    # the browser's own version of the thousands separators
                    js_fmt = page.evaluate("(ns) => ns.map(n => n.toLocaleString('en-US'))", numbers)
                    ctx.close()  # done with the second context
                finally:  # runs whether the checks passed or failed
                    br.close()  # always shut the browser down
        for key in plain:  # each of the five containers
            self.assertEqual(plain[key], built[key], key)  # the copy and the script's version are the same
        # header + 4 rows, 2 cards, 6 tiles
        self.assertEqual((shown["rows"], shown["loops"], shown["tiles"]), (1 + 4, 2, 6))
        self.assertTrue(shown["list"] and shown["method"], shown)  # the list and the cards below take up room on screen
        self.assertEqual(shown["chips"], 0)  # the filter needs a script, so it is not there
        text = shown["text"].lower()  # innerText follows the style sheet's upper-casing, so compare in lower case
        self.assertIn("heron", text)  # a word of the list is in the text on screen
        self.assertIn("heard back by two listeners", text)  # and so is the method section
        self.assertEqual([report._pct(x) for x in fractions], js_pct)  # the percentages match the browser's
        self.assertEqual([report._fmt(n) for n in numbers], js_fmt)  # and so do the thousands separators


# Lets the file be run directly as a script.
if __name__ == "__main__":
    unittest.main()  # runs every test in this module
