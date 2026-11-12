# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Regression tests for review fixes in emblem_rsi.pron (candidates, lexicon, trial, cli revert, paths).

None of these needs the voice runtime or the recognizer models: espeak and the synthesis worker are replaced by
stand-ins, so the tests run anywhere.
"""
import json  # writes fake project files and reads the fake worker's job spec
import os  # builds a clean environment for the paths subprocess
import shutil  # removes the temporary folders
import stat  # marks the fake runtime script executable
import subprocess  # imports emblem_rsi.pron.paths in a fresh interpreter
import sys  # the interpreter that runs the fake runtime and the subprocess
import tempfile  # temporary folders for projects, runs and WAVs
import threading  # runs synthesize() with a deadline, so a deadlock fails instead of hanging
import unittest  # standard library test framework
from pathlib import Path  # paths for the fake project and run folders
from unittest import mock  # replaces espeak, verb_reading and score during a test

from emblem_rsi import loop  # LoopError raised by revert
from emblem_rsi.pron import candidates, cli, lexicon as lx, paths, trial  # the modules under test

REPO = Path(__file__).resolve().parents[2]  # repository root: tests/narration is two levels below it
NOUN = "R EH1 K ER0 D"  # CMU "record" as a noun
VERB = "R IH0 K AO1 R D"  # CMU "record" as a verb


# heteronym_fixes must score every suspect occurrence against candidates built from the reading it needs.
class HeteronymCandidates(unittest.TestCase):
    def test_each_occurrence_uses_its_own_reading(self):
        occ = lambda tok, role: {"suspect": True, "expected_role": role, "token": tok, "seg": "s1",  # noqa: E731
                                 "sentence": "x", "espeak_in_context": "ɹˈɛkɔːd"}
        het = [{"word": "record", "cmu_arpabet": [NOUN, VERB],
                "occurrences": [occ(1, "verb"), occ(2, "noun-adj")]}]  # first wants the verb, second the noun
        calls = []  # (candidate texts, targets) per score() call

        def fake_score(word, cands, row=None, allow_stress_fix=False, targets=None, before=None):
            calls.append(({c["text"] for c in cands}, targets))
            return []  # nothing ranked; only the inputs matter here

        with mock.patch.object(lx, "espeak", lambda texts, *a, **k: {"en": [""] * len(list(texts))}), \
                mock.patch.object(lx, "verb_reading", lambda w, a: a == VERB), \
                mock.patch.object(candidates, "score", fake_score):
            out = candidates.heteronym_fixes(het)
        self.assertEqual(len(calls), 2)
        verb_texts, noun_texts = set(candidates.respellings(VERB)), set(candidates.respellings(NOUN))
        self.assertTrue(verb_texts <= calls[0][0])  # the verb occurrence gets verb respellings
        self.assertTrue(noun_texts <= calls[1][0])  # the noun occurrence gets noun respellings ...
        self.assertFalse((verb_texts - noun_texts) & calls[1][0])  # ... and none built only for the verb
        self.assertEqual(calls[1][1], [lx.arpabet_to_ipa(NOUN)])  # scored against the noun target
        roles = [o["expected_role"] for o in out["record"]["occurrences"]]
        self.assertEqual(roles, ["verb", "noun-adj"])  # each occurrence records its own reading


# arpabet_to_ipa raises ValueError, never AttributeError, for any bad symbol.
class ArpabetErrors(unittest.TestCase):
    def test_malformed_and_unknown_symbols(self):
        for bad in ("ah0", "AH3", "A-B", "R EH1 k", "QQ1"):  # wrong shape, wrong digit, junk, lowercase, unknown
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                lx.arpabet_to_ipa(bad)
        self.assertTrue(lx.arpabet_to_ipa(NOUN))  # a valid pronunciation still converts


# trial.build_jobs drops repeated candidates (keeping order) and synthesize() cannot deadlock on worker output.
class Trial(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_build_jobs_dedupes_variants(self):
        req = {"segments": [{"id": "s1", "tokens": ["The", "record."], "tokenIndex": [10, 11]}]}
        jobs = trial.build_jobs(req, {"record": ["re-cord", trial.CURRENT, "re-cord", "rekord"]}, 1,
                                self.tmp, self.tmp)
        self.assertEqual([j["variant"] for j in jobs], [trial.CURRENT, "re-cord", "rekord"])
        self.assertEqual(len({j["id"] for j in jobs}), len(jobs))  # job ids (and WAV paths) stay unique

    # A stand-in runtime "python": ignores its arguments, writes far more than a pipe buffer to stdout and stderr,
    # creates every requested WAV path, and exits with the given code.
    def fake_runtime(self, code=0):
        script = self.tmp / "fake-python"
        script.write_text(f"#!{sys.executable}\n"
                          "import json, sys\n"
                          "spec = json.load(sys.stdin)\n"
                          "for j in spec['jobs']:\n"
                          "    open(j['out'], 'wb').close()\n"
                          "    sys.stdout.write('x' * 200000 + '\\n'); sys.stdout.flush()\n"
                          "sys.stderr.write('e' * 200000 + 'worker said no\\n')\n"
                          f"sys.exit({code})\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return str(script)

    def run_synth(self, jobs, runtime):
        box = {}

        def go():
            try:
                with mock.patch.object(trial, "RUNTIME_PY", runtime), \
                        mock.patch.object(trial, "VOICE_CONFIG", self.tmp / "voice.json"):
                    trial.synthesize(jobs, workers=2)
            except Exception as e:  # noqa: BLE001 - handed back to the test thread
                box["error"] = e
        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(60)
        self.assertFalse(t.is_alive(), "synthesize() deadlocked on worker output")
        return box.get("error")

    def test_synthesize_with_noisy_workers(self):
        jobs = [{"text": "x", "scale": 1.0, "out": str(self.tmp / f"j{k}.wav")} for k in range(6)]
        self.assertIsNone(self.run_synth(jobs, self.fake_runtime(0)))
        self.assertTrue(all(Path(j["out"]).exists() for j in jobs))

    def test_synthesize_reports_failure(self):
        jobs = [{"text": "x", "scale": 1.0, "out": str(self.tmp / "j.wav")}]
        err = self.run_synth(jobs, self.fake_runtime(3))
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("worker said no", str(err))


# Running `pron revert` twice walks back two applies instead of restoring the same snapshot again.
class RevertTwice(unittest.TestCase):
    def test_second_revert_goes_further_back(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        proj, run = tmp / "proj", tmp / "run"
        (proj / "work" / "speech").mkdir(parents=True)
        for n, tag in (("001", "before-first"), ("002", "before-second")):  # two snapshots, oldest first
            snap = run / "snapshots" / n
            (snap / "speech").mkdir(parents=True)
            for f in ("request.json", "docmodel.json"):
                (snap / f).write_text(json.dumps({"tag": tag}), encoding="utf-8")
        (proj / "work" / "request.json").write_text("{}", encoding="utf-8")
        read = lambda: json.loads((proj / "work" / "request.json").read_text(encoding="utf-8"))["tag"]  # noqa: E731
        cli.revert(run, proj, log=lambda *a: None)
        self.assertEqual(read(), "before-second")
        cli.revert(run, proj, log=lambda *a: None)
        self.assertEqual(read(), "before-first")  # not before-second again
        with self.assertRaises(loop.LoopError):  # both snapshots are used up
            cli.revert(run, proj, log=lambda *a: None)


# paths has neutral defaults: the current directory as the root and None (with a clear error) for the rest.
class NeutralPaths(unittest.TestCase):
    def test_defaults_without_environment(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("EMBLEM_")}
        env["PYTHONPATH"] = str(REPO)
        code = ("from emblem_rsi.pron import paths; import json\n"
                "print(json.dumps([str(paths.ROOT), paths.RUNTIME_PY, paths.MODELS, paths.VOICE_CONFIG,\n"
                "                  paths.REBUILD_CMD, paths.REVOICE_CMD, paths.VOICE_CMD]))\n"
                "try:\n    paths.voice_cmd('a', 'b', '0/1')\nexcept RuntimeError as e:\n    print(e)\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=tmp, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        first, msg = r.stdout.splitlines()[:2]
        self.assertEqual(json.loads(first), [str(Path(tmp).resolve()), None, None, None, None, None, None])
        self.assertIn("EMBLEM_VOICE_CMD", msg)

    def test_need(self):
        self.assertEqual(paths.need("x", "EMBLEM_X", "thing"), "x")
        with self.assertRaisesRegex(RuntimeError, "EMBLEM_X"):
            paths.need(None, "EMBLEM_X", "thing")


if __name__ == "__main__":
    unittest.main()
