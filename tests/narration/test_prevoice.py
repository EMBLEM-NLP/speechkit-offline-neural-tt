"""The prevoice stage and its public API in emblem_rsi.pron, on synthetic sentences only (no book text).

    python3 -m unittest tests.narration.test_prevoice

espeak is replaced by FakeEspeak (a fixed table), so no voice runtime is needed; tests that use the real spaCy
tagger are skipped when it is not installed.
"""
# Store type hints as unevaluated strings, so `str | None` works on every supported Python.
from __future__ import annotations

import hashlib  # compares files byte for byte through their SHA-256
import json  # project.json, pronunciations.json and the stage's own files
import os  # the current folder, to prove the word audit writes nothing there
import re  # splits fake phrases into words
import sys  # puts this checkout first on the import path
import tempfile  # throwaway project folders
import unittest  # plain unittest, like the rest of the suite
from pathlib import Path  # project paths
from unittest import mock  # replaces the spaCy loader to simulate a machine without it

REPO = Path(__file__).resolve().parents[2]  # the repository root, two folders above tests/narration/
sys.path.insert(0, str(REPO))  # this checkout ahead of any installed copy

from speechkit_narrate import registry as R  # noqa: E402
from speechkit_narrate.audit import prevoice as pv  # noqa: E402
from speechkit_narrate.project import Project, ProjectError  # noqa: E402
from speechkit_narrate.text import docmodel  # noqa: E402
from emblem_rsi.pron import heteronyms as het, inventory, lexicon, tagger, wordaudit  # noqa: E402

FIXTURE = REPO / "tests" / "narration" / "fixtures" / "prevoice" / "sentences.json"  # synthetic sentences with expected roles

# What the fake espeak says for single words: heteronyms get the reading real espeak "en" gives them out of context.
WORDS = {"use": "jˈuːs", "the": "ðə", "tools": "tˈuːlz", "before": "bɪfˈɔː", "you": "juː", "begin": "bɪɡˈɪn",
         "we": "wiː", "record": "ɹˈɛkɔːd", "every": "ˈɛvɹi", "step": "stˈɛp", "shows": "ʃˈəʊz", "a": "ɐ",
         "steady": "stˈɛdi", "climb": "klˈaɪm", "date": "dˈeɪt", "then": "ðˈɛn", "close": "klˈəʊs", "jar": "dʒˈɑː",
         "contents": "kˈɒntɛnts", "that": "ðat", "matter": "mˈatə", "come": "kˈʌm", "first": "fˈɜːst", "to": "tə",
         "it": "ɪt", "well": "wˈɛl", "used": "jˈuːzd", "path": "pˈɑːθ", "tests": "tˈɛsts", "had": "hˈad"}
# Whole texts the fake reads differently from its words: sentences where espeak picks the verb itself, and the carrier
# phrases fix_phonemes() tries ("to use" makes espeak say the verb).
PHRASES = {"We record every step.": "wiː ɹɪkˈɔːd ˈɛvɹi stˈɛp.", "to use": "tə jˈuːz", "to record": "tə ɹɪkˈɔːd",
           "to close": "tə klˈəʊz", "the record": "ðə ɹˈɛkɔːd", "the use": "ðə jˈuːs"}
# The voice's phoneme set as far as these tests need it (a real voice config has about 150 symbols).
SYMBOLS = set("abdefhijklmnoprstuvwzɐɑɒɔəɛɜɡɪʃʊʌʒθðŋɹˈˌːʲ") | {"tʃ", "dʒ"}

# A synthetic book.md: each paragraph tests one decision of the stage.
BOOK = """# 1. Tests

Use the tools before you begin.

We record every step.

The record shows a steady climb.

Record the date, then close the jar.

Contents that matter come first.

A well-used path.
"""


