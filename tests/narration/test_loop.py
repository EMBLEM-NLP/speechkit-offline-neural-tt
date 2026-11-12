"""Unit tests for the loop rules. They use made-up measurements, so no browser is needed."""
import json  # edits the sealed rubric file and writes a bad rubric
import tempfile  # temporary run folders created with mkdtemp
import unittest  # standard library test framework
from pathlib import Path  # Path objects for run folders and rubric files

from emblem_rsi import loop  # the audit loop under test: seal, decide, score and measure

# Metric values that pass every rule of the default "viscous-letters" rubric (the loop's own browser-page rubric): no
# console errors or overflow, reduced motion honoured, letter shapes kept at rest and while shaken.
GOOD = {"console_errors": 0, "layout_overflow_px": 0, "reduced_motion_ok": 1, "rest_iou_min": 0.9, "vib_iou_min": 0.7,
        # displacement ratio, ripples, signature pitch error (Hz) and amplitude correlation, frame time (ms)
        "vib_disp_ratio_min": 2.0, "ripples_all_on": 1, "signature_f0_err_hz": 0.1, "signature_amp_corr": 0.8, "frame_ms": 20}


# Build a fake measurement in the format decide() expects (schema emblem-rsi.measurement/1) without running a browser.
# sealed is the sealed rubric and metrics the metric values; page and sig stand in for the page and signature hashes; at
# is an ISO timestamp, now by default.
def meas(sealed, metrics, page="p1", sig="s1", at=None):
    # correct schema and the sealed rubric's hash, so only the metrics decide
    return {"schema": loop.MEASURE_SCHEMA, "rubric_sha256": sealed["sha256"], "measured_at": at or loop.now(),
            # stand-in page and signature hashes; equal values mean nothing changed
            "page_sha256": page, "signature_sha256": sig, "metrics": metrics}


