"""Regression tests for the fixes made while speechkit_narrate was refactored out of the larger toolkit.

    python3 -m unittest tests.narration.test_narration_fixes        (from the repository root)

Each test fails on the behaviour before the fix and passes now. Nothing here needs the voice runtime, espeak, ffmpeg
or the network: narration records and audio are written by the tests themselves (short WAVs made with the wave
module), and the helpers that would start the voice are replaced.
"""
from __future__ import annotations  # annotation hints stay strings

import asyncio  # runs the MCP server's async handlers directly
import hashlib  # checks the manifest's SHA-256 values
import json  # project.json, request.json, speech records, manifests
import math  # the test tone written into the fake segment WAVs
import os  # SPEECHKIT_RUNTIME decides whether the real-voice test runs
import shutil  # removes the scratch folders
import struct  # packs the fake WAV samples
import subprocess  # runs the real-voice test through the command line
import sys  # puts the checkout first on the import path
import tempfile  # a scratch folder per test
import unittest  # the test framework
import wave  # writes the fake segment WAVs and reads the exported chapters
from pathlib import Path  # paths of the scratch projects
from types import SimpleNamespace  # stands in for the audit modules in the report test
from unittest import mock  # replaces helpers that would start the voice runtime or ffmpeg

REPO = Path(__file__).resolve().parents[2]  # the repository root, two folders above tests/narration/
sys.path.insert(0, str(REPO))  # this checkout ahead of any installed copy

from speechkit_narrate import audio, ingest, registry as R  # noqa: E402
from speechkit_narrate.ingest import clean  # noqa: E402
from speechkit_narrate.project import DEFAULTS, Project, ProjectError, create, deep_merge  # noqa: E402
from speechkit_narrate.text import codenames, docmodel, markup  # noqa: E402
from speechkit_narrate.voice import narrate as nar  # noqa: E402

from speechkit_narrate.voice.identity import seal_record, digest, SCHEMA, sha256

RATE = 22050


def fake_identity(project):
    components = {"testOnly": True, "profileSha256": sha256(nar.env.profile_path(project.cfg["voice"]["profile"]))}
    return {"schemaVersion": SCHEMA, "components": components, "sha256": digest(components)}  # the voice's sample rate, which export insists on


def quiet(*_):
    """A log function that discards every message."""


# Base class: a scratch folder per test, removed afterwards, and helpers that make small projects in it.
class _Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="speechkit-voice-tts-offline-pipeline-fixes-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # A project made with create() from a file holding text. name is the input's file name (its extension decides
    # the input kind); settings are merged into project.json. Returns the Project.
    def project(self, text: str, name: str = "book.txt", title: str = "Test Text", settings=None) -> Project:
        src = self.tmp / name
        src.write_text(text, encoding="utf-8")
        return create(self.tmp / ("p-" + src.stem), src, title=title, settings=settings)

    # A project whose work/book.md is `book` and whose script stage output (docmodel.json, request.json) is built.
    def scripted(self, book: str, title: str = "Test Text", settings=None) -> Project:
        p = self.project(book, title=title, settings=settings)
        p.ensure("book")
        p.p("book").write_text(book, encoding="utf-8")
        docmodel.build(p)
        return p