# A stand-in for lexicon.espeak: the same call shape (texts, voices) -> {voice: [phonemes]}, from the tables above.
# Raw [[...]] blocks pass through, as Piper's bridge passes them; unknown words read as their letters.
class FakeEspeak:
    def __init__(self):  # starts with no calls counted
        self.calls = 0  # how many times it was asked, like lexicon.ESPEAK_CALLS

    # Phonemizes one text: a known phrase as a whole, otherwise word by word with punctuation kept at the end.
    def one(self, text: str) -> str:
        if text in PHRASES:  # a text with its own reading
            return PHRASES[text]  # as real espeak would say it in that context
        out = []  # the phonemes of each word
        for w in text.split():  # word by word
            raw = re.fullmatch(r"\[\[(.*)\]\](\W*)", w)  # a raw phoneme block (a fix) with trailing punctuation
            if raw:  # passed through unchanged, like Piper's bridge does
                out.append(raw.group(1) + raw.group(2))  # the phonemes and the punctuation
                continue  # next word
            core, tail = re.fullmatch(r"(.*?)(\W*)", w).groups()  # the word and its trailing punctuation
            out.append(WORDS.get(core.lower(), core.lower()) + tail)  # the table's reading, or the letters
        return " ".join(out)  # words separated by spaces, as espeak writes them

    # The lexicon.espeak call shape: every requested voice gets the same fake reading.
    def __call__(self, texts, voices=("en",)):
        self.calls += 1  # one more "helper process"
        return {v: [self.one(t) for t in texts] for v in voices}  # one reading per text, per voice


# A stand-in tagger with the emblem_rsi.pron.tagger interface: VB unless the word follows an article, with fixed tags
# for chosen words. by_word maps a lowercase word to its tag.
class FakeTagger:
    def __init__(self, by_word=None):  # by_word: lowercase word -> Penn tag
        self.by_word = by_word or {}  # words with a fixed tag

    # One tag per span: the fixed tag, NN after "the" or "a", VB otherwise.
    def tags(self, text, spans):
        out = []  # tags in span order
        for start, end in spans:  # each word's place in the text
            word = text[start:end].lower()  # the word itself
            before = text[:start].split()[-1:] or [""]  # the word before it, if any
            out.append(self.by_word.get(word) or ("NN" if before[0].lower() in ("the", "a") else "VB"))  # its tag
        return out  # one tag per span

    role = staticmethod(tagger.role)  # the real tag -> role mapping

    # How the decisions file names this tagger.
    def describe(self):
        return "fake tagger"  # recorded in decisions.json


# Makes a project folder with the synthetic book. root is an empty folder; pron is pronunciations.json's content (None
# for no file); prevoice is the project's prevoice section (None to leave the defaults). Returns the Project.
def make_project(root: Path, pron=None, prevoice=None, book=BOOK) -> Project:
    cfg = {"title": "Prevoice Test"}  # the minimum project.json
    if prevoice is not None:  # the stage's settings
        cfg["prevoice"] = prevoice  # merged over the defaults
    (root / "project.json").write_text(json.dumps(cfg), encoding="utf-8")  # the project's settings
    (root / "work").mkdir()  # the work folder
    (root / "work" / "book.md").write_text(book, encoding="utf-8")  # the book text the script reads
    if pron is not None:  # hand or loop fixes
        # written as the loop writes it: UTF-8, IPA kept as characters
        (root / "work" / "pronunciations.json").write_text(json.dumps(pron, ensure_ascii=False), encoding="utf-8")
    return Project(root)  # opened with the defaults merged in


# Token id of the n-th (0-based) token whose display text is disp, in a built docmodel.
def token_of(project, disp: str, n: int = 0) -> int:
    toks = json.loads(project.p("docmodel").read_text(encoding="utf-8"))["tokens"]  # every token of the book
    return [t["i"] for t in toks if t["disp"] == disp][n]  # the wanted occurrence


