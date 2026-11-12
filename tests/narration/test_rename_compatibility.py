# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Installed names, plugin discovery and existing data remain usable across the public rename."""
from importlib.metadata import PackageNotFoundError
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from speechkit_narrate import env, mcp_server, registry
from speechkit_narrate.text import codenames


class DistributionCompatibility(unittest.TestCase):
    def test_canonical_and_legacy_installed_metadata(self):
        names = ['speechkit-voice-tts-offline-pipeline', 'speechkit_voice_tts_offline_pipeline',
                 'speechkit-narrate', 'speechkit_narrate']
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(env, 'REPO', Path(folder)):
            for name in names:
                with self.subTest(distribution=name):
                    record = Path(folder) / f'{name}-9.9.9.dist-info'
                    record.mkdir()
                    self.assertTrue(env.installed())
                    record.rmdir()
            self.assertFalse(env.installed())

    def test_checkout_discovery_prefers_canonical_and_keeps_legacy(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict('os.environ', {}, clear=True):
            canonical = Path(folder) / 'speechkit-voice-tts-offline-pipeline'
            legacy = Path(folder) / 'speechkit-offline-neural-tt'
            self.assertEqual(env._first('SPEECHKIT_CHECKOUT', canonical, legacy, default=canonical), canonical)
            legacy.mkdir()
            self.assertEqual(env._first('SPEECHKIT_CHECKOUT', canonical, legacy, default=canonical), legacy)
            canonical.mkdir()
            self.assertEqual(env._first('SPEECHKIT_CHECKOUT', canonical, legacy, default=canonical), canonical)
            with mock.patch.dict('os.environ', {'SPEECHKIT_CHECKOUT': str(legacy)}):
                self.assertEqual(env._first('SPEECHKIT_CHECKOUT', canonical, legacy, default=canonical), legacy)

    def test_installed_version_uses_canonical_then_legacy_metadata(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(mcp_server, 'ROOT', Path(folder)):
            for installed, expected in [('speechkit-voice-tts-offline-pipeline', '1.3.0'), ('speechkit-narrate', '1.2.0')]:
                def version(name):
                    if name != installed:
                        raise PackageNotFoundError(name)
                    return expected
                with self.subTest(installed=installed), mock.patch('importlib.metadata.version', side_effect=version):
                    self.assertEqual(mcp_server.toolkit_version(), expected)
            with mock.patch('importlib.metadata.version', side_effect=PackageNotFoundError):
                self.assertEqual(mcp_server.toolkit_version(), 'dev')

    def test_source_version_still_precedes_installed_metadata(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(mcp_server, 'ROOT', Path(folder)):
            (Path(folder) / 'pyproject.toml').write_text('[project]\nversion = "9.8.7"\n', encoding='utf-8')
            with mock.patch('importlib.metadata.version', side_effect=AssertionError('metadata must not be read')):
                self.assertEqual(mcp_server.toolkit_version(), '9.8.7')


class PluginCompatibility(unittest.TestCase):
    def test_dual_groups_register_a_real_plugin_once(self):
        entry = SimpleNamespace(value='tests.narration.fixtures.reading_stats:register')
        def entries(*, group):
            return [entry] if group in ('speechkit_voice_tts_offline_pipeline.plugins', 'speechkit_narrate.plugins') else []
        with mock.patch('importlib.metadata.entry_points', side_effect=entries), mock.patch.dict('os.environ', {'SPEECHKIT_PLUGINS': ''}):
            reg = registry.load()
        self.assertIn('stats', reg.stages)
        self.assertIn('wordcount', reg.tools)
        self.assertEqual(len(reg.plugins), 1)

    def test_legacy_entry_point_group_still_loads(self):
        entry = SimpleNamespace(value='tests.narration.fixtures.reading_stats')
        with mock.patch('importlib.metadata.entry_points', side_effect=lambda *, group: [entry] if group == 'speechkit_narrate.plugins' else []), mock.patch.dict('os.environ', {'SPEECHKIT_PLUGINS': ''}):
            reg = registry.load()
        self.assertIn('wordcount', reg.tools)
        self.assertEqual(reg.stages['narrate'].source, 'speechkit-narrate')
        self.assertIn('speechkit-narrate:', reg.stages['narrate'].impl)

    def test_new_name_has_its_own_spoken_form_and_preserves_old_alias(self):
        self.assertEqual(codenames.spoken('speechkit-voice-tts-offline-pipeline'), 'speech kit voice T T S offline pipeline')
        self.assertEqual(codenames.spoken('speechkit-narrate'), 'speech kit narrate')


if __name__ == '__main__':
    unittest.main()