# ------------------------------------------------------------------------------------------- project
class ProjectSettings(_Scratch):
    # A null section in project.json ("voice": null) keeps that section's defaults; before the fix the merged
    # settings held None and Project() (or the first stage that read voice.profile) failed.
    def test_null_section_keeps_defaults(self):
        self.assertEqual(deep_merge(DEFAULTS, {"voice": None})["voice"], DEFAULTS["voice"])
        root = self.tmp / "nulls"
        root.mkdir()
        (root / "project.json").write_text(json.dumps({"title": "Nulls", "voice": None, "spoken": None}),
                                           encoding="utf-8")
        p = Project(root)
        self.assertEqual(p["voice"]["profile"], DEFAULTS["voice"]["profile"])
        self.assertEqual(p["spoken"]["table_reading"], "cells")
        self.assertIsNone(deep_merge({"a": 1}, {"a": None})["a"])  # a null over a plain value still replaces it

    # save_settings refuses an update that would break the project and leaves project.json as it was on disk (before
    # the fix it was written first and the project could no longer be opened).
    def test_save_settings_refuses_a_breaking_update(self):
        p = self.project("Hello.\n")
        before = (p.root / "project.json").read_bytes()
        with self.assertRaises(ProjectError) as raised:
            p.save_settings({"spoken": 5})
        self.assertIn("spoken", str(raised.exception))
        self.assertEqual((p.root / "project.json").read_bytes(), before)
        self.assertIsInstance(p["spoken"], dict)  # the open project is unchanged too
        Project(p.root)  # and it still opens
        p.save_settings({"spoken": {"years": True}})  # a good update is still saved
        self.assertIs(Project(p.root)["spoken"]["years"], True)

    # create() refuses an unsupported input (a PDF) before the project folder is made.
    def test_create_refuses_an_unsupported_input_first(self):
        src = self.tmp / "book.pdf"
        src.write_bytes(b"%PDF-1.4\n")
        with self.assertRaises(ProjectError) as raised:
            create(self.tmp / "pdf-project", src)
        self.assertIn("unsupported input type", str(raised.exception))
        self.assertFalse((self.tmp / "pdf-project").exists())
        ok = self.tmp / "ok.txt"
        ok.write_text("x", encoding="utf-8")
        with self.assertRaises(ProjectError):  # bad settings are refused before the folder is made, too
            create(self.tmp / "bad-settings", ok, settings={"voice": 3})
        self.assertFalse((self.tmp / "bad-settings").exists())

    # The input kinds: .txt and .md are text, .json is a SpeechKit request.
    def test_input_kinds(self):
        for name, kind in (("a.txt", "text"), ("b.md", "text"), ("c.json", "request")):
            p = self.project("{}" if kind == "request" else "Hello.\n", name=name)
            self.assertEqual(p.input_kind(), kind, name)
            self.assertTrue((p.root / "input" / name).is_file())


# ------------------------------------------------------------------------------------------- registry
class Planner(_Scratch):
    # "all" is a word for force only. Given as from_stage, a target, only or skip it raised a ValueError (or a
    # KeyError) deep inside plan(); now it is a RegistryError, which the CLI and MCP report as a usage problem.
    def test_all_outside_force_is_a_registry_error(self):
        p = self.project("Hello.\n")
        reg = R.load(p)
        for kw in ({"from_stage": "all"}, {"targets": ["all"]}, {"only": ["all"]}, {"skip": ["all"]}):
            with self.assertRaises(R.RegistryError, msg=str(kw)) as raised:
                R.plan(p, reg, **kw)
            self.assertIn("all", str(raised.exception))
        steps = R.plan(p, reg, targets=["check"], force=["all"])  # force=all keeps its meaning
        self.assertEqual({s["stage"]: s["action"] for s in steps}, {"extract": "run", "check": "run"})

    # A stage whose run was killed leaves the "_running" marker run() writes when it starts. Its outputs may be
    # half-made, so the planner must rebuild them instead of adopting them; a finished run removes the marker.
    def test_interrupted_run_is_not_adopted(self):
        p = self.project("Hello there.\n\nSecond paragraph.\n")
        reg = R.load(p)
        r = R.run(p, reg, targets=["check"], accept_draft=True, log=quiet)
        self.assertEqual(r["ran"], ["extract", "check"])
        state = R.read_state(p)
        self.assertFalse(state.get("_running"), state.get("_running"))  # finished runs leave no marker
        self.assertTrue(p.p("book").exists())
        # imitate a project from before tracking: the record goes, the outputs stay; such outputs are adopted
        state.pop("check")
        R.write_state(p, state)
        self.assertEqual({s["stage"]: s["action"] for s in R.plan(p, reg, targets=["check"])}["check"], "adopt")
        # now imitate a killed run instead: the marker is there and nothing was recorded after it
        state.setdefault("_running", {})["check"] = {"at": "2026-01-02T03:04:05", "pid": 1}
        R.write_state(p, state)
        step = {s["stage"]: s for s in R.plan(p, reg, targets=["check"])}["check"]
        self.assertEqual(step["action"], "run")
        self.assertTrue(step["reasons"][0].startswith("last run was interrupted"), step["reasons"])
        self.assertIn("2026-01-02T03:04:05", step["reasons"][0])
        R.run(p, reg, targets=["check"], log=quiet)  # a successful run removes the marker again
        self.assertNotIn("check", R.read_state(p).get("_running", {}))
        self.assertEqual({s["stage"]: s["action"] for s in R.plan(p, reg, targets=["check"])}["check"], "skip")

    # A plug-in stage that is interrupted (KeyboardInterrupt) gets an error record, not a stale marker, and is rerun.
    def test_marker_is_replaced_by_the_error_record(self):
        p = self.project("Hello.\n")
        reg = R.load(p)
        calls = []

        def boom(project, ctx):
            calls.append(1)
            (project.root / "out").mkdir(exist_ok=True)
            (project.root / "out" / "half.txt").write_text("half", encoding="utf-8")
            raise KeyboardInterrupt
        reg.add_stage(R.Stage("half", run=boom, outputs=("out/half.txt",), source="test"))
        with self.assertRaises(KeyboardInterrupt):
            R.run(p, reg, only=["half"], log=quiet)
        state = R.read_state(p)
        self.assertNotIn("half", state.get("_running", {}))
        self.assertIn("half", state["_errors"])
        step = R.plan(p, reg, only=["half"])[0]
        self.assertEqual(step["action"], "run")
        self.assertTrue(step["reasons"][0].startswith("last run failed"))


