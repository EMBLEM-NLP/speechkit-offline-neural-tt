"""An optional silent cover must not alter or silence matching body text."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from speechkit_narrate.project import Project, DEFAULTS
from speechkit_narrate.text import docmodel, markup


class ReadCoverTests(unittest.TestCase):
    def model(self, read_cover=None):
        blocks = markup.parse("Repeated title.\n\nMCP sends a JSON request.\n", "Repeated title.")
        opts = copy.deepcopy(DEFAULTS["spoken"])
        if read_cover is None:
            opts.pop("read_cover", None)
        else:
            opts["read_cover"] = read_cover
        opts["code_names"] = True
        tokens = docmodel.tokenize(blocks, opts, {}, {})
        return blocks, tokens, docmodel.segments(blocks, tokens)

    def test_existing_default_reads_cover(self):
        blocks, tokens, segments = self.model()
        self.assertEqual([s["first_block"] for s in segments], [0, 1, 2])
        self.assertTrue(all(tokens[i]["spoken"] for i in blocks[0]["tokens"]))

    def test_false_keeps_display_cover_but_no_cover_segment(self):
        blocks, tokens, segments = self.model(False)
        self.assertEqual(blocks[0]["kind"], "cover")
        self.assertEqual([tokens[i]["disp"] for i in blocks[0]["tokens"]], ["Repeated", "title."])
        self.assertTrue(all(tokens[i]["spoken"] == "" for i in blocks[0]["tokens"]))
        self.assertEqual([s["first_block"] for s in segments], [1, 2])
        self.assertEqual(segments[0]["text"], "Repeated title.")
        self.assertEqual(segments[1]["text"], "MCP sends a JSON request.")
        self.assertIn("M C P", segments[1]["spokenText"])

    def test_flag_does_not_renumber_visible_tokens(self):
        a = self.model(True)
        b = self.model(False)
        self.assertEqual([(t["i"], t["disp"], t["block"]) for t in a[1]],
                         [(t["i"], t["disp"], t["block"]) for t in b[1]])

    def test_build_emits_only_requested_body_with_original_display(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "work").mkdir()
            (root / "project.json").write_text(json.dumps({"title": "Extra title", "spoken": {
                "read_cover": False, "code_names": True}}))
            (root / "work/book.md").write_text("MCP sends JSON.\n\nMCP sends JSON.\n")
            docmodel.build(Project(root))
            request = json.loads((root / "work/request.json").read_text())
            self.assertEqual(len(request["segments"]), 2)
            self.assertEqual([s["text"] for s in request["segments"]], ["MCP sends JSON."] * 2)
            self.assertNotEqual(request["segments"][0]["id"], request["segments"][1]["id"])


if __name__ == "__main__":
    unittest.main()
