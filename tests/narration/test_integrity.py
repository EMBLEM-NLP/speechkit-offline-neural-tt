"""Negative controls for cache identity, delivery integrity and writer exclusion."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from speechkit_narrate import audio, registry as R
from speechkit_narrate.locking import project_lock, ProjectBusyError, lock_token, DELEGATE_TOKEN
from speechkit_narrate.voice import narrate as nar
from speechkit_narrate.voice.identity import (SCHEMA, digest, sha256, synthesis_identity, validate_record)
from tests.narration.test_narration_fixes import _Scratch, fake_narration, fake_identity, quiet


class Segments(_Scratch):
    BOOK = "# Example\n\nFirst sentence.\n\nSecond sentence.\n"

    def setUp(self):
        super().setUp()
        self.p = self.scripted(self.BOOK)
        fake_narration(self.p)
        self.request = json.loads(self.p.p("request").read_text(encoding="utf-8"))
        self.seg = self.request["segments"][-1]
        self.wav = self.p.p("speech") / f"{self.seg['id']}.wav"
        self.record = self.wav.with_suffix(".wav.json")
        patcher = mock.patch.object(nar, "project_identity", side_effect=fake_identity)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_wav_is_not_completed_by_its_record(self):
        self.wav.unlink()
        self.assertIn(self.seg["id"], nar.plan(self.p)["voice"])

    def test_same_size_changed_bytes_and_preserved_mtime_invalidate(self):
        before = self.wav.stat()
        payload = bytearray(self.wav.read_bytes())
        payload[-2] ^= 1
        self.wav.write_bytes(payload)
        os.utime(self.wav, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertIn(self.seg["id"], nar.plan(self.p)["voice"])

    def test_truncated_audio_is_not_a_donor(self):
        self.wav.write_bytes(self.wav.read_bytes()[:-4])
        pl = nar.plan(self.p)
        self.assertIn(self.seg["id"], pl["voice"])
        self.assertNotIn(self.seg["id"], pl["reuse"].values())

    def test_profile_identity_change_invalidates_all_segments(self):
        identity = fake_identity(self.p)
        identity["components"]["newVoice"] = "different"
        identity["sha256"] = digest(identity["components"])
        with mock.patch.object(nar, "project_identity", return_value=identity):
            self.assertEqual(len(nar.plan(self.p)["voice"]), len(self.request["segments"]))

    def test_unchanged_audio_and_pace_review_survive_resume(self):
        record = json.loads(self.record.read_text(encoding="utf-8"))
        record["paceReview"] = "REQUIRED"
        self.record.write_text(json.dumps(record), encoding="utf-8")
        before = sha256(self.wav)
        with mock.patch.object(nar, "run_workers", side_effect=AssertionError("unexpected synthesis")):
            result = nar.narrate(self.p, log=quiet)
        self.assertEqual(result["voiced"], 0)
        self.assertEqual(result["pace_review"], 1)
        self.assertEqual(sha256(self.wav), before)

    def test_nonfinite_timing_is_invalid(self):
        record = json.loads(self.record.read_text(encoding="utf-8"))
        record["words"][0]["start"] = float("nan")
        self.assertTrue(validate_record(record, self.wav, self.seg))

    def test_changed_timing_with_valid_bounds_is_not_reused(self):
        record = json.loads(self.record.read_text(encoding="utf-8"))
        record["words"][0]["end"] -= .01
        self.record.write_text(json.dumps(record), encoding="utf-8")
        self.assertIn(self.seg["id"], nar.plan(self.p)["voice"])

    def test_same_path_model_config_and_renderer_changes_affect_signature(self):
        root = self.tmp / "runtime"
        root.mkdir()
        profile = root / "profile.json"
        profile.write_text(json.dumps({"model": "model.onnx", "config": "config.json", "sampleRate": 22050}),
                           encoding="utf-8")
        for name in ("model.onnx", "config.json", "aligned.onnx", "voice.py", "renderer.py"):
            (root / name).write_text("before", encoding="utf-8")
        def identity():
            return synthesis_identity(root, profile, root / "aligned.onnx", root, root / "renderer.py")["sha256"]
        previous = identity()
        for name in ("model.onnx", "config.json", "aligned.onnx", "renderer.py"):
            (root / name).write_text("after", encoding="utf-8")
            changed = identity()
            self.assertNotEqual(previous, changed, name)
            previous = changed


class ExportIntegrity(_Scratch):
    def setUp(self):
        super().setUp()
        self.p = self.scripted("# One\n\nAlpha beta gamma.\n")
        fake_narration(self.p)
        audio.export(self.p, log=quiet)
        self.out = self.p.p("out_narration")
        self.manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))

    def test_manifest_alone_does_not_mark_delivery_complete(self):
        (self.out / self.manifest["chapters"][0]["audio"]).unlink()
        self.assertTrue(audio.manifest_issues(self.p))
        reg = R.load(self.p)
        R.record(self.p, reg.stages["export"], state := {})
        R.write_state(self.p, state)
        self.assertEqual(R.plan(self.p, reg, only=["export"])[0]["action"], "run")

    def test_same_size_export_corruption_and_words_corruption_are_detected(self):
        for key in ("audio", "timings"):
            path = self.out / self.manifest["chapters"][0][key]
            original = path.read_bytes()
            payload = bytearray(original)
            payload[-2] ^= 1
            path.write_bytes(payload)
            self.assertTrue(audio.manifest_issues(self.p), key)
            path.write_bytes(original)
        self.assertEqual(audio.manifest_issues(self.p), [])

    def test_unrelated_file_is_preserved(self):
        unrelated = self.out / "editor-notes.txt"
        unrelated.write_text("keep", encoding="utf-8")
        audio.export(self.p, log=quiet)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_failed_export_leaves_prior_delivery_identical(self):
        before = {p.name: sha256(p) for p in self.out.iterdir() if p.is_file()}
        with mock.patch.object(audio, "_write_audio", side_effect=RuntimeError("encoder failed")):
            with self.assertRaisesRegex(RuntimeError, "encoder failed"):
                audio.export(self.p, log=quiet)
        self.assertEqual({p.name: sha256(p) for p in self.out.iterdir() if p.is_file()}, before)

    def test_new_audit_invalidates_export_without_audio_change(self):
        audit = self.p.p("rsi") / "pron" / "autofix-last.json"
        audit.parent.mkdir(parents=True)
        audit.write_text('{"newAudit":true}', encoding="utf-8")
        self.assertIn("export input identity changed", audio.manifest_issues(self.p))

    def test_stale_record_is_rejected_before_replacing_delivery(self):
        request = json.loads(self.p.p("request").read_text(encoding="utf-8"))
        request["segments"][-1]["spokenText"] += " extra"
        self.p.p("request").write_text(json.dumps(request), encoding="utf-8")
        before = (self.out / "manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "invalid narration"):
            audio.export(self.p, log=quiet)
        self.assertEqual((self.out / "manifest.json").read_bytes(), before)


class PlannerIntegrity(_Scratch):
    def test_output_options_invalidate_but_workers_do_not(self):
        p = self.project("Hello.")
        st = R.Stage("custom", options={"format": {"default": "wav"}, "workers": {"default": 1}},
                     operational_options=("workers",))
        baseline = R.fingerprint(p, st)["all"]
        self.assertEqual(baseline, R.fingerprint(p, st, {"custom": {"workers": 4}})["all"])
        self.assertNotEqual(baseline, R.fingerprint(p, st, {"custom": {"format": "mp3"}})["all"])

    def test_helper_only_change_invalidates_implementation(self):
        helper = self.tmp / "helper.py"
        helper.write_text("value=1\n", encoding="utf-8")
        st = R.Stage("custom", dependencies=(str(helper),))
        first = st.impl
        helper.write_text("value=2\n", encoding="utf-8")
        self.assertNotEqual(first, st.impl)

    def test_missing_postcondition_is_failure_not_success(self):
        p = self.project("Hello.")
        reg = R.Registry()
        reg.add_stage(R.Stage("incomplete", run=lambda p, c: {}, outputs=("out/missing.txt",)))
        with self.assertRaisesRegex(R.RegistryError, "valid outputs"):
            R.run(p, reg, log=quiet)
        state = R.read_state(p)
        self.assertNotIn("incomplete", state)
        self.assertIn("incomplete", state["_errors"])

    def test_recursive_delegate_is_rejected_before_spawning(self):
        with mock.patch.dict(os.environ, {"SPEECHKIT_DELEGATION_DEPTH": "1", "SPEECHKIT_DELEGATE": "unused"}):
            with self.assertRaisesRegex(R.RegistryError, "recursive delegation"):
                R._delegate_cmd()


class WriterLocks(_Scratch):
    def test_explicit_nested_writer_can_borrow_the_coordinator_lock(self):
        p = self.project("Hello.")
        with project_lock(p.root):
            result = subprocess.run([sys.executable, "-m", "speechkit_narrate", "--json", "settings", str(p.root),
                                     "--set", '{"title":"nested writer"}'], capture_output=True, text=True,
                                    encoding="utf-8", timeout=10,
                                    env={**os.environ, DELEGATE_TOKEN: lock_token(p.root)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        p.__init__(p.root)
        self.assertEqual(p.title, "nested writer")

    def test_second_cli_writer_is_rejected_and_settings_unchanged(self):
        p = self.project("Hello.")
        before = (p.root / "project.json").read_bytes()
        with project_lock(p.root):
            result = subprocess.run([sys.executable, "-m", "speechkit_narrate", "--json", "settings", str(p.root),
                                     "--set", '{"title":"racing writer"}'], capture_output=True, text=True,
                                    encoding="utf-8", timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("busy", result.stdout)
        self.assertEqual((p.root / "project.json").read_bytes(), before)
        p.save_settings({"title": "after release"})
        self.assertEqual(p.title, "after release")

    def test_killed_owner_releases_os_lock(self):
        p = self.project("Hello.")
        code = ("from speechkit_narrate.locking import project_lock; import sys,time; "
                "\nwith project_lock(sys.argv[1]):\n print('locked',flush=True); time.sleep(30)\n")
        proc = subprocess.Popen([sys.executable, "-c", code, str(p.root)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            with self.assertRaises(ProjectBusyError):
                with project_lock(p.root):
                    pass
        finally:
            proc.kill()
            proc.communicate(timeout=5)
        with project_lock(p.root):
            p.save_settings({"title": "recovered"})
        self.assertEqual(p.title, "recovered")


if __name__ == "__main__":
    unittest.main()