# ------------------------------------------------------------------------------------------- ingest
class Ingest(_Scratch):
    REQUEST = {"schemaVersion": "1.0.0", "id": "req", "segments": [
        {"id": "h1-intro", "text": "Intro", "spokenText": "Introduction"},
        {"id": "s0001", "text": "Hello there.", "spokenText": "Hello there, friend."},
        {"id": "chapter-2", "text": "Two", "spokenText": "Chapter two"},
        {"id": "s0002", "text": "Goodbye now."},  # no spokenText: its text is read
        {"id": "s0003", "text": "", "spokenText": "  "},  # nothing to say: left out
    ]}

    # A SpeechKit request .json becomes one paragraph per segment of its spokenText (or text); an id starting with
    # "h1-" or "chapter" makes a chapter heading.
    def test_request_input(self):
        p = self.project(json.dumps(self.REQUEST), name="talk.json", title="A Request")
        blocks, ctx = ingest.read_blocks(p)
        self.assertEqual(ctx["kind"], "request")
        self.assertEqual([(b["kind"], b["text"]) for b in blocks],
                         [("h1", "Introduction"), ("p", "Hello there, friend."), ("h1", "Chapter two"),
                          ("p", "Goodbye now.")])
        r = R.run(p, R.load(p), targets=["script"], accept_draft=True, log=quiet)
        self.assertEqual(r["ran"], ["extract", "check", "script"])
        req = json.loads(p.p("request").read_text(encoding="utf-8"))
        self.assertIn("Hello there, friend.", [s["spokenText"] for s in req["segments"]])

    # A malformed request is a ProjectError (a problem the user can fix), not a KeyError or TypeError.
    def test_malformed_request(self):
        for i, bad in enumerate(({"segments": "nope"}, [1, 2], {"segments": [5]}, {"segments": [{"id": "x"}]})):
            p = self.project(json.dumps(bad), name=f"bad{i}.json")
            with self.assertRaises(ProjectError, msg=json.dumps(bad)):
                ingest.run(p)
        p = self.project("{not json", name="broken.json")
        with self.assertRaises(ProjectError):
            ingest.run(p)

    MD = "# Setup\n\nRun the helper first.\n\n```\nfoo_bar = load(path)\nprint(foo_bar)\n```\n\nThen read on.\n"

    # A fenced code block in Markdown is kept as a "code" block that docmodel gives no spoken tokens (the default,
    # ingest.code_blocks "keep"); with "read" its lines become a paragraph that is spoken.
    def test_markdown_code_block(self):
        p = self.project(self.MD, name="guide.md", title="Guide")
        blocks, _ = ingest.read_blocks(p)
        code = [b for b in blocks if b["kind"] == "code"]
        self.assertEqual(len(code), 1)
        self.assertEqual(code[0]["text"], "foo_bar = load(path)\nprint(foo_bar)")
        R.run(p, R.load(p), targets=["script"], accept_draft=True, log=quiet)
        dm = json.loads(p.p("docmodel").read_text(encoding="utf-8"))
        cb = [b for b in dm["blocks"] if b["kind"] == "code"]
        self.assertEqual(len(cb), 1)
        self.assertEqual(cb[0]["tokens"], [])
        spoken = " ".join(s["spokenText"] for s in dm["segments"])
        self.assertNotIn("foo_bar", spoken)
        self.assertIn("Then read on.", spoken)
        p2 = self.project(self.MD, name="guide2.md", title="Guide", settings={"ingest": {"code_blocks": "read"}})
        blocks, _ = ingest.read_blocks(p2)
        self.assertFalse([b for b in blocks if b["kind"] == "code"])
        self.assertTrue(any(b["kind"] == "p" and "foo_bar" in b["text"] for b in blocks))

    # A text_fixes replacement holding a backslash is inserted literally ("C:\new" stayed "C:" + newline + "ew" when
    # the value was used as a regex replacement template).
    def test_text_fix_with_backslash(self):
        log = []
        out = clean.apply_text_fixes([{"kind": "p", "text": "Copy teh file."},
                                      {"kind": "tr", "cells": ["teh", "x"]}], {"teh": "C:\\new"}, log)
        self.assertEqual(out[0]["text"], "Copy C:\\new file.")
        self.assertEqual(out[1]["cells"], ["C:\\new", "x"])
        self.assertEqual(log, [{"fix": "teh", "to": "C:\\new", "count": 2}])

    # balance_quotes keeps the apostrophe of an elided word (goin’) inside dialogue that a real ” closes later, but
    # still repairs a closing quote misread as ’ where no real ” follows.
    def test_balance_quotes(self):
        text = "“We’re goin’ home,” she said."
        self.assertEqual(clean.balance_quotes(text), (text, []))
        self.assertEqual(clean.balance_quotes("“We are home,’ she said."), ("“We are home,” she said.", [("’", "”")]))
        # the next dialogue opens before any ”: the stray ’ closed the first one
        self.assertEqual(clean.balance_quotes("“Go now.’ “Wait,” he said.")[0], "“Go now.” “Wait,” he said.")


