"""Unit tests of two registry behaviours an unattended worker depends on: a stage whose last run failed is run
again rather than adopted (its outputs may be half-made), and every run writes its progress to
work/.speechkit_narrate/events.jsonl as it happens.

    python3 -m unittest tests.narration.test_run_events        (from the repository root)
"""
# annotations stay unevaluated strings, the toolkit-wide convention
from __future__ import annotations

import json  # reads the event log back
import shutil  # removes the scratch folder after each test
import sys  # puts the repository root on the import path
import tempfile  # a scratch folder per test
import unittest  # the built-in test framework: TestCase classes and assertions
from pathlib import Path  # paths of the scratch project and its outputs

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the repository root, so `speechkit-voice-tts-offline-pipeline` can be imported
from speechkit_narrate import registry as R  # noqa: E402  the module under test
from speechkit_narrate.project import create  # noqa: E402  makes a minimal project folder


# A registry with two stub stages: "make" writes two outputs (out/a.txt and out/b.txt, declared with a glob) and can be
# told to fail after writing only the first; "after" needs "make". No real pipeline stage is involved.
def stub_registry(fail: dict) -> R.Registry:
    # The "make" stage: writes out/a.txt, then fails when fail["now"] is true, else writes out/b.txt too.
    def make(project, ctx):
        out = project.root / "out"  # the stage's output folder
        out.mkdir(exist_ok=True)  # created on the first run
        (out / "a.txt").write_text("a", encoding="utf-8")  # the first output is always written
        if fail["now"]:  # the test asked for a failure half-way
            raise RuntimeError("half-way failure")  # like a stage running out of disk space
        (out / "b.txt").write_text("b", encoding="utf-8")  # the second output, only on success
        return {"made": 2}  # a small result for the steps list

    # The "after" stage: does nothing but depend on "make", so a plan has a dependant.
    def after(project, ctx):
        return {}  # nothing to report

    reg = R.Registry()  # an empty registry, without the built-in stages
    # "make" declares its outputs with a glob, as a stage that makes several files does
    reg.add_stage(R.Stage("make", run=make, outputs=("out/*.txt",)))
    reg.add_stage(R.Stage("after", run=after, needs=("make",)))  # depends on make
    return reg  # ready for plan() and run()


# Planning and running with the stub stages in a fresh project.
class FailedStageAndEvents(unittest.TestCase):
    """A failed stage is never adopted, and every run leaves a readable event log."""

    # A scratch project for each test: create() needs an input file, so a two-line text file is written first.
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="speechkit-voice-tts-offline-pipeline-events-"))  # removed in tearDown
        src = self.tmp / "book.txt"  # a tiny input; the stub stages never read it
        src.write_text("Title\n\nOne line of text.\n", encoding="utf-8")  # UTF-8, as everywhere in the toolkit
        self.p = create(self.tmp / "project", src, title="Events")  # a real project folder with project.json
        self.fail = {"now": False}  # switched by the tests to make "make" fail
        self.reg = stub_registry(self.fail)  # the two stub stages

    # Removes the scratch folder and everything in it.
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)  # the project, its outputs and its state

    # Reads the project's event log as a list of dicts.
    def events(self) -> list[dict]:
        p = R.events_path(self.p)  # work/.speechkit_narrate/events.jsonl
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]  # one per line

    # The action the planner gives `stage` now.
    def action(self, stage: str) -> str:
        return {s["stage"]: s for s in R.plan(self.p, self.reg)}[stage]["action"]  # plan of the whole stub pipeline

    # Outputs left by a failed run are not adopted: the next plan runs the stage again, giving the failure as reason.
    def test_failed_stage_is_run_again_not_adopted(self):
        self.fail["now"] = True  # the first run fails after writing out/a.txt
        with self.assertRaises(RuntimeError):  # the failure propagates, as it does for the CLI
            R.run(self.p, self.reg, log=lambda *a: None)  # quiet log
        self.assertTrue((self.p.root / "out" / "a.txt").exists())  # the half-made output is on disk
        step = {s["stage"]: s for s in R.plan(self.p, self.reg)}["make"]  # what the planner now decides
        self.assertEqual(step["action"], "run")  # before this change it was "adopt", as the glob matched a.txt
        self.assertTrue(any(r.startswith("last run failed") for r in step["reasons"]))  # and it says why
        self.fail["now"] = False  # the next run succeeds
        R.run(self.p, self.reg, log=lambda *a: None)  # runs make (and after) for real
        self.assertTrue((self.p.root / "out" / "b.txt").exists())  # the missing output is now made
        self.assertEqual(self.action("make"), "skip")  # and the stage is up to date

    # Outputs made before tracking, with no failure on record, are still adopted as before.
    def test_untracked_outputs_still_adopted(self):
        out = self.p.root / "out"  # the stub stage's output folder
        out.mkdir()  # made by hand, as if by an older toolkit
        (out / "a.txt").write_text("a", encoding="utf-8")  # one existing output matching the glob
        self.assertEqual(self.action("make"), "adopt")  # unchanged behaviour when nothing failed

    # A successful run logs run-start, then start and done per stage, then run-end, with UTC times.
    def test_events_of_a_successful_run(self):
        lines = []  # the human log lines
        R.run(self.p, self.reg, log=lines.append)  # both stages run
        ev = self.events()  # the event log
        # the events in order: the plan, each stage's start and finish, then the end of the run
        self.assertEqual([e["event"] for e in ev], ["run-start", "start", "done", "start", "done", "run-end"])
        self.assertEqual([e.get("stage") for e in ev[1:5]], ["make", "make", "after", "after"])  # in run order
        self.assertTrue(all(e["time"].endswith("Z") for e in ev))  # UTC, marked with Z
        self.assertTrue(ev[-1]["ok"])  # the run ended well
        self.assertEqual(ev[-1]["ran"], ["make", "after"])  # and names what ran
        self.assertIn("done: make in", "\n".join(lines))  # the human log says each stage finished as it happened

    # A failing run logs the failure with its error, then a run-end with ok false.
    def test_events_of_a_failed_run(self):
        self.fail["now"] = True  # make fails half-way
        with self.assertRaises(RuntimeError):  # the error still propagates
            R.run(self.p, self.reg, log=lambda *a: None)  # quiet log
        ev = self.events()  # the event log
        self.assertEqual([e["event"] for e in ev], ["run-start", "start", "failed", "run-end"])  # stops at make
        self.assertIn("half-way failure", ev[2]["error"])  # the error text is kept
        self.assertFalse(ev[-1]["ok"])  # the run did not end well
        self.assertEqual(ev[-1]["failed"], "make")  # and names the stage that failed

    # A dry run writes no events: nothing ran.
    def test_dry_run_writes_no_events(self):
        R.run(self.p, self.reg, log=lambda *a: None, dry_run=True)  # plan only
        self.assertFalse(R.events_path(self.p).exists())  # no event log was started


if __name__ == "__main__":  # allows `python3 tests/narration/test_run_events.py`
    unittest.main()  # run every test in this file
