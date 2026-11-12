"""Cue provenance remains exact across regenerated positional SpeechKit IDs."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from speechkit_narrate.project import Project
from speechkit_narrate.text import docmodel

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


if __name__ == "__main__":
    unittest.main()