# ------------------------------------------------------------------------------------------- text
class CodeNames(unittest.TestCase):
    # codenames.spoken reads names from source code the way a programmer says them, keeping quotes, brackets and
    # trailing punctuation around the reading, and leaves plain words alone.
    def test_spoken(self):
        cases = {
            "emblem_rsi/pron/asr.py": "emblem R S I slash pron slash A S R dot pie",
            "--accept-draft": "dash dash accept draft",
            "HTMLParser": "H T M L parser",
            "SPEECHKIT_CACHE": "speech kit cache",
            "os.environ": "O S dot environ",
            "2.3.0": "2 point 3 point 0",
            "e.g.": "for example",            # the abbreviation's own dot is not a sentence end
            "e.g.,": "for example,",
            "“speechkit_narrate”,": "“speech kit narrate”,",
            "(os.environ).": "(O S dot environ).",
            "--accept-draft.": "dash dash accept draft.",
        }
        for word, want in cases.items():
            self.assertEqual(codenames.spoken(word), want, word)
        for word in ("hello", "The", "running.", "“Quiet,”", "42", "—"):
            self.assertIsNone(codenames.spoken(word), word)

    # docmodel reads code names only when spoken.code_names is true, and spoken.say rules still win.
    def test_docmodel_uses_it_only_when_asked(self):
        self.assertEqual(docmodel.spoken_form("os.environ", None, None, {"code_names": True}), "O S dot environ")
        self.assertEqual(docmodel.spoken_form("os.environ", None, None, {}), "os.environ")
        self.assertEqual(docmodel.spoken_form("os.environ", None, None,
                                              {"code_names": True, "say": [["^os\\.environ$", "the environment"]]}),
                         "the environment")
        self.assertEqual(docmodel.spoken_form("e.g.,", None, None, {"code_names": True}), "for example,")