# The spoken token texts of request.json by token id.
def spoken(project) -> dict:
    req = json.loads(project.p("request").read_text(encoding="utf-8"))  # what the voice is given
    return {i: t for s in req["segments"] for i, t in zip(s["tokenIndex"], s["tokens"], strict=True)}  # id -> text


# SHA-256 of a file, to compare outputs byte for byte.
def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()  # the whole file


# The shared word splitter (inventory.piece_words) and token_words.
class Splitter(unittest.TestCase):
    # Each word keeps its token id, its place in the token, its clause flag and its offsets, and a hyphenated token
    # gives two words of the same token.
    def test_piece_words(self):
        # five tokens with global ids 7 to 11; the fourth ends a clause, the third holds two words
        pw = inventory.piece_words([(7, "Record"), (8, "the"), (9, "well-used"), (10, "path,"), (11, "then")])
        self.assertEqual([w.norm for w in pw], ["record", "the", "well", "used", "path", "then"])  # keys in order
        self.assertEqual([(w.token, w.sub) for w in pw][2:4], [(9, 0), (9, 1)])  # two words of one token
        self.assertEqual([w.punct_after for w in pw], [False, False, False, False, True, False])  # after "path,"
        text = "Record the well-used path, then"  # the reference tokens joined by spaces
        self.assertTrue(all(text[w.start:w.end] == w.surface for w in pw))  # offsets point at each word

    # An overridden token gives its whole override to its first word, as the inventory always did.
    def test_override_voiced_forms(self):
        self.assertEqual(inventory.token_words("Use,", "[[jˈuːz]],"), [("Use", "use", "[[jˈuːz]]")])  # one word
        self.assertEqual([v for _, _, v in inventory.token_words("well-used", "[[x]]")], ["[[x]]", ""])  # tail empty

    # The same heteronym twice in one sentence is found twice and judged at each position (the prototype's
    # words.index() always judged the first one).
    def test_word_twice_in_one_sentence(self):
        toks = [{"spoken": s, "spoken_ref": s} for s in "We record the record.".split()]  # tokens 0..3
        pcs = het.pieces(toks, [("s0000", [0, 1, 2, 3])])  # one segment, one piece
        occ = het.judge(pcs, het.find(pcs), "v1")  # both occurrences, judged
        self.assertEqual([(o["token"], o["expected"]) for o in occ], [(1, "verb"), (3, "noun-adj")])  # each its own


# lexicon.explain_role / expected_role, versions 1 and 2, on the synthetic sentences of the fixture.
class Rules(unittest.TestCase):
    # Every fixture sentence gets the role it lists, under both versions; expected_role() matches explain_role().
    def test_fixture_cases(self):
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]  # the synthetic sentences
        for c in cases:  # each case
            toks = c["text"].split()  # one token per word
            pw = inventory.piece_words(list(enumerate(toks)))  # the shared splitter
            words, punct = [w.norm for w in pw], [w.punct_after for w in pw]  # keys and clause flags
            self.assertEqual(words[c["at"]], c["word"], c["text"])  # the fixture points at the heteronym
            for v in (1, 2):  # both rule sets
                role, why = lexicon.explain_role(words, c["at"], c["word"], punct, version=v)  # with its reason
                self.assertEqual(role, c[f"v{v}"], f"v{v}: {c['text']} ({why})")  # the listed role
                self.assertEqual(lexicon.expected_role(words, c["at"], c["word"], punct, version=v), role)  # wrapper
            # the default is version 1, unchanged
            self.assertEqual(lexicon.expected_role(words, c["at"], c["word"], punct), c["v1"])

    # Version 2 only adds opinions where version 1 has none, never changes one; an unknown version is refused.
    def test_v2_extends_v1(self):
        words = "the record shows a steady climb".split()  # a sentence with a noun reading
        self.assertEqual(lexicon.expected_role(words, 1, "record", version=2), "noun-adj")  # v1's verdict kept
        self.assertTrue(lexicon.explain_role(words, 1, "record")[1].startswith("v1:"))  # and named as v1's
        with self.assertRaises(ValueError):  # a typo must not silently mean version 1
            lexicon.expected_role(words, 1, "record", version=3)  # no version 3 exists

    # The role of a CMU variant, including the tense heteronym "read"; and the in-context reading of an occurrence.
    def test_variant_and_context_reading(self):
        self.assertEqual(lexicon.variant_role("use", "Y UW1 Z"), "verb")  # to use
        self.assertEqual(lexicon.variant_role("use", "Y UW1 S"), "noun-adj")  # a use
        self.assertEqual(lexicon.variant_role("read", "R EH1 D"), "past")  # red
        self.assertEqual(lexicon.variant_role("read", "R IY1 D"), "present")  # reed
        self.assertIsNone(lexicon.variant_role("wind", "W IH1 N D"))  # no rule tells wind's readings apart
        self.assertIn("record", lexicon.ROLE_WORDS)  # a public set of judgeable words
        # stress decides which variant espeak used
        self.assertEqual(lexicon.context_reading("record", "ɹɪkˈɔːd", ["R EH1 K ER0 D", "R IH0 K AO1 R D"]),
                         ("R IH0 K AO1 R D", "verb"))  # the second variant: the verb
        self.assertEqual(lexicon.context_reading("record", "", ["R EH1 K ER0 D"]), (None, None))  # nothing heard


