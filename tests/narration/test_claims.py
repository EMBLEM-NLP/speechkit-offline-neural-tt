"""Tests of how the parallel voice workers share a narration (speechkit_narrate/voice/claims.py and narrate.run_workers).

    python3 -m unittest tests.narration.test_claims        (from the repository root)

A fixed split of the segments leaves one worker idle at the end when the other was handed the slow ones. The workers of
one run now take the next free segment, longest first, by creating a marker file per segment in a folder they share.
These tests check the ordering and the marker logic by themselves, then run narrate.run_workers with small stand-in
workers (ordinary Python scripts, because the real voice needs the speech runtime) to check the whole hand-over: one
voicing per segment, a fresh marker folder that is removed again, failures reported, and output read from all workers
at once.
"""
# lets annotations stay unevaluated strings
from __future__ import annotations

import ast  # reads the real worker script's source without running it
import json  # the stand-in workers write small JSON records
import sys  # puts the repository root on the import path, and gives the Python that runs the stand-in workers
import tempfile  # scratch folders
import textwrap  # writes the stand-in worker's source without its indentation
import threading  # many threads race for the same markers
import unittest  # the built-in test framework: TestCase classes and assertions
from pathlib import Path  # builds the repository root path and the files in the scratch folders
from unittest import mock  # replaces the voice settings and the worker script for the duration of one test

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the repository root, so `speechkit-voice-tts-offline-pipeline` can be imported
from speechkit_narrate.voice import claims, narrate  # noqa: E402  the modules under test


# builds one request segment, as the voice script lists them
def segment(i: int, words: int) -> dict:
    """A request segment with the given number of spoken words."""
    return {"id": f"s{i:04d}", "text": "shown", "spokenText": " ".join(["word"] * words)}  # ids sort in book order


# Seven segments with these word counts; the two 40-word ones tie, which shows that ties keep the book's order.
SEGMENTS = [segment(i, w) for i, w in enumerate([3, 40, 7, 40, 1, 22, 9])]


# The order in which workers ask for segments.
class LongestFirst(unittest.TestCase):
    # Longest spoken text first; equal lengths stay in book order; the argument is left alone.
    def test_orders_by_length_and_keeps_book_order_on_ties(self):
        before = list(SEGMENTS)  # a copy to compare with afterwards
        ids = [s["id"] for s in claims.longest_first(SEGMENTS)]  # the ids in the order of asking
        self.assertEqual(ids, ["s0001", "s0003", "s0005", "s0006", "s0002", "s0000", "s0004"])  # 40, 40, 22, 9, 7, 3, 1
        self.assertEqual(SEGMENTS, before)  # the caller's list was not reordered

    # A segment without a spoken form is measured by its display text, as the voice does.
    def test_falls_back_to_the_display_text(self):
        plain = {"id": "s9", "text": "x " * 300}  # no "spokenText" at all, and a display text of 600 characters
        empty = {"id": "s8", "spokenText": "", "text": "x " * 300}  # an empty spoken text counts as missing too
        for seg in (plain, empty):  # each should be measured by its display text
            ids = [s["id"] for s in claims.longest_first([SEGMENTS[1], seg])]  # against a segment of 199 characters
            # 600 characters beat 199, so it comes first (length 0 would put it last)
            self.assertEqual(ids[0], seg["id"])