class Markup(unittest.TestCase):
    # FIGURE splits at the first " | " (so a caption may contain one), LINK at the last (so a title may), and a bare
    # "|" inside a link address stays in the address.
    def test_figure_and_link_split(self):
        blocks = markup.parse("FIGURE fig.png | Inputs | outputs\n\nLINK Plans | prices | https://x.org/a|b\n\n"
                              "LINK Plain|https://y.org/q", "T")
        self.assertEqual(blocks[1], {"kind": "figure", "src": "fig.png", "text": "Inputs | outputs"})
        self.assertEqual(blocks[2], {"kind": "link", "text": "Plans | prices", "href": "https://x.org/a|b"})
        self.assertEqual(blocks[3], {"kind": "link", "text": "Plain", "href": "https://y.org/q"})
        self.assertEqual(markup.validate("# A\n\nLINK Plans | prices | https://x.org/a|b\n"), [])
        self.assertEqual(markup.parse(markup.dump(blocks), "T"), blocks)  # the round trip keeps them

    # "Nov 10 – 11": both numbers are days, read as ordinals, with a free-standing dash between them.
    def test_day_range_with_dash(self):
        self.assertEqual(docmodel._day_positions(["Nov", "10", "–", "11"]), {1, 3})
        self.assertEqual(docmodel._day_positions(["Nov", "10", "or", "11"]), {1, 3})
        self.assertEqual(docmodel._day_positions(["page", "10", "–", "11"]), set())


# ------------------------------------------------------------------------------------------- fake narration
# A short mono 16-bit WAV at 22050 Hz: `seconds` long, a quiet tone (or silence with amp=0).
def write_wav(path: Path, seconds: float, amp: float = 0.1) -> None:
    n = int(round(seconds * RATE))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * 220 * k / RATE)))
                               for k in range(n)))


# Writes sNNNN.wav and sNNNN.wav.json for every segment of the project's request, as the voice workers would.
# Each segment gets its own length, so moved audio can be recognised. Returns {segment id: WAV bytes}.
def fake_narration(p: Project) -> dict:
    req = json.loads(p.p("request").read_text(encoding="utf-8"))
    sp = p.p("speech")
    sp.mkdir(parents=True, exist_ok=True)
    out = {}
    for n, s in enumerate(req["segments"]):
        secs = 0.3 + 0.05 * n
        write_wav(sp / f"{s['id']}.wav", secs)
        k = len(s["tokenIndex"])
        words = [{"token": t, "start": round(secs * i / k, 3), "end": round(secs * (i + 1) / k, 3)}
                 for i, t in enumerate(s["tokenIndex"])]
        rec = {"id": s["id"], "text": s["text"], "spokenText": s["spokenText"], "path": f"{s['id']}.wav",
               "words": words, "durationSeconds": secs}
        rec = seal_record(rec, sp / f"{s['id']}.wav", fake_identity(p))
        (sp / f"{s['id']}.wav.json").write_text(json.dumps(rec), encoding="utf-8")
        out[s["id"]] = (sp / f"{s['id']}.wav").read_bytes()
    return out


def no_voice(*a, **k):
    raise AssertionError("the voice runtime must not be started")