# The optional spaCy adapter.
class Tagger(unittest.TestCase):
    # Penn tags map to roles; "read" has its own past/present mapping.
    def test_role_mapping(self):
        # verb tags, noun and adjective tags, and tags that decide nothing
        self.assertEqual([tagger.role(t, "use") for t in ("VB", "VBZ", "NNS", "JJ", "IN", None)],
                         ["verb", "verb", "noun-adj", "noun-adj", None, None])  # verbs, nouns/adjectives, nothing
        # "read": past tense and participle against everything else
        self.assertEqual([tagger.role(t, "read") for t in ("VBD", "VBN", "VBP", "NN")],
                         ["past", "past", "present", "present"])  # red for the past and the participle

    # Without spaCy the adapter reports itself unavailable, tags nothing, and the stage's rule falls back to the rule
    # alone; the toolkit never needs spaCy.
    def test_without_spacy(self):
        with mock.patch.object(tagger, "_load", return_value=None):  # as on a machine without spaCy
            self.assertFalse(tagger.available())  # nothing to use
            self.assertEqual(tagger.tags("Use the tools.", [(0, 3)]), [None])  # no tag for any word
            self.assertEqual(tagger.describe(), "not installed (rules only)")  # what the decisions file says

    # With spaCy installed, the model loads once and tags each word by its own character offsets.
    @unittest.skipUnless(tagger.available(), "spaCy or en_core_web_sm is not installed")
    def test_with_spacy(self):  # runs only where the model loads
        self.assertIs(tagger._load(), tagger._load())  # one pipeline per process
        text = "We record the record."  # the same word twice, with two readings
        self.assertEqual(tagger.tags(text, [(3, 9), (14, 20)]), ["VBP", "NN"])  # each occurrence by its own offset


