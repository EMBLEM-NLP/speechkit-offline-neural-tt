"""Source ledger negative controls, with all writes confined to temporary fixtures."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('source_ledger', Path(__file__).parents[1] / 'scripts/source_ledger.py')
ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger)


class SourceLedgerTests(unittest.TestCase):
    def test_source_corruption_and_new_file_are_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'BUNDLE_MANIFEST.json').write_text(json.dumps({'archives': []}))
            (root / 'README.md').write_text('original')
            (root / 'SHA256SUMS').write_text(''.join(f'{v}  {n}\n' for n, v in ledger.expected(root).items()))
            self.assertEqual(ledger.verify(root)['status'], 'passed')
            (root / 'README.md').write_text('modified')
            with self.assertRaisesRegex(ValueError, 'README.md'):
                ledger.verify(root)
            (root / 'README.md').write_text('original')
            (root / 'scripts').mkdir()
            (root / 'scripts/new.py').write_text('pass\n')
            with self.assertRaisesRegex(ValueError, 'scripts/new.py'):
                ledger.verify(root)

    def test_absent_runtime_is_reported_and_wrong_present_bytes_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'archives/runtime.zip'
            (root / 'BUNDLE_MANIFEST.json').write_text(json.dumps({'archives': [
                {'path': 'archives/runtime.zip', 'sha256': '0' * 64}]}))
            (root / 'SHA256SUMS').write_text(''.join(f'{v}  {n}\n' for n, v in ledger.expected(root).items()))
            self.assertEqual(ledger.verify(root)['runtime_archives_not_fetched'], ['archives/runtime.zip'])
            path.parent.mkdir()
            path.write_bytes(b'wrong')
            with self.assertRaisesRegex(ValueError, 'runtime.zip'):
                ledger.verify(root)