class NarrateReuse(_Scratch):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(nar, "project_identity", side_effect=fake_identity)
        patch.start()
        self.addCleanup(patch.stop)

    BOOK = "# One\n\nAlpha beta.\n\nGamma delta.\n\nEpsilon zeta eta.\n"

    def rebuild(self, p: Project, book: str) -> dict:
        p.p("book").write_text(book, encoding="utf-8")
        docmodel.build(p)
        return {s["id"]: s for s in json.loads(p.p("request").read_text(encoding="utf-8"))["segments"]}

    # A paragraph inserted earlier shifts the ids and token numbers of every later segment; their spoken text is the
    # same, so plan() reuses their audio instead of voicing them again. Only the new paragraph is voiced.
    def test_plan_reuses_shifted_segments(self):
        p = self.scripted(self.BOOK)
        fake_narration(p)
        old = {s["spokenText"]: s["id"] for s in self.rebuild(p, self.BOOK).values()}
        segs = self.rebuild(p, self.BOOK.replace("Alpha beta.", "A new paragraph here.\n\nAlpha beta."))
        new = {s["spokenText"]: s["id"] for s in segs.values()}
        with mock.patch.object(nar, "same_phonemes", lambda pairs: [False] * len(pairs)):
            pl = nar.plan(p)
        self.assertEqual(pl["voice"], [new["A new paragraph here."]])
        self.assertEqual(pl["reuse"], {new[t]: old[t] for t in ("Alpha beta.", "Gamma delta.", "Epsilon zeta eta.")})
        self.assertEqual(pl["text_only"], [])

    # With nothing to voice (a paragraph moved to the front), narrate() moves every audio file to its new id and
    # renumbers the record's tokens, without starting the voice runtime or the espeak helper.
    def test_narrate_moves_audio_without_the_voice(self):
        p = self.scripted(self.BOOK)
        audio_by_id = fake_narration(p)
        old = {s["spokenText"]: s["id"] for s in self.rebuild(p, self.BOOK).values()}
        segs = self.rebuild(p, "# One\n\nEpsilon zeta eta.\n\nAlpha beta.\n\nGamma delta.\n")
        with mock.patch.object(nar, "same_phonemes", lambda pairs: no_voice() if pairs else []), \
                mock.patch.object(nar, "run_workers", no_voice):
            r = nar.narrate(p, log=quiet)
        self.assertEqual(r, {"voiced": 0, "reused": 3, "text_only": 0, "pace_review": 0})
        sp = p.p("speech")
        for sid, seg in segs.items():
            rec = json.loads((sp / f"{sid}.wav.json").read_text(encoding="utf-8"))
            self.assertEqual(rec["id"], sid)
            self.assertEqual(rec["spokenText"], seg["spokenText"])
            self.assertEqual([w["token"] for w in rec["words"]], seg["tokenIndex"])  # renumbered
            self.assertEqual((sp / f"{sid}.wav").read_bytes(), audio_by_id[old[seg["spokenText"]]])
            if old[seg["spokenText"]] != sid:
                self.assertEqual(rec["reused"]["from"], old[seg["spokenText"]])
        self.assertFalse((sp / ".reuse").exists())
        with mock.patch.object(nar, "same_phonemes", lambda pairs: no_voice() if pairs else []):
            again = nar.plan(p)  # everything matches now
        self.assertEqual((again["voice"], again["reuse"], again["text_only"]), ([], {}, []))


# ------------------------------------------------------------------------------------------- export
class Export(_Scratch):
    BOOK = "# One\n\nAlpha beta gamma.\n\nDelta epsilon.\n\n# Two\n\nZeta eta theta.\n"

    # export writes one WAV and one word-timing file per chapter, the script and a manifest whose SHA-256 values match
    # the files, and removes files left by an earlier export.
    def test_export_chapters_and_manifest(self):
        p = self.scripted(self.BOOK, title="Export Test")
        fake_narration(p)
        out = p.p("out_narration")
        out.mkdir(parents=True)
        stale = out / f"{p.slug}_ch-07.mp3"  # from an earlier export with another format and more chapters
        stale.write_bytes(b"old")
        (out / "manifest.json").write_text(json.dumps({"script": f"{p.slug}_script.md",
            "chapters": [{"audio": stale.name, "timings": "old.words.json"}]}), encoding="utf-8")
        r = audio.export(p, log=quiet)
        self.assertEqual((r["chapters"], r["format"]), (2, "wav"))
        self.assertFalse(stale.exists())
        names = sorted(f.name for f in out.iterdir())
        slug = p.slug
        self.assertEqual(names, sorted([f"{slug}_ch-00.wav", f"{slug}_ch-00.words.json", f"{slug}_ch-01.wav",
                                        f"{slug}_ch-01.words.json", f"{slug}_script.md", "manifest.json"]))
        man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))

        def sha(name):
            return hashlib.sha256((out / name).read_bytes()).hexdigest()
        self.assertEqual(man["scriptSha256"], sha(f"{slug}_script.md"))
        self.assertEqual((out / f"{slug}_script.md").read_text(encoding="utf-8"), self.BOOK)
        self.assertEqual(len(man["chapters"]), 2)
        total_words = 0
        for ch in man["chapters"]:
            self.assertEqual(ch["audioSha256"], sha(ch["audio"]))
            self.assertEqual(ch["timingsSha256"], sha(ch["timings"]))
            with wave.open(str(out / ch["audio"])) as w:
                self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, RATE))
                self.assertAlmostEqual(w.getnframes() / RATE, ch["seconds"], places=1)
            words = json.loads((out / ch["timings"]).read_text(encoding="utf-8"))
            self.assertEqual(len(words), ch["words"])
            starts = [w["start"] for w in words]
            self.assertEqual(starts, sorted(starts))
            self.assertTrue(all(a < b for a, b in zip(starts, starts[1:])), starts)
            self.assertTrue(all(w["end"] > w["start"] for w in words))
            self.assertLessEqual(words[-1]["end"], ch["seconds"])
            total_words += len(words)
        self.assertEqual(man["words"], total_words)
        self.assertEqual([w["word"] for w in json.loads((out / man["chapters"][1]["timings"]).read_text(
            encoding="utf-8"))], ["Two", "Zeta", "eta", "theta."])

    # mp3 (and m4a) need ffmpeg: without it on PATH, export says so with a RuntimeError.
    def test_mp3_without_ffmpeg(self):
        p = self.scripted(self.BOOK)
        fake_narration(p)
        p.save_settings({"export": {"format": "mp3"}})
        with mock.patch.object(audio.shutil, "which", lambda name: None):
            with self.assertRaises(RuntimeError) as raised:
                audio.export(p, log=quiet)
        self.assertIn("ffmpeg", str(raised.exception))