# Taking segments by creating marker files.
class TakeInTurns(unittest.TestCase):
    # Builds a fresh marker folder for each test and removes it afterwards.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()  # the scratch folder
        self.addCleanup(self.tmp.cleanup)  # removed when the test ends
        self.folder = Path(self.tmp.name)  # the shared marker folder

    # One worker alone gets every segment once, in the order of asking.
    def test_a_single_worker_takes_everything_once(self):
        got = [s["id"] for s in claims.take_in_turns(SEGMENTS, self.folder)]  # drain the generator
        self.assertEqual(got, [s["id"] for s in claims.longest_first(SEGMENTS)])  # all of them, longest first
        # one marker each
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), sorted(s["id"] for s in SEGMENTS))

    # Two workers that ask by turns never get the same segment, and together they get all of them.
    def test_two_workers_never_take_the_same_segment(self):
        a, b = claims.take_in_turns(SEGMENTS, self.folder), claims.take_in_turns(SEGMENTS, self.folder)  # two workers
        mine, theirs = [], []  # what each one got
        for _ in range(len(SEGMENTS)):  # alternate: a asks, then b asks
            for who, gen in ((mine, a), (theirs, b)):  # each worker in turn, with the list its segments go to
                try:  # a generator that has run out raises StopIteration
                    who.append(next(gen)["id"])  # the next free segment for this worker
                except StopIteration:  # this worker has run out of segments
                    pass  # nothing left for it
        self.assertEqual(sorted(mine + theirs), sorted(s["id"] for s in SEGMENTS))  # every segment, exactly once
        self.assertEqual(mine, ["s0001", "s0005", "s0002", "s0004"])  # a: 1st, 3rd, 5th and 7th of the asking order
        self.assertEqual(theirs, ["s0003", "s0006", "s0000"])  # b: 2nd, 4th and 6th

    # Asking happens only when the next segment is wanted: a worker that stops asking holds nothing back.
    def test_a_worker_that_stops_asking_holds_nothing_back(self):
        # two workers that share the same marker folder
        slow, fast = claims.take_in_turns(SEGMENTS, self.folder), claims.take_in_turns(SEGMENTS, self.folder)
        first = next(slow)["id"]  # the slow worker takes the longest segment and then does not ask again
        rest = [s["id"] for s in fast]  # the fast worker takes everything else
        self.assertEqual(first, "s0001")  # the longest, in book order among the ties
        # nothing was held back or taken twice
        self.assertEqual(sorted(rest + [first]), sorted(s["id"] for s in SEGMENTS))
        self.assertEqual(len(rest), len(SEGMENTS) - 1)  # the fast worker got all but one

    # Markers left in the folder hide their segments, which is why every run needs a new folder.
    def test_old_markers_hide_segments(self):
        (self.folder / "s0001").touch()  # a marker from an earlier run
        got = [s["id"] for s in claims.take_in_turns(SEGMENTS, self.folder)]  # a new worker in the same folder
        self.assertNotIn("s0001", got)  # that segment is treated as taken
        self.assertEqual(len(got), len(SEGMENTS) - 1)  # all others are still handed out

    # Many threads asking at once still get every segment exactly once (the markers are the only coordination).
    def test_a_race_between_threads_gives_each_segment_once(self):
        many = [segment(i, 1 + i % 17) for i in range(300)]  # 300 segments of varying length
        taken = [[] for _ in range(6)]  # what each of six threads got
        start = threading.Barrier(6)  # releases all threads together

        def work(slot: int):  # the body of one worker thread
            start.wait()  # begin at the same moment as the others
            taken[slot].extend(s["id"] for s in claims.take_in_turns(many, self.folder))  # ask until nothing is left

        threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]  # six workers
        for t in threads:  # start every thread
            t.start()  # go
        for t in threads:  # and then wait until each has finished
            t.join()  # wait for all
        everyone = [i for part in taken for i in part]  # all the ids taken, with repeats if any
        self.assertEqual(len(everyone), 300)  # no segment taken twice and none missed
        self.assertEqual(sorted(everyone), sorted(s["id"] for s in many))  # exactly the 300 ids


# The stand-in worker. It understands the options narrate.run_workers sends, takes its segments the way karaoke_voice.py
# does, and writes a small record for each. Two options of the test (SLOW_WORKER, NOISY) change what it does.
WORKER = textwrap.dedent('''
    import argparse, importlib.util, json, sys, time
    from pathlib import Path
    ap = argparse.ArgumentParser()
    ap.add_argument("--script"); ap.add_argument("--out")
    ap.add_argument("--worker"); ap.add_argument("--claims", default="")
    a = ap.parse_args()
    k, n = map(int, a.worker.split("/"))
    script = json.loads(Path(a.script).read_text(encoding="utf-8"))
    mode = json.loads(Path(a.script).with_name("mode.json").read_text(encoding="utf-8"))
    if a.claims and n > 1:
        spec = importlib.util.spec_from_file_location("c", mode["claims_py"])
        c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
        segments = c.take_in_turns(script["segments"], a.claims)
    else:
        segments = script["segments"][k::n]
    out = Path(a.out)
    if mode.get("fail") == k:
        print("boom from worker %d" % k, file=sys.stderr); sys.exit(3)
    if mode.get("noisy") == k:
        sys.stderr.write("x" * 400000); sys.stderr.flush()
        (out / "noise.done").write_text("done")
    if mode.get("waits_for_noise") == k:
        for _ in range(200):
            if (out / "noise.done").exists(): break
            time.sleep(0.1)
        else:
            sys.exit(5)
    for s in segments:
        (out / (s["id"] + ".wav.json")).write_text(json.dumps({"worker": k, "claims": a.claims}), encoding="utf-8")
        if mode.get("slow") == k: time.sleep(0.3)
''')  # the end of the stand-in worker's source text


