"""Unit tests of two helpers of the pronunciation loop's driver (speechkit_narrate/audit/autofix.py): carrying on with a trial
that was stopped part way, and freeing the speech recognizers before voice workers start.

    python3 -m unittest tests.narration.test_autofix_resume        (from the repository root)
"""
# lets annotations stay unevaluated strings
from __future__ import annotations

import json  # writes the trial plans the helper compares
import sys  # puts the repository root on the import path
import tempfile  # a scratch run directory per test
import unittest  # the built-in test framework: TestCase classes and assertions
from pathlib import Path  # builds the run directory's paths
from types import SimpleNamespace  # a stand-in for the asr module, with only the attribute release() touches

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the repository root, so `speechkit-narrate` can be imported
from speechkit_narrate.audit import autofix  # noqa: E402  the module under test


# trial_dir: which folder a trial of a plan uses
class TrialDir(unittest.TestCase):
    """A trial stopped part way is resumed in its own folder; any other plan gets a new folder."""

    PLAN = {"max_occurrences": 4, "words": {"meta": ["[[mˈɛtɐ]]", "meeta"]}}  # a plan with one word and two variants

    # A scratch run directory for each test, removed afterwards.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()  # deleted in tearDown
        self.d = Path(self.tmp.name)  # the run directory

    # Removes the scratch run directory.
    def tearDown(self):
        self.tmp.cleanup()  # the folder and everything in it

    # Writes trial-NN/plan.json with `plan` and returns the folder.
    def trial(self, n: int, plan: dict) -> Path:
        t = self.d / f"trial-{n:02d}"  # the folder name the driver uses
        t.mkdir()  # created as the driver does before writing the plan
        (t / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")  # the plan, as JSON
        return t  # the folder

    # With no trial yet the first folder is trial-01.
    def test_first(self):
        self.assertEqual(autofix.trial_dir(self.d, self.PLAN), self.d / "trial-01")  # numbered from one

    # The newest trial ran the same plan (it was stopped, say by running out of memory): its folder is reused.
    def test_resume_same_plan(self):
        self.trial(1, {"max_occurrences": 4, "words": {}})  # an older trial of another plan
        t = self.trial(2, self.PLAN)  # the newest trial, of this plan
        self.assertEqual(autofix.trial_dir(self.d, self.PLAN), t)  # carried on in trial-02

    # A different plan, even only in one variant, starts a new folder, and an older match is not reused.
    def test_new_plan(self):
        self.trial(1, self.PLAN)  # an older trial of this plan
        self.trial(2, {"max_occurrences": 4, "words": {"meta": ["[[mˈɛtɐ]]"]}})  # the newest, one variant fewer
        self.assertEqual(autofix.trial_dir(self.d, self.PLAN), self.d / "trial-03")  # the next number

    # A trial folder without a plan (stopped before it was written) is not reused.
    def test_folder_without_plan(self):
        (self.d / "trial-01").mkdir()  # an empty folder
        self.assertEqual(autofix.trial_dir(self.d, self.PLAN), self.d / "trial-02")  # a fresh one beside it


# release: freeing the recognizers the asr module keeps
class Release(unittest.TestCase):
    """release() empties the asr module's recognizer cache."""

    # The cache is emptied, and the module is otherwise left alone.
    def test_clears_cache(self):
        asr = SimpleNamespace(_RECOGNIZERS={("ctc", 2): object(), ("confirm", 2): object()}, other=1)  # two loaded
        autofix.release({"asr": asr})  # the driver passes configure()'s module dict
        self.assertEqual(asr._RECOGNIZERS, {})  # nothing is held any more
        self.assertEqual(asr.other, 1)  # nothing else changed


if __name__ == "__main__":  # allows `python3 tests/narration/test_autofix_resume.py`
    unittest.main()  # run every test in this file