# The word audit: rows, warnings, and output only where asked.
class WordAudit(unittest.TestCase):
    # Raw phonemes against the fix, missing symbols, the kind of fix, and no file unless out is given.
    def test_rows_and_explicit_output(self):
        fake = {"calm": "kˈɑːm", "tomato": "təmˈɑːtəʊ", "begin": "bɪɡˈɪn", "Be-gin": "bˈiːɡɪn"}  # word -> phonemes

        # The phonemizer's call shape, from the table above.
        def phon(texts, voices):
            return {v: [fake.get(t, "") for t in texts] for v in voices}  # same reading for every voice
        # an r-prone vowel removed, a fix that changes nothing, a respelling, and a symbol the voice lacks
        fixes = {"calm": "[[kˈɒm]]", "tomato": "[[təmˈɑːtəʊ]]", "begin": "Be-gin", "zed": "[[zɛʔ]]"}
        before = sorted(os.listdir("."))  # the current folder before the audit
        rows = wordaudit.audit(fixes, SYMBOLS, phonemize=phon)  # no out: rows only
        self.assertEqual(sorted(os.listdir(".")), before)  # nothing was written to the current folder
        by = {r["word"]: r for r in rows}  # rows by word
        self.assertEqual(by["calm"]["kind"], wordaudit.KIND_R)  # the fix removes an r-prone vowel
        self.assertTrue(by["tomato"]["same"])  # a fix that changes nothing
        self.assertEqual((by["begin"]["fix"], by["begin"]["how"]), ("bˈiːɡɪn", "respelling 'Be-gin'"))  # respelled
        self.assertEqual(by["zed"]["symbols_missing"], ["ʔ"])  # the glottal stop is not in the symbol set
        warn = wordaudit.warnings(rows)  # what needs a person's attention
        self.assertTrue(any("tomato" in w for w in warn) and any("zed" in w for w in warn))  # both reported
        with tempfile.TemporaryDirectory() as td:  # an explicit destination
            out = Path(td) / "audit.json"  # where the rows go
            wordaudit.audit(list(fixes.items()), SYMBOLS, phonemize=phon, out=out)  # pairs work as well as a dict
            self.assertEqual(len(json.loads(out.read_text(encoding="utf-8"))), 4)  # written there, as UTF-8


