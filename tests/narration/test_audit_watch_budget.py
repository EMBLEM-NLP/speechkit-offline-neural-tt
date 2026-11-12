"""Watch propagation and bounded repair regressions using deterministic recognizer stubs."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from speechkit_narrate import audit
from speechkit_narrate.audit import autofix
from emblem_rsi.pron import asr, measure
from tests.narration.test_pron_asr import req, tr


class WatchCoverage(unittest.TestCase):
    def test_project_watch_terms_reach_measurer(self):
        project = SimpleNamespace(cfg={"audit": {"watch": ["MCP", "M C P"]}}, p=lambda key: Path(key))
        receiver = mock.Mock()
        with mock.patch.object(audit, "configure", return_value={"measure": receiver}), \
             mock.patch.object(audit, "run_dir", return_value=Path("run")):
            audit.measure(project, "pilot")
        self.assertEqual(receiver.measure.call_args.kwargs["watch"], ["MCP", "M C P"])

    def test_measure_forwards_watch_to_recognizer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); run = root / "run"; run.mkdir()
            request = root / "request.json"
            request.write_text(json.dumps(req(("s0", ["M C P"]))), encoding="utf-8")
            class ReachedRecognizer(Exception):
                pass
            with mock.patch.object(measure.loop, "load_sealed", return_value={"sha256": "test"}), \
                 mock.patch.object(measure.asr, "run", side_effect=ReachedRecognizer) as recognize:
                with self.assertRaises(ReachedRecognizer):
                    measure.measure(run, request, None, root / "speech", None, watch=["M C P"])
            self.assertEqual(recognize.call_args.kwargs["watch"], ["M C P"])

    def test_watched_expanded_acronym_selects_confirmation_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); speech = root / "speech"; speech.mkdir()
            request = root / "request.json"
            request.write_text(json.dumps(req(("s0", ["M C P", "bridge"]),
                                              ("s1", ["Blender", "scene"]))), encoding="utf-8")
            for sid in ("s0", "s1"):
                (speech / f"{sid}.wav").write_bytes(b"fixture")
            model = root / "model.onnx"; model.write_bytes(b"model")
            calls = []
            def transcribe(paths, role, **kw):
                ids = [p.stem for p in paths]; calls.append((role, ids))
                kw["stats"].update(model="fixture", segments=len(ids))
                return {sid: tr("m c p bridge" if sid == "s0" else "blender scene") for sid in ids}
            with mock.patch.object(asr, "transcribe", side_effect=transcribe), \
                 mock.patch.object(asr, "resolve_model", return_value={"name": "fixture", "kind": "ctc",
                                                                       "paths": {"model": str(model)}}):
                result = asr.run(request, speech, root / "out", docmodel_path=None,
                                 watch=["M C P"], log=lambda _: None)
            self.assertEqual(calls, [("ctc", ["s0", "s1"]), ("confirm", ["s0"])])
            self.assertEqual(result["counts"]["confirm_segments"], 1)
            self.assertEqual(result["inputs"]["missing_wavs"], [])
            self.assertTrue(all(result["watch"][letter]["confirm"] for letter in ("m", "c", "p")))


class RepairBudget(unittest.TestCase):
    def test_bad_take_counts_stop_before_model_configuration(self):
        with mock.patch.object(autofix, "configure") as configure:
            for takes, retakes in ((0, 0), (True, 0), (1, -1), (1, True)):
                with self.assertRaises(ValueError):
                    autofix.autofix(SimpleNamespace(cfg={}), takes=takes, retake_takes=retakes)
        configure.assert_not_called()

    def test_zero_retakes_skips_revoice_and_budget_defers_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = SimpleNamespace(cfg={"voice": {"workers": 1},
                                           "audit": {"max_fix_words": 3, "max_occurrences_per_word": 2}},
                                      root=root, p=lambda key: root / key)
            words = {f"term{i}": {"variants": [f"spoken{i}"]} for i in range(5)}
            cli = mock.Mock()
            trial = mock.Mock(); trial.run.return_value = {"summary": {}}
            modules = {"cli": cli, "trial": trial, "asr": SimpleNamespace(_RECOGNIZERS={})}
            with mock.patch.object(autofix, "configure", return_value=modules), \
                 mock.patch.object(autofix, "run_dir", return_value=root), \
                 mock.patch.object(autofix, "status", return_value=[{"baseline": True}]), \
                 mock.patch.object(autofix, "latest", return_value={"asr": {"confirmed": [{"word": "bad", "segs": ["s0"]}]}}), \
                 mock.patch.object(autofix, "proposals", return_value=(words, {})), \
                 mock.patch.object(autofix, "choose", return_value={}):
                result = autofix.autofix(project, takes=1, retake_takes=0, log=lambda _: None)
            cli.retake.assert_not_called()
            self.assertEqual(list(result["deferred_words"]), ["term3", "term4"])
            plan = trial.run.call_args.args[0]
            self.assertEqual(plan["max_occurrences"], 2)
            self.assertEqual(len(plan["words"]), 3)
            self.assertEqual(trial.run.call_args.kwargs["workers"], 1)


if __name__ == "__main__":
    unittest.main()