# Tests of the loop's fixed rules. Every test starts from a freshly sealed default rubric in its own folder.
class LoopTests(unittest.TestCase):
    # Runs before every test: create a run folder and seal the default rubric into it. The mkdtemp folders are not
    # removed afterwards.
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())  # new empty folder, left behind after the test run
        # seal the default rubric; returns the sealed dict with its hash and timestamp
        self.sealed = loop.seal(loop.DEFAULT_RUBRIC, self.dir)

    # The first measurement that passes every metric is promoted with a perfect score. There is no history, so nothing
    # can veto it.
    def test_first_good_loop_is_promoted(self):
        e = loop.decide(self.sealed, meas(self.sealed, GOOD), [])  # decide with an empty history list
        self.assertEqual(e["decision"], "promote", e["reasons"])  # promote; on failure the veto reasons are printed
        self.assertEqual(e["score"], 1.0)  # every metric passes, so full credit

    # A run folder can be sealed only once. A second seal must fail so the rules cannot be swapped after measuring.
    def test_seal_is_never_overwritten(self):
        with self.assertRaises(loop.LoopError):  # expect the loop's error type
            loop.seal(loop.DEFAULT_RUBRIC, self.dir)  # sealing again into the same run folder

    # Editing a sealed rubric afterwards is detected through its hash. Loading it must then fail.
    def test_edited_rubric_is_detected(self):
        p = self.dir / "rubric.sealed.json"  # the sealed rubric file inside the run folder
        # lower a threshold in place, the kind of edit the seal guards against
        s = json.loads(p.read_text()); s["rubric"]["metrics"]["rest_iou_min"]["value"] = 0.1
        p.write_text(json.dumps(s))  # write the tampered file back
        with self.assertRaises(loop.LoopError):  # loading the tampered rubric must now raise
            loop.load_sealed(self.dir)  # the stored hash no longer matches the rubric

    # Measuring needs a sealed rubric. Without one, measure() refuses before touching the page or starting a browser.
    def test_measure_refuses_without_a_seal(self):
        empty = Path(tempfile.mkdtemp())  # a fresh folder with no sealed rubric
        with self.assertRaises(loop.LoopError):  # expect a refusal raised by the loop module
            # page, signature, run folder, node_modules; the page does not even exist
            loop.measure(Path("x.html"), None, empty, None)

    # A failed gate vetoes the loop even on the first measurement. rest_iou_min is a gate in the default rubric.
    def test_gate_failure_vetoes(self):
        bad = dict(GOOD, rest_iou_min=0.5)  # rest_iou_min 0.5 is below the 0.85 gate threshold
        e = loop.decide(self.sealed, meas(self.sealed, bad), [])  # decide the first loop with no history
        # vetoed, and the reasons name the failed gate
        self.assertEqual(e["decision"], "veto"); self.assertIn("rest_iou_min", " ".join(e["reasons"]))

    # A metric that was not measured (None) counts as failed. This one is a gate, so the loop is vetoed.
    def test_unmeasured_counts_as_failed(self):
        m = dict(GOOD); m["signature_amp_corr"] = None  # signature_amp_corr missing from the measurement
        e = loop.decide(self.sealed, meas(self.sealed, m), [])  # first loop, no earlier history needed
        # vetoed; the metric's row says it did not pass
        self.assertEqual(e["decision"], "veto"); self.assertFalse(e["rows"]["signature_amp_corr"]["pass"])

    # A second measurement with the same perfect score is vetoed. The score must rise by at least min_gain over the best
    # promoted loop.
    def test_no_improvement_vetoes(self):
        first = loop.decide(self.sealed, meas(self.sealed, GOOD), [])  # a perfect first loop, promoted
        # the same metrics on a different page: no gain
        e = loop.decide(self.sealed, meas(self.sealed, GOOD, page="p2"), [first])
        # vetoed with the "not at least" gain reason
        self.assertEqual(e["decision"], "veto"); self.assertIn("not at least", " ".join(e["reasons"]))

    # A measurement with the same page and signature as the last promoted loop is vetoed even when its score is higher.
    # Nothing changed that could explain the gain.
    def test_unchanged_page_vetoes(self):
        weak = dict(GOOD, vib_iou_min=0.5)           # soft metric fails, gates pass
        # the weak first loop is still promoted, since soft metrics only lower the score
        first = loop.decide(self.sealed, meas(self.sealed, weak), [])
        self.assertEqual(first["decision"], "promote")  # confirm the weak baseline was promoted
        # better metrics, but page "p1" and signature "s1" are unchanged
        e = loop.decide(self.sealed, meas(self.sealed, GOOD, page="p1"), [first])
        # vetoed with the unchanged page reason
        self.assertEqual(e["decision"], "veto"); self.assertIn("unchanged", " ".join(e["reasons"]))

    # A real gain is promoted: a changed page and a score that rises enough. The soft metric vib_iou_min goes from 0.5
    # to 0.7.
    def test_real_gain_is_promoted(self):
        weak = dict(GOOD, vib_iou_min=0.5)  # first loop with a weak soft metric
        first = loop.decide(self.sealed, meas(self.sealed, weak), [])  # the promoted baseline enters the history
        e = loop.decide(self.sealed, meas(self.sealed, GOOD, page="p2"), [first])  # changed page and better metrics
        self.assertEqual(e["decision"], "promote", e["reasons"])  # promoted; the veto reasons are shown if not

    # A guard metric that gets worse while the score rises looks like a probe artefact and is vetoed. rest_iou_min drops
    # from 0.9 to 0.86, beyond the 0.02 slack, though it still passes its gate.
    def test_guard_regression_vetoes_even_if_score_rises(self):
        base = dict(GOOD, vib_iou_min=0.3, vib_disp_ratio_min=0.5, ripples_all_on=0)   # lots of soft credit missing
        first = loop.decide(self.sealed, meas(self.sealed, base), [])  # the first loop, promoted with a low score
        better = dict(base, vib_iou_min=0.7, vib_disp_ratio_min=2.0, ripples_all_on=1, rest_iou_min=0.86)   # rest_iou_min still passes but dropped
        # decide on a changed page with the history
        e = loop.decide(self.sealed, meas(self.sealed, better, page="p2"), [first])
        self.assertGreater(e["score"], first["score"])  # the score really did rise above the baseline
        # vetoed, and the reason calls it a probe artefact
        self.assertEqual(e["decision"], "veto"); self.assertIn("probe artefact", " ".join(e["reasons"]))

    # A guard that gets worse while the score falls is vetoed as a plain regression. The reason must not call it a probe
    # artefact.
    def test_guard_regression_with_lower_score_is_not_called_an_artefact(self):
        first = loop.decide(self.sealed, meas(self.sealed, GOOD), [])  # perfect first loop, promoted as baseline
        worse = dict(GOOD, signature_amp_corr=0.0)  # amplitude correlation drops to 0, which also fails its gate
        # decide on a changed page with history
        e = loop.decide(self.sealed, meas(self.sealed, worse, page="p2"), [first])
        self.assertEqual(e["decision"], "veto")  # vetoed because the guard regressed
        text = " ".join(e["reasons"])  # all veto reasons joined into one string
        # plain "got worse" wording, without the artefact label
        self.assertIn("guard signature_amp_corr got worse", text); self.assertNotIn("probe artefact", text)

    # A measurement carrying another rubric's hash is vetoed. The loop never judges a measurement by rules it was not
    # made for.
    def test_measurement_from_another_rubric_vetoes(self):
        m = meas(self.sealed, GOOD); m["rubric_sha256"] = "0" * 64  # replace the rubric hash with zeros
        e = loop.decide(self.sealed, m, [])  # first loop, no history needed
        # vetoed with the "different rubric" reason
        self.assertEqual(e["decision"], "veto"); self.assertIn("different rubric", " ".join(e["reasons"]))

    # A measurement older than the seal is vetoed. The rules must exist before anything is measured.
    def test_measurement_older_than_seal_vetoes(self):
        # timestamp from the year 2000, long before the seal
        e = loop.decide(self.sealed, meas(self.sealed, GOOD, at="2000-01-01T00:00:00+00:00"), [])
        # vetoed with the older than seal reason
        self.assertEqual(e["decision"], "veto"); self.assertIn("older than the rubric seal", " ".join(e["reasons"]))

    # credit() gives partial credit for near misses. A min metric earns value/threshold, a max metric threshold/value,
    # an unmeasured one 0.
    def test_credit_shapes(self):
        self.assertEqual(loop.credit("min", 0.8, 0.4), 0.5)  # 0.4 of a 0.8 minimum earns half credit
        self.assertEqual(loop.credit("max", 10, 20), 0.5)  # 20 against a maximum of 10 earns half credit
        self.assertEqual(loop.credit("min", 1, None), 0.0)  # a None value earns no credit at all

    # A rubric with an unknown operator is refused when it is sealed. Only "min" and "max" exist.
    def test_bad_rubrics_are_refused(self):
        # write a rubric whose single metric uses op "near"
        p = self.dir / "bad.json"; p.write_text(json.dumps({"schema": loop.RUBRIC_SCHEMA, "metrics": {"x": {"op": "near", "value": 1}}}))
        with self.assertRaises(loop.LoopError):  # expect a validation error from seal
            loop.seal(p, Path(tempfile.mkdtemp()))  # sealing it into a new folder must fail


if __name__ == "__main__":  # run the tests when the file is executed directly
    unittest.main()  # unittest finds LoopTests in this module
