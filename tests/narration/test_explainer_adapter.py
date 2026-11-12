"""Cue provenance remains exact across regenerated positional SpeechKit IDs."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from speechkit_narrate.project import Project
from speechkit_narrate.text import docmodel
from tests.narration.test_narration_fixes import _Scratch, fake_narration, quiet

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/explainer_narration.py"
spec = importlib.util.spec_from_file_location("explainer_narration", SCRIPT)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class ExplainerMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "work").mkdir()
        self.package = {"schema_version": 1, "title": "Extra title", "cue_count": 2, "scenes": [{
            "id": "scene-a", "cues": [
                {"id": "cue-a", "scene_id": "scene-a", "text": "MCP sends JSON."},
                {"id": "cue-b", "scene_id": "scene-a", "text": "MCP sends JSON."}]}]}
        (self.root / "project.json").write_text(json.dumps({"title": "Extra title", "spoken": {
            "read_cover": False, "code_names": True}}))
        (self.root / "work/book.md").write_text("MCP sends JSON.\n\nMCP sends JSON.\n")
        docmodel.build(Project(self.root))

    def test_identical_cues_keep_distinct_ids_and_exact_display(self):
        mapping = adapter.cue_mapping(self.package, self.root)
        self.assertEqual([r["cue_id"] for r in mapping["cues"]], ["cue-a", "cue-b"])
        self.assertEqual([r["segment_id"] for r in mapping["cues"]], ["s0000", "s0001"])
        self.assertTrue(all(r["text"] == "MCP sends JSON." for r in mapping["cues"]))
        self.assertTrue(all("M C P" in r["spoken_text"] for r in mapping["cues"]))

    def test_unexpected_text_change_is_rejected(self):
        (self.root / "work/book.md").write_text("MCP sends something else.\n\nMCP sends JSON.\n")
        docmodel.build(Project(self.root))
        with self.assertRaisesRegex(ValueError, "changed"):
            adapter.cue_mapping(self.package, self.root)

    def test_extra_title_narration_is_rejected(self):
        p = Project(self.root)
        p.cfg["spoken"]["read_cover"] = True
        docmodel.build(p)
        with self.assertRaisesRegex(ValueError, "Cover"):
            adapter.cue_mapping(self.package, self.root)

    def test_request_token_mismatch_is_rejected(self):
        path = self.root / "work/request.json"
        request = json.loads(path.read_text())
        request["segments"][0]["tokenIndex"] = [999]
        path.write_text(json.dumps(request))
        with self.assertRaisesRegex(ValueError, "disagree"):
            adapter.cue_mapping(self.package, self.root)

    def test_missing_native_audio_is_rejected(self):
        mapping = adapter.cue_mapping(self.package, self.root)
        with self.assertRaises(FileNotFoundError):
            adapter.verify_native_audio(self.root, mapping)

    def test_empty_or_reused_source_ids_rejected(self):
        package = copy.deepcopy(self.package)
        package["scenes"][0]["cues"][1]["id"] = "cue-a"
        path = self.root / "package.json"
        path.write_text(json.dumps(package))
        with self.assertRaisesRegex(ValueError, "unique"):
            adapter.read_package(path)

    def test_watch_terms_expand_acronyms_without_changing_captions(self):
        path = self.root / 'watch.json'
        path.write_text(json.dumps({'terms': ['MCP', 'JSON', 'absent']}))
        mapping = adapter.cue_mapping(self.package, self.root)
        watch = adapter.watch_plan(path, self.root, mapping)
        self.assertTrue({'mcp', 'm', 'c', 'p', 'json'}.issubset(watch['audit_watch']))
        self.assertEqual(watch['terms'][0]['matched_token_ids'], [2, 5])
        self.assertFalse(watch['terms'][-1]['present_in_authored_text'])
        self.assertEqual(mapping['cues'][0]['text'], 'MCP sends JSON.')

    def test_measure_only_command_explicitly_skips_autofix(self):
        args = adapter.narration_args(self.root, audit=True)
        self.assertEqual(args[:4], ['run', str(self.root), 'audit', 'export'])
        self.assertIn('--skip', args)
        self.assertEqual(args[args.index('--skip') + 1], 'autofix')
        self.assertNotIn('--options', args)

    def test_optional_autofix_has_bounded_takes(self):
        args = adapter.narration_args(self.root, autofix=True)
        opts = json.loads(args[args.index('--options') + 1])
        self.assertEqual(opts, {'autofix': {'takes': 1, 'retakes': 0}})

    def test_missing_audit_evidence_is_not_passed(self):
        mapping = adapter.cue_mapping(self.package, self.root)
        with self.assertRaisesRegex(ValueError, 'No pronunciation measurement'):
            adapter.verify_audit(self.root, mapping, {'audit_watch': ['mcp']})


class ExplainerExportValidationTests(_Scratch):
    def setUp(self):
        super().setUp()
        from speechkit_narrate import audio
        self.project = self.scripted("MCP sends JSON.\n", settings={"spoken": {"read_cover": False},
                                                                   "voice": {"profile": "ljspeech-high"}})
        fake_narration(self.project)
        audio.export(self.project, log=quiet)
        self.manifest_path = self.project.p("out_narration") / "manifest.json"

    def measurement(self):
        from emblem_rsi.pron import measure, trial
        project = self.project
        request = json.loads(project.p("request").read_text(encoding="utf-8"))
        model = json.loads(project.p("docmodel").read_text(encoding="utf-8"))
        folder = project.p("rsi") / "pron/measure-001"
        reference = folder / "request.ref.json"
        adapter.write_json(reference, {**request, **trial.reference_request(request, model)})
        asr = folder / "asr/asr_measure.json"
        adapter.write_json(asr, {"inputs": {"request_sha256": adapter.sha(reference), "missing_wavs": []},
                                 "segments": {s["id"]: {} for s in request["segments"]}})
        path = folder.with_suffix(".json")
        metrics = {"asr_segments_coverage": 1.0, "words_tracked_ratio": 1.0}
        adapter.write_json(path, {"page_sha256": measure.page_sha256(project.p("request"), project.p("pronunciations")),
                                  "signature_sha256": measure.signature_sha256(project.p("speech"),
                                                                              [s["id"] for s in request["segments"]]),
                                  "metrics": metrics})
        return {"status": "coverage_complete", "measurement": str(path.relative_to(project.root)), "measurement_sha256": adapter.sha(path),
                "asr": str(asr.relative_to(project.root)), "asr_sha256": adapter.sha(asr), "metrics": metrics}

    def test_current_export_without_requested_audit_is_valid(self):
        result = adapter.verify_generic_export(self.project.root)
        self.assertEqual(result["status"], "current_export_validated")
        self.assertEqual(result["voice_approval"], "PENDING")
        self.assertIsNone(result["measurement"])

    def test_pre_audit_export_is_rejected_after_measurement(self):
        summary = self.measurement()
        with self.assertRaisesRegex(ValueError, "export input identity changed"):
            adapter.verify_generic_export(self.project.root, summary)

    def test_adapter_cannot_finish_with_valid_native_audio_and_stale_export(self):
        summary = self.measurement()
        output = self.tmp / "adapter-output"
        shutil.copytree(self.project.root, output / "project")
        package = {"schema_version": 1, "title": "Test Text", "cue_count": 1, "scenes": [{
            "id": "scene-a", "cues": [{"id": "cue-a", "scene_id": "scene-a", "text": "MCP sends JSON."}]}]}
        source = self.tmp / "package.json"
        watches = self.tmp / "watch.json"
        adapter.write_json(source, package)
        adapter.write_json(output / "adapter_source.json", package)
        adapter.write_json(watches, {"terms": ["MCP", "JSON"]})
        with mock.patch.object(adapter, "verify_audit", return_value=summary):
            with self.assertRaisesRegex(ValueError, "export input identity changed"):
                adapter.main(["--input-package", str(source), "--output-root", str(output),
                              "--stage", "validate", "--audit", "--watchlist", str(watches)])
        state = json.loads((output / "run_status.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed")
        self.assertIn("export input identity changed", state["error"])
        self.assertFalse((output / "generic_export_validation.json").exists())

    def test_current_export_binds_verified_measurement_and_stays_pending(self):
        from speechkit_narrate import audio
        summary = self.measurement()
        before = {p.name: adapter.sha(p) for p in self.project.p("speech").iterdir()}
        audio.export(self.project, log=quiet)
        result = adapter.verify_generic_export(self.project.root, summary)
        self.assertEqual(result["status"], "current_export_validated")
        self.assertEqual(result["pronunciation_approval"], "PENDING")
        self.assertEqual(result["measurement"]["sha256"], summary["measurement_sha256"])
        self.assertEqual(before, {p.name: adapter.sha(p) for p in self.project.p("speech").iterdir()})
        altered = {**summary, "measurement_sha256": "stale"}
        with self.assertRaisesRegex(ValueError, "does not bind"):
            adapter.verify_generic_export(self.project.root, altered)

    def test_removed_provenance_cannot_pass_with_valid_input_identity(self):
        from speechkit_narrate import audio
        summary = self.measurement()
        audio.export(self.project, log=quiet)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["pronunciationAudit"] = None
        adapter.write_json(self.manifest_path, manifest)
        self.assertEqual(audio.manifest_issues(self.project), [])
        with self.assertRaisesRegex(ValueError, "provenance is stale"):
            adapter.verify_generic_export(self.project.root, summary)

    def test_generated_audio_cannot_claim_listening_approval(self):
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["voiceApproval"] = "APPROVED"
        adapter.write_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "PENDING"):
            adapter.verify_generic_export(self.project.root)


if __name__ == "__main__":
    unittest.main()
