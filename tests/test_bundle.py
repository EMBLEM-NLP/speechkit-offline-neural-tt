# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Negative controls for speechkit-offline-neural-tt."""
import hashlib  # Compute fixture identities, not production pins.
import importlib.util  # Load the bootstrap without executing its CLI.
import tempfile  # Keep all corruption fixtures outside production archives.
import json  # Read the manifest.
import unittest  # Execute reproducible assertion-based checks.
import warnings  # Silence the intentional duplicate-member creation warning.
import zipfile  # Create small malformed archive fixtures.
from pathlib import Path  # Resolve this package independent of cwd.

ROOT = Path(__file__).resolve().parents[1]  # This distribution's real root.
SPEC = importlib.util.spec_from_file_location('bundle_bootstrap', ROOT / 'bootstrap.py')
BOOT = importlib.util.module_from_spec(SPEC)  # Import helpers under a private name.
SPEC.loader.exec_module(BOOT)  # No installation happens on import.


RUNTIME_PATH = ROOT / 'archives' / BOOT.RUNTIME  # Release asset; absent in a fresh git clone.
NEEDS_RUNTIME = unittest.skipUnless(RUNTIME_PATH.is_file(), 'runtime not fetched: run bootstrap.py fetch')


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()  # Each test has an isolated fixture folder.
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()  # Only remove test-generated files.

    def make_zip(self, entries):
        path = self.root / 'fixture.zip'  # Explicit fixture, never a supplied component.
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)  # Duplicate names are deliberately tested.
            with zipfile.ZipFile(path, 'w') as archive:
                for name, data in entries:
                    archive.writestr(name, data)  # Build only the small declared fixture.
        return path

    def inspect(self, entries):
        path = self.make_zip(entries)
        return BOOT.inspect_archive(path, BOOT.sha(path))  # Pin the fixture for internal tests.

    @NEEDS_RUNTIME
    def test_runtime_file_exists_in_distribution(self):
        self.assertTrue((ROOT / 'archives' / BOOT.RUNTIME).is_file())

    @NEEDS_RUNTIME
    def test_runtime_is_not_an_empty_placeholder(self):
        self.assertGreater((ROOT / 'archives' / BOOT.RUNTIME).stat().st_size, 200_000_000)

    def test_missing_runtime_is_explicit_failure(self):
        with self.assertRaisesRegex(FileNotFoundError, 'bootstrap.py fetch'):
            BOOT.inspect_archive(self.root / BOOT.RUNTIME, BOOT.PINS[BOOT.RUNTIME])

    def test_wrong_outer_hash_is_rejected(self):
        path = self.make_zip([('data.txt', b'x')])
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            BOOT.inspect_archive(path, '0' * 64)

    def test_good_internal_ledger(self):
        digest = hashlib.sha256(b'hello').hexdigest()
        result = self.inspect([('data.txt', b'hello'), ('SHA256SUMS', digest + '  data.txt\n')])
        self.assertEqual(result['internalChecksumRecordsPassed'], 1)

    def test_corrupt_member_ledger_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Internal checksum failed'):
            self.inspect([('data.txt', b'hello'), ('SHA256SUMS', '0' * 64 + '  data.txt\n')])

    def test_missing_internal_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'missing member'):
            self.inspect([('SHA256SUMS', '0' * 64 + '  absent.txt\n')])

    def test_nested_internal_ledger_is_supported(self):
        digest = hashlib.sha256(b'ok').hexdigest()
        result = self.inspect([('component/file.txt', b'ok'),
                               ('component/SHA256SUMS', digest + '  file.txt\n')])
        self.assertEqual(result['internalChecksumRecordsPassed'], 1)

    def test_traversal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            self.inspect([('../escape', b'bad')])

    def test_drive_path_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            self.inspect([('C:/escape', b'bad')])

    def test_backslash_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            self.inspect([('..\\escape', b'bad')])

    def test_duplicate_member_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.inspect([('same', b'one'), ('same', b'two')])

    def test_symlink_is_rejected(self):
        member = zipfile.ZipInfo('link')  # Mark a fixture member as a Unix symbolic link.
        member.create_system = 3
        member.external_attr = 0o120777 << 16
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.inspect([(member, b'outside')])

    def test_checksum_path_traversal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            self.inspect([('SHA256SUMS', '0' * 64 + '  ../escape\n')])

    @NEEDS_RUNTIME
    def test_historic_runtime_identity_is_preserved(self):
        self.assertEqual(BOOT.sha(ROOT / 'archives' / BOOT.RUNTIME), BOOT.PINS[BOOT.RUNTIME])


    def test_toolkit_is_in_repo_and_standalone(self):
        self.assertTrue((ROOT / 'speechkit' / 'src' / 'voice.py').is_file())
        for path in (ROOT / 'speechkit').rglob('*'):
            if path.suffix in ('.py', '.json', '.md'):
                text = path.read_text(errors='ignore').lower()
                for other in ('obc-study', 'video-factory', 'orchestrator'):
                    self.assertNotIn(other, text, f'{path} references {other}')

    def test_license_is_gpl3(self):
        self.assertIn('GNU GENERAL PUBLIC LICENSE', (ROOT / 'LICENSE').read_text()[:200])

    def test_manifest_matches_bootstrap_pins(self):
        manifest = json.loads((ROOT / 'BUNDLE_MANIFEST.json').read_text())
        self.assertEqual(manifest['distributionVersion'], BOOT.DIST_VERSION)
        self.assertEqual({Path(a['path']).name: a['sha256'] for a in manifest['archives']}, BOOT.PINS)
        self.assertEqual(manifest['license'], 'GPL-3.0-or-later')

    def test_fetch_rejects_plain_http(self):
        with self.assertRaisesRegex(ValueError, 'https'):
            BOOT.fetch(self.root, 'http://example.invalid/x.zip')

    def test_fetch_rejects_wrong_hash_and_leaves_nothing(self):
        source = self.root / 'bad.zip'
        source.write_bytes(b'not the runtime')
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            self._fetch_local(source)
        self.assertFalse((self.root / 'archives' / BOOT.RUNTIME).exists())

    def _fetch_local(self, source):
        import urllib.request
        original = urllib.request.urlopen
        urllib.request.urlopen = lambda url, timeout=0: source.open('rb')  # Offline stand-in for the release.
        try:
            return BOOT.fetch(self.root, 'https://example.invalid/runtime.zip')
        finally:
            urllib.request.urlopen = original


if __name__ == '__main__':
    unittest.main()  # This test file never installs or synthesizes implicitly.