# The stage itself, through the real planner, with espeak faked and narrate replaced by a stub.
class Stage(unittest.TestCase):
    # A registry whose narrate stage records its calls instead of voicing, and whose prevoice stage runs the real
    # pv.run with the fake espeak; both keep the built-in declarations (needs, inputs, settings, satisfied hook).
    def registry(self, project, tagger_obj=None):
        reg = R.load(project)  # the built-in stages
        self.voiced = []  # one entry per narrate run: the request it saw

        # The stub narration: remembers what it was given and writes one timing record so its output exists.
        def narrate(p, ctx):
            self.voiced.append(spoken(p))  # the token texts it would have voiced
            (p.p("speech")).mkdir(parents=True, exist_ok=True)  # the speech folder
            (p.p("speech") / "s0000.wav.json").write_text("{}", encoding="utf-8")  # so outputs are not missing
            return {"voiced": True}  # a stage result
        fake = FakeEspeak()  # one fake espeak per registry
        # the stub keeps every declaration of the built-in narrate except its platform (the stub runs anywhere)
        reg.add_stage(R.Stage("narrate", run=narrate, platforms=("any",), source="test"), replaces=True)
        # The real stage code with the fake espeak injected; needs, inputs, settings and the hook are inherited.
        def prevoice(p, ctx):
            return pv.run(p, log=ctx.log, phonemize=fake, tagger=tagger_obj, symbols=SYMBOLS)  # no runtime needed
        reg.add_stage(R.Stage("prevoice", run=prevoice, platforms=("any",), source="test"), replaces=True)  # anywhere
        # add_stage inherits default-valued fields; explicitly override the merged test stages.
        reg.stages["narrate"].platforms = ("any",)
        reg.stages["prevoice"].platforms = ("any",)
        return reg  # ready for plan() and run()

    # The declarations the coordinator relies on: narrate's inputs and settings are unchanged, it needs prevoice,
    # and prevoice is opt-in, sits between script and narrate, and rebuilds the script.
    def test_declarations(self):
        reg = R.load()  # built-ins only
        names = [s.name for s in reg.topo()]  # the run order
        self.assertEqual(names[names.index("script") + 1], "prevoice")  # right after script
        self.assertEqual(names[names.index("prevoice") + 1], "narrate")  # right before narrate
        n, s, p = reg.stages["narrate"], reg.stages["script"], reg.stages["prevoice"]  # the three stages
        # narrate fingerprints exactly what it did before: request.json and the voice settings
        self.assertEqual((n.inputs, n.settings, n.needs), (("request",), ("voice.profile",), ("script", "prevoice")))
        script_settings = ("title", "subtitle", "author", "language", "spoken", "chapters")  # unchanged too
        self.assertEqual((s.inputs, s.settings), (("book", "pronunciations"), script_settings))  # script as before
        self.assertIn("prevoice", p.settings)  # its own settings are fingerprinted
        self.assertEqual(p.refreshes, ("script",))  # it rewrites the script's outputs consistently
        with tempfile.TemporaryDirectory() as td:  # the default: off
            proj = make_project(Path(td))  # no prevoice section in project.json
            self.assertEqual(proj.cfg["prevoice"], {"enabled": False, "rules": "v1+tagger"})  # off by default
            self.assertEqual(pv.off_reason(proj), "off (project.json prevoice.enabled is false)")  # skipped

    # Off, on, nothing changed, off again: the fixes reach narrate only through request.json, which narrate already
    # fingerprints; run() runs narrate again each time request.json changes and never downgrades it then.
    def test_fixes_reach_narrate_through_request(self):
        with tempfile.TemporaryDirectory() as td:  # a throwaway project
            p = make_project(Path(td))  # the synthetic book, prevoice off (the default)
            reg = self.registry(p)  # stub narrate, fake espeak, rules alone (no tagger)
            quiet = []  # the run log
            r = R.run(p, reg, targets=["narrate"], log=quiet.append)  # off: script and narrate only
            self.assertEqual(r["ran"], ["script", "narrate"])  # prevoice was skipped by its hook
            step = {s["stage"]: s for s in r["steps"]}["prevoice"]  # its plan step
            self.assertEqual(step["action"], "skip")  # never run while off
            self.assertEqual(step["reasons"], ["off (project.json prevoice.enabled is false)"])  # and says why
            self.assertFalse((p.root / pv.DIR).exists())  # off writes nothing at all
            plain = sha(p.p("request"))  # the script as it is built without the stage
            fp_off = R.fingerprint(p, reg.stages["narrate"])["inputs"]["request"]  # what narrate fingerprints

            p.save_settings({"prevoice": {"enabled": True}})  # turn it on (default rules, no tagger: rules alone)
            r = R.run(p, reg, targets=["narrate"], log=quiet.append)  # prevoice, then narrate
            self.assertEqual(r["ran"], ["prevoice", "narrate"])  # narrate ran because request.json changed
            self.assertNotEqual(R.fingerprint(p, reg.stages["narrate"])["inputs"]["request"], fp_off)  # it did
            use = token_of(p, "Use")  # the imperative "Use" espeak reads as the noun
            self.assertEqual(self.voiced[-1][use], "[[jˈuːz]]")  # narrate was given the verb's phonemes
            self.assertEqual(spoken(p)[token_of(p, "record", 0)], "record")  # "We record": espeak is right, kept
            fixes = json.loads((p.root / pv.FIXES).read_text(encoding="utf-8"))  # the fixes file
            self.assertEqual(fixes["tokens"][str(use)], {"word": "use", "say": "[[jˈuːz]]"})  # carrier "to use"
            dec = json.loads((p.root / pv.DECISIONS).read_text(encoding="utf-8"))  # the decisions file
            row = next(o for o in dec["occurrences"] if o["token"] == use)  # the "Use" decision
            # espeak reads the noun, the sentence wants the verb, so it is fixed
            self.assertEqual((row["reading"], row["expected"], row["decision"]), ("noun-adj", "verb", "fix"))
            self.assertIn("tagger not installed, rule alone", row["reason"])  # no tagger was given
            self.assertEqual(row["fix_from"], "espeak in 'to use'")  # espeak's own verb reading
            self.assertTrue((p.root / pv.AUDIT).exists())  # the word audit, in work/prevoice only

            r = R.run(p, reg, targets=["narrate"], log=quiet.append)  # nothing changed
            self.assertEqual(r["ran"], [])  # everything is up to date

            p.save_settings({"prevoice": {"enabled": False}})  # turn it off again
            r = R.run(p, reg, targets=["narrate"], log=quiet.append)  # prevoice cleans up, narrate re-voices
            self.assertEqual(r["ran"], ["prevoice", "narrate"])  # once more, to take the fixes out
            self.assertFalse((p.root / pv.FIXES).exists())  # the fixes file is gone
            self.assertEqual(sha(p.p("request")), plain)  # the script is byte-identical to the plain build again
            self.assertEqual(self.voiced[-1][use], "Use")  # and narrate was given the plain word
            steps = {s["stage"]: s["action"] for s in R.plan(p, reg, targets=["narrate"])}  # and from now on
            self.assertEqual((steps["prevoice"], steps["narrate"]), ("skip", "skip"))  # nothing to do

    # When the stage changes nothing in request.json, run() downgrades narrate as for any unchanged input: the
    # request is the one path by which the fixes reach the voice.
    def test_no_fix_means_no_revoice(self):
        with tempfile.TemporaryDirectory() as td:  # a book whose only heteronym espeak already reads right
            p = make_project(Path(td), book="# 1. Tests\n\nWe record every step.\n")  # verb, read as the verb
            reg = self.registry(p)  # stub narrate, fake espeak
            R.run(p, reg, targets=["narrate"], log=lambda *_: None)  # off: narrate once
            p.save_settings({"prevoice": {"enabled": True}})  # on
            r = R.run(p, reg, targets=["narrate"], log=lambda *_: None)  # prevoice runs, finds nothing to fix
            self.assertEqual(r["ran"], ["prevoice"])  # narrate was not run again
            step = {s["stage"]: s for s in r["steps"]}["narrate"]  # narrate's step
            self.assertEqual(step["reasons"], ["inputs unchanged after upstream ran"])  # downgraded, as designed
            self.assertEqual(len(self.voiced), 1)  # voiced once in all

    # pronunciations.json always wins: a token it fixes and a word it fixes everywhere are never touched, and the
    # rule set's tagger must confirm the rule (the "Contents that ..." noun is not turned into a verb).
    def test_priority_and_tagger_agreement(self):
        with tempfile.TemporaryDirectory() as td:  # a throwaway project
            root = Path(td)  # its folder
            p = make_project(root)  # built once without fixes, to learn the token ids
            docmodel.build(p)  # the plain script
            rec = token_of(p, "Record")  # "Record the date": an imperative espeak reads as the noun
            hand = {"overrides": {"close": "[[klˈəʊz]]"}, "tokens": {str(rec): "[[ɹɪkˈɔːd]]"}}  # a word and a token fix
            (root / "work" / "pronunciations.json").write_text(json.dumps(hand), encoding="utf-8")  # as by hand
            p.save_settings({"prevoice": {"enabled": True, "rules": "v1+tagger"}})  # on, tagger must agree
            tg = FakeTagger({"contents": "NNS"})  # a tagger that reads "Contents" as a plural noun
            res = pv.run(p, log=lambda *_: None, phonemize=FakeEspeak(), tagger=tg, symbols=SYMBOLS)  # the stage
            rows = json.loads((root / pv.DECISIONS).read_text(encoding="utf-8"))["occurrences"]  # every decision
            dec = {(o["token"], o["word"]): o for o in rows}  # by token and word
            self.assertEqual(dec[(rec, "record")]["decision"], "keep")  # the hand token fix wins
            self.assertIn("pronunciations.json fixes this token", dec[(rec, "record")]["reason"])  # and says so
            close = token_of(p, "close")  # "then close the jar"
            self.assertIn("fixes this word everywhere", dec[(close, "close")]["reason"])  # the word fix wins
            cont = dec[(token_of(p, "Contents"), "contents")]  # the clause-initial noun
            # the v1 rule says verb, the tagger says noun: not confirmed, so not fixed
            self.assertEqual((cont["rule"], cont["tag_role"], cont["decision"]), ("verb", "noun-adj", "skip"))
            self.assertIn("does not confirm it", cont["reason"])  # no fix without the tagger's agreement
            used = dec[(token_of(p, "well-used"), "used")]  # a hyphenated token
            self.assertIn("not one plain word", used["reason"])  # never replaced by a per-token fix
            self.assertEqual(res["fixes"], 1)  # only "Use" is fixed
            self.assertEqual(res["kept_by_pronunciations"], 2)  # the two hand fixes were left alone
            self.assertEqual(spoken(p)[rec], "[[ɹɪkˈɔːd]]")  # the request carries the hand fix
            self.assertEqual(spoken(p)[close], "[[klˈəʊz]]")  # and the word fix
            self.assertEqual(res["tagger"], "fake tagger")  # the tagger that decided is named

    # A fixes file made from another book.md is never applied: token ids may have moved.
    def test_stale_fixes_are_ignored(self):
        with tempfile.TemporaryDirectory() as td:  # a throwaway project
            p = make_project(Path(td), prevoice={"enabled": True})  # on
            pv.run(p, log=lambda *_: None, phonemize=FakeEspeak(), tagger=None, symbols=SYMBOLS)  # fixes for this book
            use = token_of(p, "Use")  # the fixed token
            self.assertEqual(docmodel.load_prevoice(p)[use], ("use", "[[jˈuːz]]"))  # current: applied
            with open(p.p("book"), "a", encoding="utf-8") as f:  # the book is edited
                f.write("\nOne more line.\n")  # ids after this point could move
            self.assertEqual(docmodel.load_prevoice(p), {})  # stale: not applied
            docmodel.build(p)  # the script stage would now build without them
            self.assertEqual(spoken(p)[use], "Use")  # until the stage runs again

    # A wrong rule-set name is a project error that lists the valid ones; an unknown mode is refused by the engine.
    def test_bad_rules_setting(self):
        with tempfile.TemporaryDirectory() as td:  # a throwaway project
            p = make_project(Path(td), prevoice={"enabled": True, "rules": "v9"})  # a typo
            with self.assertRaises(ProjectError) as cm:  # stopped before anything is written
                pv.run(p, log=lambda *_: None, phonemize=FakeEspeak(), tagger=None, symbols=SYMBOLS)  # the typo
            self.assertIn("v1+tagger", str(cm.exception))  # the message names the valid sets
        with self.assertRaises(ValueError):  # the engine checks too
            het.judge([], [], "v9")  # no such mode


# fix_phonemes(): espeak's own reading from a carrier phrase, else the CMU variant in the voice's symbols.
class FixPhonemes(unittest.TestCase):
    def test_carrier_then_dictionary(self):  # two readings found in carriers, one only in the dictionary
        made = het.fix_phonemes([("use", "verb"), ("record", "noun-adj"), ("contents", "verb")],  # wanted readings
                                phonemize=FakeEspeak(), to_voice=lambda a: "<" + a + ">")  # a visible converter
        self.assertEqual(made[("use", "verb")], ("jˈuːz", "espeak in 'to use'"))  # the verb from "to use"
        self.assertEqual(made[("record", "noun-adj")], ("ɹˈɛkɔːd", "espeak in 'the record'"))  # the noun
        ph, how = made[("contents", "verb")]  # no carrier makes the fake say the verb
        self.assertTrue(how.startswith("CMU ") and ph.startswith("<"))  # so the dictionary variant is used


if __name__ == "__main__":  # python3 tests/narration/test_prevoice.py
    unittest.main()  # run every test in this file