# ------------------------------------------------------------------------------------------- audit report
class AuditReportRows(_Scratch):
    # A word that both recognizers misheard and that is also a wrongly read heteronym (a context row) appears once in
    # the report, under its first category; before the fix it was listed twice.
    def test_word_listed_once(self):
        from speechkit_narrate.audit import report
        p = self.scripted("# One\n\nI read it. They read it. We lead.\n")
        d = p.p("rsi") / "pron"
        (d / "measure-001" / "asr").mkdir(parents=True)
        (d / "measure-001" / "lexicon").mkdir(parents=True)
        (d / "measure-001.json").write_text(json.dumps({"metrics": {}, "inputs": {"words_total": 9, "segments": 3}}),
                                            encoding="utf-8")
        (d / "measure-001" / "asr" / "asr_measure.json").write_text(json.dumps({
            "confirmed": [{"word": "read", "count": 2, "segs": ["s0002"], "ctc_heard": {"red": 2}}],
            "occurrences": {"5": {"expected": "read"}, "8": {"expected": "read"}},
            "per_word": {"read": {"ok": 0, "count": 2, "heard": ["red"]}}, "per_word_confirm": {}}), encoding="utf-8")

        def occ(token, sentence):
            return {"token": token, "suspect": True, "reading_role": "past", "expected_role": "present",
                    "sentence": sentence}
        (d / "measure-001" / "lexicon" / "lexicon_measure.json").write_text(json.dumps({
            "flagged": [], "rows": [], "totals": {"heteronym_suspects": 3},
            "heteronyms": [{"word": "read", "occurrences": [occ(5, "I read it."), occ(8, "They read it.")]},
                           {"word": "lead", "occurrences": [occ(11, "We lead.")]}]}), encoding="utf-8")
        seen, real = {}, report.render_page

        def capture(data, title):  # keeps the page data, then builds the real page
            seen.update(data)
            return real(data, title)
        mods = {"trial": SimpleNamespace(word_key=lambda w: w.lower().strip(".,"))}
        with mock.patch.object(report, "configure", lambda project: mods), \
                mock.patch.object(report, "status", lambda project, name="pron": []), \
                mock.patch.object(report, "render_page", capture):
            page = report.build(p, log=quiet)
        self.assertTrue(page.is_file())
        rows = [(r["cat"], r["word"]) for r in seen["rows"]]
        self.assertEqual(sorted(rows), [("context", "lead"), ("heard", "read")])