# narrate.run_workers with the stand-in worker in place of karaoke_voice.py.
class RunWorkers(unittest.TestCase):
    # Builds a scratch folder holding the stand-in worker and a request, and swaps the voice settings for it.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()  # the scratch folder
        self.addCleanup(self.tmp.cleanup)  # removed when the test ends
        self.dir = Path(self.tmp.name)  # its path
        # the stand-in, under the real script's name
        (self.dir / "karaoke_voice.py").write_text(WORKER, encoding="utf-8")
        self.script = self.dir / "request.json"  # the request the workers read
        # the request: twelve segments of one to nine spoken words
        self.script.write_text(json.dumps({"segments": [segment(i, 1 + i % 9) for i in range(12)]}), encoding="utf-8")
        self.out = self.dir / "speech"  # where the records go
        self.mode({})  # no special behaviour yet
        for patch in (mock.patch.object(narrate, "HERE", self.dir),  # the script is looked up beside the module
                      # replaces the function that builds the worker's command line
                      mock.patch.object(narrate, "voice_args",
                                        # the stand-in worker takes only the request and the output folder
                                        lambda project, script, out: ["--script", str(script), "--out", str(out)]),
                      # a plain Python is enough
                      mock.patch.object(narrate.env, "runtime_python", lambda: sys.executable)):
            patch.start()  # in force from here
            self.addCleanup(patch.stop)  # and undone when the test ends

    # Tells the stand-in how to behave (which worker is slow, noisy, waiting or failing).
    def mode(self, options: dict):
        # mode.json holds the path of claims.py and the options of the test
        (self.dir / "mode.json").write_text(json.dumps({"claims_py": str(Path(claims.__file__)), **options}),
                                            encoding="utf-8")  # read as UTF-8, like the stand-in's other files

    # Runs the workers and returns {segment id: record the worker wrote}.
    def run_workers(self, workers: int) -> dict:
        # the project is not used, because voice_args is replaced
        narrate.run_workers(None, self.script, self.out, workers)
        # one record per segment, keyed by the segment's id without ".wav.json"
        return {p.name[:-len(".wav.json")]: json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(self.out.glob("*.wav.json"))}  # read in file-name order

    # Every segment is voiced once, by workers that were all given the same marker folder, which is gone afterwards.
    def test_each_segment_is_voiced_once_and_the_marker_folder_is_removed(self):
        recs = self.run_workers(2)  # two workers
        self.assertEqual(sorted(recs), [f"s{i:04d}" for i in range(12)])  # all twelve have a record
        folders = {r["claims"] for r in recs.values()}  # the marker folder each record's worker was given
        self.assertEqual(len(folders), 1)  # the same one for every worker
        folder = Path(folders.pop())  # its path
        self.assertIn("speechkit-voice-tts-offline-pipeline-claims-", folder.name)  # made by run_workers
        self.assertFalse(folder.exists())  # and removed when the run ended

    # A lone worker has nobody to share with, so it gets no marker folder and the fixed share (all of it).
    def test_a_single_worker_gets_no_marker_folder(self):
        recs = self.run_workers(1)  # one worker
        self.assertEqual(len(recs), 12)  # it voiced everything
        self.assertEqual({r["claims"] for r in recs.values()}, {""})  # without a --claims option

    # The point of sharing: a slow worker is not handed half the segments, so the fast one does not sit idle.
    def test_a_slow_worker_gets_fewer_segments(self):
        self.mode({"slow": 0})  # worker 0 takes 0.3 s over every segment
        recs = self.run_workers(2)  # two workers
        by_worker = [sum(1 for r in recs.values() if r["worker"] == k) for k in (0, 1)]  # segments per worker
        self.assertEqual(sum(by_worker), 12)  # still every segment, once
        # the fast worker took at least three quarters of them (a fixed split gives 6)
        self.assertGreaterEqual(by_worker[1], 9)

    # A failing worker is reported with its error text, and the marker folder is removed anyway.
    def test_a_failing_worker_is_reported(self):
        self.mode({"fail": 1})  # worker 1 exits with an error
        made = []  # the folder names that run_workers asked for
        real = tempfile.TemporaryDirectory  # the real class, kept before it is replaced below

        def track(*a, **kw):  # wraps TemporaryDirectory to learn the folder's name
            d = real(*a, **kw)  # the real one
            made.append(Path(d.name))  # remember where it is
            return d  # hand the real object back unchanged

        with mock.patch.object(narrate.tempfile, "TemporaryDirectory", track):  # watch the folder being made
            with self.assertRaises(RuntimeError) as caught:  # the run fails as a whole
                self.run_workers(2)  # two workers, one of which fails
        self.assertIn("boom from worker 1", str(caught.exception))  # with the worker's own words
        self.assertTrue(made and not made[0].exists())  # and the marker folder is gone

    # The real worker script cannot run here (it needs the speech runtime), so this only checks that it still has the
    # two pieces the hand-over relies on: the --claims option, and a call of claims.take_in_turns for several workers.
    def test_the_real_worker_script_takes_part(self):
        src = (Path(narrate.__file__).with_name("karaoke_voice.py")).read_text(encoding="utf-8")  # the script's source
        tree = ast.parse(src)  # as a syntax tree, without running it
        options = [n.args[0].value for n in ast.walk(tree)  # first argument of every add_argument call
                   # only calls of a method named add_argument that were given arguments
                   if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_argument" and n.args
                   and isinstance(n.args[0], ast.Constant)]  # the first argument must be a literal such as "--claims"
        self.assertIn("--claims", options)  # the option narrate.run_workers sends
        calls = [n for n in ast.walk(tree)  # every call of a method named take_in_turns in the script
                 # the name is checked on the attribute, because it is called as claims.take_in_turns
                 if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "take_in_turns"]
        self.assertEqual(len(calls), 1)  # asked for in one place
        # with the whole request and the shared folder
        self.assertIn("take_in_turns(script['segments'], args.claims)", src)
        # the innermost `if` that holds the call: it decides between sharing and the fixed split
        ifs = [n for n in ast.walk(tree)
               # an `if` is a candidate when the call sits anywhere in its body
               if isinstance(n, ast.If) and any(calls[0] is m for s in n.body for m in ast.walk(s))]
        branch = min(ifs, key=lambda n: n.end_lineno - n.lineno)  # the smallest one is the innermost
        # shared only when asked for, and with several workers
        self.assertEqual(ast.unparse(branch.test), "args.claims and n > 1")
        other = ast.unparse(ast.Module(body=branch.orelse, type_ignores=[]))  # what happens in every other case
        # the fixed split of before, for a lone worker or an old caller
        self.assertEqual(other, "segments = script['segments'][k::n]")

    # Output is read from all workers at once. Worker 1 writes 400 KB to stderr, more than a pipe holds, and only then
    # lets worker 0 go on. If the workers were read one after another, worker 1 would block on the full pipe while
    # worker 0 waited for it, and worker 0 would give up with an error after 20 seconds.
    def test_all_workers_are_read_at_the_same_time(self):
        # worker 1 floods stderr and then signals; worker 0 waits for the signal
        self.mode({"noisy": 1, "waits_for_noise": 0})
        recs = self.run_workers(2)  # raises if worker 0 gave up
        self.assertEqual(len(recs), 12)  # and everything was voiced


# Lets the file be run directly as a script.
if __name__ == "__main__":
    unittest.main()  # runs every test in this module
