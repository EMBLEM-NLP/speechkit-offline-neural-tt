"""Audio model integrity and alignment/cache regressions without neural inference."""
from __future__ import annotations

import ast
import difflib
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from speechkit_narrate import env
from emblem_rsi.pron import asr


def digest(data):
    return hashlib.sha256(data).hexdigest()


class PinnedModels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.name = "test-model"
        self.members = {"model.onnx": b"weights", "tokens.txt": b"tokens"}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
            for name, data in self.members.items():
                member = tarfile.TarInfo(self.name + "/" + name)
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
        self.archive = buf.getvalue()
        self.lock = {self.name: {"url": "https://example.invalid/model", "bytes": len(self.archive),
                                "sha256": digest(self.archive),
                                "members": {n: {"sha256": digest(d), "bytes": len(d)}
                                            for n, d in self.members.items()}}}
        for patch in (mock.patch.object(env, "ASR_MODELS", self.root / "models"),
                      mock.patch.object(env, "CACHE", self.root / "cache"),
                      mock.patch.object(env, "asr_lock", return_value=self.lock)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_verified_archive_is_staged_and_all_members_checked(self):
        with mock.patch.object(env.urllib.request, "urlopen", return_value=io.BytesIO(self.archive)):
            env.install_asr_model(self.name, lambda _: None)
        self.assertEqual(env.verify_asr_model(self.name), [])
        (env.ASR_MODELS / self.name / "model.onnx").write_bytes(b"changed")
        self.assertEqual(env.verify_asr_model(self.name), ["changed model.onnx"])
        with self.assertRaisesRegex(RuntimeError, "invalid ASR model"):
            env.install_asr_model(self.name, lambda _: None)

    def test_verified_archive_receipt_binds_required_members_and_model(self):
        spec = self.lock[self.name]
        spec["requiredMembers"] = list(spec.pop("members"))
        with mock.patch.object(env.urllib.request, "urlopen", return_value=io.BytesIO(self.archive)):
            env.install_asr_model(self.name, lambda _: None)
        self.assertEqual(env.verify_asr_model(self.name), [])
        receipt = env.ASR_MODELS / self.name / ".speechkit-install.json"
        record = json.loads(receipt.read_text())
        self.assertEqual(record["modelName"], self.name)
        self.assertEqual(record["archiveSha256"], digest(self.archive))
        self.assertEqual(set(record["members"]), set(self.members))
        record["modelName"] = "different-model"
        receipt.write_text(json.dumps(record))
        self.assertEqual(env.verify_asr_model(self.name), ["missing or invalid verified-install receipt"])

    def test_tokens_only_is_incomplete(self):
        p = env.ASR_MODELS / self.name
        p.mkdir(parents=True)
        (p / "tokens.txt").write_bytes(b"tokens")
        self.assertEqual(env.verify_asr_model(self.name), ["missing model.onnx"])

    def test_bad_download_does_not_publish(self):
        with mock.patch.object(env.urllib.request, "urlopen", return_value=io.BytesIO(b"not the archive")):
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                env.install_asr_model(self.name, lambda _: None)
        self.assertFalse((env.ASR_MODELS / self.name).exists())


class DerivedModel(unittest.TestCase):
    def test_modified_derived_cache_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "voice.onnx"
            src.write_bytes(b"original weights")
            calls = []
            def save(model, path):
                calls.append(path)
                Path(path).write_bytes(b"original weights and duration output")
            fake = SimpleNamespace(__version__="test", load=lambda p: SimpleNamespace(
                graph=SimpleNamespace(node=[SimpleNamespace(op_type="Ceil", output=["durations"])], output=[])),
                save=save, helper=SimpleNamespace(ValueInfoProto=lambda: SimpleNamespace()))
            with mock.patch.object(env, "CACHE", root / "cache"), \
                 mock.patch.object(env, "voice_model", return_value=src), \
                 mock.patch.object(env, "profile", return_value={"modelSha256": digest(src.read_bytes())}), \
                 mock.patch.dict("sys.modules", {"onnx": fake}):
                path = env.aligned_model("test")
                self.assertEqual(len(calls), 1)
                self.assertEqual(env.aligned_model("test"), path)
                self.assertEqual(len(calls), 1)
                path.write_bytes(b"tampered derived model")
                env.aligned_model("test")
                self.assertEqual(len(calls), 2)
                receipt = json.loads(path.with_suffix(path.suffix + ".json").read_text())
                self.assertEqual(receipt["derivedSha256"], digest(path.read_bytes()))


class RecognitionIdentity(unittest.TestCase):
    def test_model_and_resampler_changes_invalidate_cache(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weights = root / "model.onnx"; weights.write_bytes(b"version one")
            tokens = root / "tokens.txt"; tokens.write_text("words")
            wav = root / "s0001.wav"; wav.write_bytes(b"unchanged audio fixture")
            spec = {"name": "test-model", "kind": "nemo_ctc", "frame_s": .04,
                    "paths": {"model": str(weights), "tokens": str(tokens)}}
            result = SimpleNamespace(text="hello", tokens=[" hello"], timestamps=[0.0])
            rec = SimpleNamespace(create_stream=lambda: SimpleNamespace(result=result, accept_waveform=lambda *a: None),
                                  decode_stream=lambda stream: None)
            with mock.patch.object(asr, "resolve_model", return_value=spec), \
                 mock.patch.object(asr, "_recognizer", return_value=rec) as load, \
                 mock.patch.object(asr, "load_audio_16k", return_value=np.zeros(16000, dtype=np.float32)):
                a = asr.transcribe([wav], cache_dir=root / "cache")
                b = asr.transcribe([wav], cache_dir=root / "cache")
                self.assertEqual(a, b)
                self.assertEqual(load.call_count, 1)
                weights.write_bytes(b"version two with a new shape")
                c = asr.transcribe([wav], cache_dir=root / "cache")
                self.assertEqual(load.call_count, 2)
                self.assertNotEqual(a["s0001"]["recognizer_identity"], c["s0001"]["recognizer_identity"])
                with mock.patch.object(asr.importlib.metadata, "version", return_value="another version"):
                    d = asr.transcribe([wav], cache_dir=root / "cache")
                self.assertEqual(load.call_count, 3)
                self.assertNotEqual(c["s0001"]["recognizer_identity"], d["s0001"]["recognizer_identity"])


    def test_bad_cache_shapes_and_hashes_are_decoded_again(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weights = root / "model.onnx"; weights.write_bytes(b"weights")
            wav = root / "s0.wav"; wav.write_bytes(b"audio")
            spec = {"name": "fixture", "kind": "nemo_ctc", "frame_s": .04, "paths": {"model": str(weights)}}
            result = SimpleNamespace(text="hello", tokens=[" hello"], timestamps=[0.0])
            rec = SimpleNamespace(create_stream=lambda: SimpleNamespace(result=result, accept_waveform=lambda *a: None),
                                  decode_stream=lambda stream: None)
            with mock.patch.object(asr, "resolve_model", return_value=spec), \
                 mock.patch.object(asr, "_recognizer", return_value=rec) as load, \
                 mock.patch.object(asr, "load_audio_16k", return_value=np.zeros(16000, dtype=np.float32)):
                asr.transcribe([wav], cache_dir=root / "cache")
                cache = next((root / "cache").rglob("*.json"))
                for value in ([], None):
                    cache.write_text(json.dumps(value))
                    asr.transcribe([wav], cache_dir=root / "cache")
                value = json.loads(cache.read_text()); value["sha256"] = "wrong"
                cache.write_text(json.dumps(value))
                asr.transcribe([wav], cache_dir=root / "cache")
                value = json.loads(cache.read_text()); value["words"] = [None]
                cache.write_text(json.dumps(value))
                asr.transcribe([wav], cache_dir=root / "cache")
                self.assertEqual(load.call_count, 5)

    def test_loaded_recognizer_is_not_reused_for_changed_weights(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); weights = root / "model.onnx"; weights.write_bytes(b"one")
            tokens = root / "tokens.txt"; tokens.write_bytes(b"tokens")
            spec = {"name": "fixture", "kind": "nemo_ctc", "frame_s": .04,
                    "paths": {"model": str(weights), "tokens": str(tokens)}}
            loader = mock.Mock(side_effect=[object(), object()])
            fake = SimpleNamespace(OfflineRecognizer=SimpleNamespace(from_nemo_ctc=loader))
            with mock.patch.dict("sys.modules", {"sherpa_onnx": fake}), \
                 mock.patch.object(asr, "_RECOGNIZERS", {}):
                first = asr._recognizer(spec, 1)
                self.assertIs(first, asr._recognizer(spec, 1))
                weights.write_bytes(b"two changed")
                self.assertIsNot(first, asr._recognizer(spec, 1))
                self.assertEqual(loader.call_count, 2)


class AlignmentQuality(unittest.TestCase):
    def setUp(self):
        # Load only this pure function; the worker's argparse/runtime setup must not run.
        path = Path(env.__file__).parent / "voice" / "karaoke_voice.py"
        source = ast.parse(path.read_text())
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "assign_words")
        self.ns = {"difflib": difflib, "token_phones_at": lambda t: ["a", "b"][t:t+1]}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), self.ns)

    def test_real_matches_are_distinguished_from_estimated_words(self):
        spans, quality, estimated = self.ns["assign_words"]([0, 1], [["a", 0, 10]], 0, 12)
        self.assertEqual(spans[0], [0, 10])
        self.assertEqual(estimated, [1])
        self.assertLess(quality, .8)
        self.assertTrue(0 <= spans[1][0] <= spans[1][1] <= 12)

    def test_multiple_unmatched_end_tokens_never_extend_audio(self):
        spans, _, estimated = self.ns["assign_words"]([0, 1, 2], [["a", 0, 10]], 0, 10)
        self.assertEqual(spans[1], [10, 10])
        self.assertEqual(spans[2], [10, 10])
        self.assertEqual(estimated, [1, 2])

    def test_fallback_at_end_stays_inside_audio(self):
        spans, _, estimated = self.ns["assign_words"]([0, 1], [["a", 0, 10]], 0, 10)
        self.assertEqual(spans[1], [10, 10])
        self.assertEqual(estimated, [1])


if __name__ == "__main__":
    unittest.main()