# ------------------------------------------------------------------------------------------- MCP server
class McpHandlers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from speechkit_narrate import mcp_server
        except ImportError as e:  # the MCP SDK is optional
            raise unittest.SkipTest(f"MCP SDK not installed: {e}")
        cls.m = mcp_server

    # project_new checks its required arguments itself, so a call that bypassed the schema validation (jsonschema
    # not installed) gets a clear message instead of a KeyError.
    def test_project_new_without_input(self):
        m = self.m
        with self.assertRaises(m.ToolError) as raised:
            asyncio.run(m.t_new(None, {}))
        self.assertIn("project_new needs", str(raised.exception))
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(m.ToolError) as raised:
                asyncio.run(m.t_new(None, {"project": str(Path(td) / "new")}))
            self.assertIn("`input`", str(raised.exception))
            self.assertFalse((Path(td) / "new").exists())

    # run_tool waits as long as the client asked (wait_seconds), also for a tool that declares its own wait_seconds
    # parameter (which then receives it too); before the fix such a tool always waited the default 30 s.
    def test_run_tool_uses_the_clients_wait(self):
        m = self.m
        calls = []

        async def fake_start_job(ctx, kind, name, args, project, wait, post=None):
            calls.append({"kind": kind, "name": name, "args": args, "wait": wait})
            return "started"
        own = {"type": "object", "properties": {"x": {"type": "integer"}, "wait_seconds": {"type": "number"}}}
        plain = {"type": "object", "properties": {"x": {"type": "integer"}}}
        with mock.patch.object(m, "start_job", fake_start_job):
            route = m.Route("tool", "owner", own, info={"name": "owner", "params": own})
            self.assertEqual(asyncio.run(m.run_tool(None, route, {"x": 1, "wait_seconds": 7})), "started")
            schema = json.loads(json.dumps(plain))
            schema["properties"]["wait_seconds"] = {"type": "number"}
            route = m.Route("tool", "plain", schema, info={"name": "plain", "params": plain})
            asyncio.run(m.run_tool(None, route, {"x": 2, "wait_seconds": 9}))
        self.assertEqual(calls[0]["wait"], 7)
        self.assertEqual(json.loads(calls[0]["args"][2].split("=", 1)[1]), {"x": 1, "wait_seconds": 7})
        self.assertEqual(calls[1]["wait"], 9)
        self.assertEqual(json.loads(calls[1]["args"][2].split("=", 1)[1]), {"x": 2})  # the server's alone
        self.assertEqual(calls[1]["kind"], "tool")


# ------------------------------------------------------------------------------------------- real voice (opt-in)
VOICE_RUNTIME = os.environ.get("SPEECHKIT_RUNTIME", "")


# Runs only when SPEECHKIT_RUNTIME points at an installed speechkit runtime: the speak tool narrates one sentence
# with the real voice, through the command line, with a throwaway cache (the aligned voice model is built there).
@unittest.skipUnless(VOICE_RUNTIME and (Path(VOICE_RUNTIME) / ".venv").is_dir(),
                     "set SPEECHKIT_RUNTIME to an installed speechkit runtime to run the real-voice test")
class RealVoice(_Scratch):
    def test_speak(self):
        out = self.tmp / "hello.wav"
        env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONUTF8": "1", "SPEECHKIT_CACHE": str(self.tmp / "cache")}
        r = subprocess.run([sys.executable, "-m", "speechkit_narrate", "--json", "tool", "speak", "--text",
                            "The keeper lit the lamp at dusk.", "--out", str(out), "--workers", "1"],
                           capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(REPO), timeout=600)
        res = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
        self.assertTrue(res.get("ok"), r.stderr[-2000:])
        self.assertEqual(res["words"], 7)
        with wave.open(str(out)) as w:
            self.assertEqual(w.getframerate(), RATE)
            self.assertGreater(w.getnframes(), RATE)  # more than a second of speech
        words = json.loads((self.tmp / "hello.words.json").read_text(encoding="utf-8"))
        self.assertEqual([w["word"] for w in words], ["The", "keeper", "lit", "the", "lamp", "at", "dusk."])
        starts = [w["start"] for w in words]
        self.assertEqual(starts, sorted(starts))


if __name__ == "__main__":
    unittest.main()
