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

    def test_voice_override_changes_signature_and_empty_matches_unset(self):
        root = self.tmp / "runtime"
        root.mkdir()
        profile = root / "profile.json"
        profile.write_text(json.dumps({"model": "model.onnx", "config": "config.json"}), encoding="utf-8")
        for name in ("model.onnx", "config.json", "aligned.onnx", "voice.py", "renderer.py"):
            (root / name).write_text("fixture", encoding="utf-8")
        def identity():
            return synthesis_identity(root, profile, root / "aligned.onnx", root, root / "renderer.py")
        with mock.patch.dict(os.environ):
            os.environ.pop("KARAOKE_ESPEAK_VOICE", None)
            baseline = identity()
            os.environ["KARAOKE_ESPEAK_VOICE"] = ""
            self.assertEqual(baseline, identity())
            os.environ["KARAOKE_ESPEAK_VOICE"] = "en-us"
            overridden = identity()
            self.assertNotEqual(baseline, overridden)
            os.environ["KARAOKE_ESPEAK_VOICE"] = "en-gb"
            self.assertNotEqual(overridden, identity())


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

    def measurement(self, number=1):
        from emblem_rsi.pron import measure, trial
        request = json.loads(self.p.p("request").read_text(encoding="utf-8"))
        dm = json.loads(self.p.p("docmodel").read_text(encoding="utf-8"))
        folder = self.p.p("rsi") / "pron" / f"measure-{number:03d}"
        (folder / "asr").mkdir(parents=True)
        reference = folder / "request.ref.json"
        reference.write_text(json.dumps({**request, **trial.reference_request(request, dm)}), encoding="utf-8")
        asr = folder / "asr" / "asr_measure.json"
        asr.write_text(json.dumps({"inputs": {"request_sha256": sha256(reference)},
                                  "segments": {s["id"]: {} for s in request["segments"]}}), encoding="utf-8")
        path = folder.with_suffix(".json")
        path.write_text(json.dumps({"page_sha256": measure.page_sha256(self.p.p("request"), self.p.p("pronunciations")),
                                    "signature_sha256": measure.signature_sha256(self.p.p("speech"),
                                                                                [s["id"] for s in request["segments"]]),
                                    "metrics": {"asr_segments_coverage": 1.0}}), encoding="utf-8")
        return path, asr, reference

    def test_measure_only_audit_is_bound_and_changed_evidence_invalidates(self):
        stage = R.load(self.p).stages["export"]
        before = R.fingerprint(self.p, stage)["all"]
        measurement, asr, reference = self.measurement()
        self.assertNotEqual(before, R.fingerprint(self.p, stage)["all"])
        self.assertIn("export input identity changed", audio.manifest_issues(self.p))
        audio.export(self.p, log=quiet)
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        audit = manifest["pronunciationAudit"]
        self.assertEqual(audit["mode"], "measurement")
        self.assertEqual(audit["approval"], "PENDING")
        self.assertEqual(audit["measurement"]["sha256"], sha256(measurement))
        self.assertEqual(audit["measurement"]["asr"]["sha256"], sha256(asr))
        self.assertEqual(audit["measurement"]["reference"]["sha256"], sha256(reference))
        self.assertIsNone(audit["autofixSummary"])
        for path in (measurement, asr):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                value = json.loads(original)
                value["note"] = "new measurement evidence"
                path.write_text(json.dumps(value), encoding="utf-8")
                self.assertIn("export input identity changed", audio.manifest_issues(self.p))
                path.write_bytes(original)
        self.assertEqual(audio.manifest_issues(self.p), [])

    def test_same_run_audit_refreshes_cached_export_without_revoicing(self):
        reg = R.load(self.p)
        reg.stages["audit"].run = lambda project, ctx: {"measurement": str(self.measurement()[0])}
        reg.stages["audit"].platforms = ("any",)
        state = {}
        for name in ("script", "narrate", "export"):
            R.record(self.p, reg.stages[name], state)
        R.write_state(self.p, state)
        planned = {step["stage"]: step for step in R.plan(self.p, reg, targets=["audit", "export"])}
        self.assertEqual(planned["export"]["action"], "skip")
        before = {path.name: sha256(path) for path in self.p.p("speech").glob("*.wav")}
        with mock.patch.object(nar, "narrate", side_effect=AssertionError("unchanged narration reran")):
            result = R.run(self.p, reg, targets=["audit", "export"], log=quiet)
        self.assertEqual(result["ran"], ["audit", "export"])
        self.assertEqual(before, {path.name: sha256(path) for path in self.p.p("speech").glob("*.wav")})
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["pronunciationAudit"], audio.audit_provenance(self.p))
        self.assertEqual(manifest["pronunciationAudit"]["mode"], "measurement")
        self.assertEqual(audio.manifest_issues(self.p), [])

    def test_stale_newer_measurement_cannot_replace_current_evidence(self):
        current, _, _ = self.measurement()
        newer, _, _ = self.measurement(2)
        value = json.loads(newer.read_text(encoding="utf-8"))
        valid_measurement = dict(value)
        valid_page = value["page_sha256"]
        value["page_sha256"] = "different page"
        newer.write_text(json.dumps(value), encoding="utf-8")
        audio.export(self.p, log=quiet)
        def selected():
            return json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))["pronunciationAudit"]["measurement"]
        self.assertEqual(selected()["sha256"], sha256(current))
        stage = R.load(self.p).stages["export"]
        before = R.fingerprint(self.p, stage)["all"]
        value["page_sha256"] = valid_page
        value["signature_sha256"] = "different audio"
        newer.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(audio.manifest_issues(self.p), [])
        self.assertEqual(before, R.fingerprint(self.p, stage)["all"])
        newer.write_text(json.dumps(valid_measurement), encoding="utf-8")
        self.assertIn("export input identity changed", audio.manifest_issues(self.p))
        audio.export(self.p, log=quiet)
        self.assertEqual(selected()["sha256"], sha256(newer))

    def test_missing_or_mismatched_audit_reference_invalidates(self):
        _, asr, reference = self.measurement()
        audio.export(self.p, log=quiet)
        original = reference.read_bytes()
        reference.write_text('{"segments":[]}', encoding="utf-8")
        evidence = json.loads(asr.read_text(encoding="utf-8"))
        evidence["inputs"]["request_sha256"] = sha256(reference)
        asr.write_text(json.dumps(evidence), encoding="utf-8")
        self.assertIn("export input identity changed", audio.manifest_issues(self.p))
        reference.write_bytes(original)
        asr.unlink()
        self.assertIn("export input identity changed", audio.manifest_issues(self.p))

    def test_autofix_summary_is_labelled_without_current_measurement_claim(self):
        summary = self.p.p("rsi") / "pron" / "autofix-last.json"
        summary.parent.mkdir(parents=True)
        summary.write_text('{"chosen":{}}', encoding="utf-8")
        audio.export(self.p, log=quiet)
        audit = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))["pronunciationAudit"]
        self.assertEqual(audit["mode"], "autofix_summary")
        self.assertIsNone(audit["measurement"])
        self.assertEqual(audit["autofixSummary"]["currentMeasurementVerified"], False)
        self.assertEqual(audit["approval"], "PENDING")


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

    def test_voice_override_invalidates_narration_but_workers_do_not(self):
        p = self.project("Hello.")
        stage = R.load(p).stages["narrate"]
        with mock.patch.dict(os.environ):
            os.environ.pop("KARAOKE_ESPEAK_VOICE", None)
            baseline = R.fingerprint(p, stage)["all"]
            os.environ["KARAOKE_ESPEAK_VOICE"] = ""
            self.assertEqual(baseline, R.fingerprint(p, stage)["all"])
            os.environ["KARAOKE_ESPEAK_VOICE"] = "en-gb"
            changed = R.fingerprint(p, stage)["all"]
            self.assertNotEqual(baseline, changed)
            self.assertEqual(changed, R.fingerprint(p, stage, {"narrate": {"workers": 4}})["all"])

    def changed_source(self, relative):
        target = (Path(R.__file__).parent / relative).resolve()
        read_bytes = Path.read_bytes
        def changed(path):
            original = read_bytes(path)
            return original + b"\nchanged source fixture\n" if path.resolve() == target else original
        return mock.patch.object(Path, "read_bytes", changed)

    def test_pronunciation_helper_changes_invalidate_affected_stages(self):
        p = self.project("Hello.")
        stages = R.load(p).stages
        for helper, names in (("../emblem_rsi/pron/lexicon.py", ("prevoice", "autofix", "audit", "report")),
                              ("../emblem_rsi/pron/_espeak_helper.py", ("narrate",))):
            for name in names:
                with self.subTest(helper=helper, stage=name):
                    before = R.fingerprint(p, stages[name])["all"]
                    with self.changed_source(helper):
                        self.assertNotEqual(before, R.fingerprint(p, stages[name])["all"])

    def test_report_template_changes_only_report_implementation(self):
        p = self.project("Hello.")
        stages = R.load(p).stages
        before = {name: st.impl for name, st in stages.items()}
        with self.changed_source("audit/template/report.html"):
            changed = {name for name, st in stages.items() if st.impl != before[name]}
        self.assertEqual(changed, {"report"})

    def test_runtime_recheck_preserves_execution_controls(self):
        cases = [
            ("changed", {}, False, True, True),
            ("unchanged", {}, False, False, False),
            ("explicit_skip", {"skip": ["consumer"]}, False, True, False),
            ("satisfied", {}, True, True, False),
            ("force_satisfied", {"force": ["producer", "consumer"]}, True, False, True),
            ("force_all_with_skip", {"force": ["all"], "skip": ["consumer"]}, False, True, False),
            ("only_producer", {"only": ["producer"]}, False, True, False),
            ("from_consumer", {"from_stage": "consumer"}, False, False, True),
        ]
        for label, controls, satisfied, change, should_run in cases:
            with self.subTest(case=label):
                p = self.project("Hello.", name=label + ".txt")
                source = p.root / "work" / "shared.txt"
                output = p.root / "out" / "result.txt"
                source.parent.mkdir(parents=True, exist_ok=True)
                output.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("before", encoding="utf-8")
                output.write_text("before", encoding="utf-8")
                calls = []
                def produce(project, ctx):
                    source.write_text("after" if change else "before", encoding="utf-8")
                def consume(project, ctx):
                    calls.append(ctx.opt("marker"))
                    output.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                reg = R.Registry()
                reg.add_stage(R.Stage("producer", run=produce, outputs=("work/shared.txt",)))
                reg.add_stage(R.Stage("consumer", run=consume, inputs=("work/shared.txt",),
                                      outputs=("out/result.txt",), options={"marker": {"default": "default"}},
                                      satisfied=(lambda project: "externally satisfied") if satisfied else None))
                options = {"consumer": {"marker": "preserved"}}
                state = {}
                R.record(p, reg.stages["consumer"], state, options=options)
                R.write_state(p, state)
                kwargs = {"force": ["producer"], **controls}
                result = R.run(p, reg, options=options, log=quiet, **kwargs)
                self.assertEqual("consumer" in result["ran"], should_run)
                self.assertEqual(calls, ["preserved"] if should_run else [])
                if label == "only_producer":
                    self.assertEqual([s["stage"] for s in result["steps"]], ["producer"])

    def test_runtime_recheck_updates_satisfied_hook_and_missing_outputs(self):
        for label in ("satisfaction_removed", "output_deleted"):
            with self.subTest(case=label):
                p = self.project("Hello.", name=label + ".txt")
                flag = p.root / "accepted"
                output = p.root / "result.txt"
                flag.write_text("accepted", encoding="utf-8")
                output.write_text("old", encoding="utf-8")
                def produce(project, ctx):
                    (flag if label == "satisfaction_removed" else output).unlink()
                def consume(project, ctx):
                    output.write_text("new", encoding="utf-8")
                reg = R.Registry()
                reg.add_stage(R.Stage("producer", run=produce))
                reg.add_stage(R.Stage("consumer", run=consume, outputs=(str(output),), inputs=(str(flag),),
                                      satisfied=(lambda project: "accepted" if flag.exists() else None)
                                      if label == "satisfaction_removed" else None))
                state = {}
                R.record(p, reg.stages["consumer"], state)
                R.write_state(p, state)
                result = R.run(p, reg, log=quiet)
                self.assertEqual(result["ran"], ["producer", "consumer"])
                self.assertEqual(output.read_text(encoding="utf-8"), "new")

    def test_runtime_recheck_reports_newly_blocked_stage(self):
        p = self.project("Hello.")
        source = p.root / "source.txt"
        source.write_text("before", encoding="utf-8")
        reg = R.Registry()
        reg.add_stage(R.Stage("producer", run=lambda project, ctx: source.write_text("after", encoding="utf-8")))
        reg.add_stage(R.Stage("consumer", run=lambda project, ctx: self.fail("unsupported stage ran"),
                              inputs=(str(source),), platforms=("unsupported-test-platform",)))
        state = {}
        R.record(p, reg.stages["consumer"], state)
        R.write_state(p, state)
        with mock.patch.dict(os.environ):
            os.environ.pop("SPEECHKIT_DELEGATE", None)
            with self.assertRaisesRegex(R.RegistryError, "cannot run here: consumer"):
                R.run(p, reg, log=quiet)

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
