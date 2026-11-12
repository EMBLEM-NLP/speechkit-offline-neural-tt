#!/usr/bin/env python3
"""Run an authored cue package through SpeechKit without losing external cue IDs.

The input is a reviewed JSON object with scenes/cues, never the report Markdown.
Only verified native segment audio is exported for the animation compositor.
"""
from __future__ import annotations

import argparse
from array import array
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import wave

REPOSITORY = Path(__file__).resolve().parents[1]
if (REPOSITORY / "speechkit_narrate").is_dir():
    sys.path.insert(0, str(REPOSITORY))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def read_package(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not data.get("scenes"):
        raise ValueError("Need a version 1 authored cue package")
    cues = [cue for scene in data["scenes"] for cue in scene["cues"]]
    ids = [c["id"] for c in cues]
    if len(ids) != len(set(ids)) or not cues or not all(isinstance(i, str) and i.strip() for i in ids):
        raise ValueError("Cue IDs must be unique and nonempty")
    for scene in data["scenes"]:
        for cue in scene["cues"]:
            if cue["scene_id"] != scene["id"] or not cue["text"].strip():
                raise ValueError("Invalid scene identity or empty cue")
            if cue["text"] != " ".join(cue["text"].split()):
                raise ValueError("Authored text must have canonical paragraph whitespace")
    if data.get("cue_count") != len(cues):
        raise ValueError("Declared cue coverage differs from actual content")
    return data


def cue_mapping(package, project):
    """Fail closed on dropped/reordered/added text, including duplicated sentences."""
    project = Path(project)
    dm = json.loads((project / "work/docmodel.json").read_text())
    request = json.loads((project / "work/request.json").read_text())
    cues = [c for s in package["scenes"] for c in s["cues"]]
    blocks, tokens, segments = dm["blocks"], dm["tokens"], dm["segments"]
    if len(blocks) != len(cues) + 1 or blocks[0]["kind"] != "cover":
        raise ValueError("Document has unexpected cover or body blocks")
    if any(tokens[i]["spoken"] for i in blocks[0]["tokens"]):
        raise ValueError("Cover would be spoken; require spoken.read_cover=false")
    if len(segments) != len(cues) or len(request["segments"]) != len(cues):
        raise ValueError("Narration segment count does not cover authored cues exactly")
    rows = []
    for index, (cue, block, segment, req) in enumerate(
            zip(cues, blocks[1:], segments, request["segments"], strict=True), 1):
        if block["kind"] != "p" or block["text"] != cue["text"]:
            raise ValueError(f"Source cue {cue['id']} changed at ordered block {index}")
        if segment["first_block"] != index or segment["last_block"] != index:
            raise ValueError(f"Source cue {cue['id']} merged or split unexpectedly")
        if segment["id"] != req["id"] or segment["tokens"] != req["tokenIndex"]:
            raise ValueError("Request and docmodel disagree")
        if req["text"] != segment["text"] or req["spokenText"] != segment["spokenText"]:
            raise ValueError("Request text and docmodel disagree")
        if " ".join(tokens[i]["disp"] for i in block["tokens"]) != cue["text"]:
            raise ValueError("Display-token spelling differs from authored caption")
        if req["tokens"] != [tokens[i]["spoken"] for i in segment["tokens"]]:
            raise ValueError("Request spoken tokens disagree")
        rows.append({"cue_id": cue["id"], "scene_id": cue["scene_id"], "ordinal": index - 1,
                     "text": cue["text"], "text_sha256": canonical_hash(cue["text"]),
                     "block_index": index, "segment_id": segment["id"],
                     "display_token_ids": block["tokens"], "spoken_token_ids": segment["tokens"],
                     "spoken_text": segment["spokenText"]})
    return {"schema_version": 1, "source_sha256": canonical_hash(package),
            "docmodel_sha256": sha(project / "work/docmodel.json"),
            "request_sha256": sha(project / "work/request.json"), "cue_count": len(rows), "cues": rows}


def verify_native_audio(project, mapping):
    """Verify headers, real PCM duration, source tokens and timing-record agreement."""
    from speechkit_narrate.voice.identity import validate_record
    project = Path(project)
    rows = []
    for cue in mapping["cues"]:
        rel = Path("work/speech") / (cue["segment_id"] + ".wav")
        path = project / rel
        record_path = path.with_suffix(".wav.json")
        record = json.loads(record_path.read_text())
        problems = validate_record(record, path, {"id": cue["segment_id"],
            "spokenText": cue["spoken_text"], "tokenIndex": cue["spoken_token_ids"]})
        if problems:
            raise ValueError(f"Invalid native record {path}: {'; '.join(problems)}")
        with wave.open(str(path), "rb") as stream:
            if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (1, 2, 22050):
                raise ValueError(f"Unsupported native audio header: {path}")
            count = stream.getnframes()
            raw = stream.readframes(count)
        pcm = array("h")
        pcm.frombytes(raw)
        if sys.byteorder != "little":
            pcm.byteswap()
        if len(pcm) != count or not count:
            raise ValueError(f"Truncated or empty PCM: {path}")
        peak = max(abs(s) for s in pcm)
        if peak < 100 or peak >= 32767:
            raise ValueError(f"Silent or clipped PCM: {path} (peak {peak})")
        duration = count / 22050
        if not math.isfinite(record["durationSeconds"]) or abs(record["durationSeconds"] - duration) > 1 / 22050 + 1e-6:
            raise ValueError(f"Audio duration disagrees with record: {path}")
        if record["spokenText"] != cue["spoken_text"]:
            raise ValueError(f"Stale spoken-text record: {path}")
        words = record["words"]
        if [w["token"] for w in words] != cue["spoken_token_ids"]:
            raise ValueError(f"Missing/duplicate/stale token timing: {path}")
        previous = -1.0
        estimated = 0
        for word in words:
            start, end = word["start"], word["end"]
            if not all(math.isfinite(x) for x in [start, end]) or not 0 <= start <= end <= duration + .002:
                raise ValueError(f"Invalid token timing: {path}")
            if start < previous - .002:
                raise ValueError(f"Nonmonotonic token timing: {path}")
            previous = start
            estimated += int(start == end or word.get("estimated", False))
        rows.append({**cue, "audio": str(rel), "record": str(rel) + ".json",
                     "audio_sha256": sha(path), "record_sha256": sha(record_path),
                     "sample_rate": 22050, "sample_count": count, "duration_seconds": duration,
                     "peak_dbfs": 20 * math.log10(peak / 32768),
                     "word_timings": words, "word_timing_basis": "Engine phoneme durations; may contain estimated spans",
                     "flagged_word_spans": estimated, "pace_review": record.get("paceReview"),
                     "alignment_quality": record.get("alignmentQuality"),
                     "synthesis_identity": record.get("synthesisIdentity")})
    return {"status": "native_audio_validated", "voice_approval": "PENDING", "cue_count": len(rows),
            "source_sha256": mapping["source_sha256"], "cues": rows}


def watch_plan(watchlist, project, mapping):
    """Expand displayed technical terms using this run's pre-override spoken forms."""
    from emblem_rsi.pron.asr import normalize_expected
    source = json.loads(Path(watchlist).read_text(encoding="utf-8"))
    terms = source.get("terms")
    if not isinstance(terms, list) or not terms or not all(isinstance(t, str) and t.strip() for t in terms):
        raise ValueError("Pronunciation watchlist needs nonempty string terms")
    dm = json.loads((Path(project) / "work/docmodel.json").read_text())
    tokens = dm["tokens"]
    included = {i for cue in mapping["cues"] for i in cue["spoken_token_ids"]}
    flat = [(word, token["i"]) for token in tokens if token["i"] in included
            for word in normalize_expected(token["disp"])]
    display_words = [w for w, _ in flat]
    watches, details = set(), []
    for term in terms:
        normalized = normalize_expected(term)
        watches.update(normalized)
        matches = set()
        if normalized:
            for index in range(len(flat) - len(normalized) + 1):
                if display_words[index:index + len(normalized)] == normalized:
                    matches.update(i for _, i in flat[index:index + len(normalized)])
        expanded = sorted({word for i in matches for word in normalize_expected(
            tokens[i].get("spoken_ref", tokens[i]["spoken"]))})
        watches.update(expanded)
        details.append({"display_term": term, "normalized_display": normalized,
                        "matched_token_ids": sorted(matches), "expanded_spoken_words": expanded,
                        "present_in_authored_text": bool(matches)})
    return {"source_watchlist_sha256": sha(watchlist), "source_sha256": mapping["source_sha256"],
            "docmodel_sha256": mapping["docmodel_sha256"], "audit_watch": sorted(watches),
            "terms": details, "pronunciation_approval": "PENDING"}


def narration_args(project, audit=False, autofix=False):
    """Measure-only audits must never acquire an implicit autofix prerequisite."""
    args = ["run", str(project)]
    if autofix:
        args += ["autofix", "report", "export", "--options",
                 json.dumps({"autofix": {"takes": 1, "retakes": 0}})]
    elif audit:
        args += ["audit", "export", "--skip", "autofix"]
    else:
        args += ["narrate", "export"]
    return args + ["--accept-draft", "--no-adopt"]


def verify_audit(project, mapping, watches):
    """Require full primary coverage and a second listen for every watched occurrence."""
    from emblem_rsi.pron.asr import normalize_expected
    from emblem_rsi.pron.measure import page_sha256, signature_sha256
    project = Path(project)
    measurements = sorted((project / "work/rsi/pron").glob("measure-*.json"))
    if not measurements:
        raise ValueError("No pronunciation measurement was produced")
    mpath = measurements[-1]
    measurement = json.loads(mpath.read_text())
    asr_path = mpath.with_suffix("") / "asr/asr_measure.json"
    asr = json.loads(asr_path.read_text())
    expected_segments = {c["segment_id"] for c in mapping["cues"]}
    if measurement.get("page_sha256") != page_sha256(project / "work/request.json", project / "work/pronunciations.json"):
        raise ValueError("Pronunciation measurement belongs to a different script")
    if measurement.get("signature_sha256") != signature_sha256(project / "work/speech",
            [c["segment_id"] for c in mapping["cues"]]):
        raise ValueError("Pronunciation measurement belongs to different audio")
    reference_path = mpath.with_suffix("") / "request.ref.json"
    if asr["inputs"].get("request_sha256") != sha(reference_path):
        raise ValueError("Recognizer reference-request hash changed")
    metrics = measurement["metrics"]
    if metrics.get("words_tracked_ratio") != 1.0 or metrics.get("asr_segments_coverage") != 1.0:
        raise ValueError("Pronunciation audit lacks full word/segment primary coverage")
    if asr["inputs"].get("missing_wavs") or set(asr["segments"]) != expected_segments:
        raise ValueError("Pronunciation audit segment identities do not match the authored cues")
    if asr["totals"]["ctc"].get("segments_scored") != len(expected_segments):
        raise ValueError("Primary recognizer did not score every segment")
    actual_watch = set(asr["params"].get("watch", []))
    if not set(watches["audit_watch"]).issubset(actual_watch):
        raise ValueError("Audit omitted requested technical watch words")
    dm = json.loads((project / "work/docmodel.json").read_text())
    tokens = dm["tokens"]
    occurrences = asr["occurrences"]
    wanted = set(watches["audit_watch"])
    expected_tokens = {i for cue in mapping["cues"] for i in cue["spoken_token_ids"]}
    if set(map(int, occurrences)) != expected_tokens:
        raise ValueError("Primary recognizer token coverage differs from the authored cue map")
    watched_ids, confirmed_ids, missing = [], [], []
    for token_id in sorted(expected_tokens):
        words = set(normalize_expected(tokens[token_id].get("spoken_ref", tokens[token_id]["spoken"])))
        if not words.intersection(wanted):
            continue
        watched_ids.append(token_id)
        occurrence = occurrences[str(token_id)] if str(token_id) in occurrences else occurrences[token_id]
        if occurrence.get("confirm", {}).get("status") in ("ok", "sub", "del"):
            confirmed_ids.append(token_id)
        else:
            missing.append(token_id)
    if not watched_ids or missing:
        raise ValueError(f"Watched-word confirmation coverage missing: {missing or 'no present watch words'}")
    report = project / "work/rsi/pron/report/Pronunciation_Audit.html"
    if not report.is_file():
        raise ValueError("Audit HTML report was not produced")
    return {"status": "coverage_complete", "voice_approval": "PENDING", "pronunciation_approval": "PENDING",
            "mode": "measurement", "measurement": str(mpath.relative_to(project)),
            "measurement_sha256": sha(mpath), "asr": str(asr_path.relative_to(project)),
            "asr_sha256": sha(asr_path), "report": str(report.relative_to(project)), "report_sha256": sha(report),
            "source_sha256": mapping["source_sha256"], "docmodel_sha256": mapping["docmodel_sha256"],
            "primary_segments": len(expected_segments), "primary_tokens": len(expected_tokens),
            "watched_tokens": len(watched_ids), "watched_confirmed_tokens": len(confirmed_ids),
            "watch": watches, "recognizers": asr["models"], "metrics": metrics,
            "confirmed_misheard": asr.get("confirmed", []), "near_review": asr.get("near_review", []),
            "note": "Coverage confirms that the recognizers ran; it does not approve the voice or pronunciation."}


def verify_generic_export(project, audit_summary=None):
    """Require the assembled export to contain the current inputs and audit evidence."""
    from speechkit_narrate import audio
    from speechkit_narrate.project import Project
    project = Project(project)
    problems = audio.manifest_issues(project)
    if problems:
        raise ValueError("Generic narration export validation failed: " + "; ".join(problems))
    path = project.p("out_narration") / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    current = audio.audit_provenance(project)
    if manifest.get("pronunciationAudit") != current:
        raise ValueError("Generic narration export pronunciation provenance is stale")
    if manifest.get("voiceApproval") != "PENDING" or current and current.get("approval") != "PENDING":
        raise ValueError("Generated narration must retain PENDING listening approval")
    if audit_summary is not None:
        measurement = (current or {}).get("measurement")
        if not measurement or any((
                measurement.get("path") != audit_summary["measurement"],
                measurement.get("sha256") != audit_summary["measurement_sha256"],
                measurement.get("asr", {}).get("path") != audit_summary["asr"],
                measurement.get("asr", {}).get("sha256") != audit_summary["asr_sha256"],
                measurement.get("metrics") != audit_summary["metrics"])):
            raise ValueError("Generic narration export does not bind the verified pronunciation measurement")
    return {"status": "current_export_validated", "manifest": str(path.relative_to(project.root)),
            "manifest_sha256": sha(path), "voice_approval": "PENDING",
            "pronunciation_approval": (current or {}).get("approval"),
            "measurement": (current or {}).get("measurement")}


def command(args, log_root, name):
    started = time.time()
    process = subprocess.run([sys.executable, "-m", "speechkit_narrate", "--json", *args],
                             capture_output=True, text=True)
    (log_root / (name + ".stdout.json")).write_text(process.stdout)
    (log_root / (name + ".stderr.log")).write_text(process.stderr)
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"{name}: no structured result; exit {process.returncode}") from None
    if process.returncode or result.get("ok") is not True or result.get("stopped") or any(
            s.get("action") in ("failed", "blocked") for s in result.get("steps", [])):
        raise RuntimeError(f"{name} failed: {result}")
    return {"command": args, "returncode": process.returncode, "seconds": time.time() - started, "result": result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-package", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--profile", default="ljspeech-high")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--stage", choices=["script", "narrate", "validate"], default="narrate")
    audit_mode = parser.add_mutually_exclusive_group()
    audit_mode.add_argument("--audit", action="store_true", help="Measure pronunciation without proposing fixes")
    audit_mode.add_argument("--autofix", action="store_true", help="Optional bounded takes=1, retakes=0 repair")
    parser.add_argument("--watchlist", type=Path, help="Defaults to pronunciation_watchlist.json beside the input")
    args = parser.parse_args(argv)
    package = read_package(args.input_package)
    out = args.output_root.resolve()
    previous = json.loads((out / "run_status.json").read_text()) if (out / "run_status.json").is_file() else {}
    if args.stage == "validate" and not (args.audit or args.autofix):
        args.audit = previous.get("audit_mode") == "measure_only"
        args.autofix = previous.get("audit_mode") == "autofix_bounded"
    watchlist = args.watchlist or args.input_package.parent / "pronunciation_watchlist.json"
    if (args.audit or args.autofix) and not watchlist.is_file():
        raise ValueError(f"Audit watchlist missing: {watchlist}")
    out.mkdir(parents=True, exist_ok=True)
    project = out / "project"
    log_root = out / "logs"
    log_root.mkdir(exist_ok=True)
    state = {"status": "running", "voice_approval": "PENDING", "stages": [], "source_sha256": canonical_hash(package),
             "audit_mode": "autofix_bounded" if args.autofix else "measure_only" if args.audit else "not_requested"}
    if args.stage == "validate":
        state["stages"] = previous.get("stages", [])
        state["validation_runs"] = previous.get("validation_runs", 0) + 1
    write_json(out / "run_status.json", state)
    try:
        if not (project / "project.json").exists():
            source = out / "source_cues.json"
            write_json(source, {"schemaVersion": "1.0.0", "segments": [
                {"id": c["id"], "text": c["text"]} for s in package["scenes"] for c in s["cues"]]})
            settings = {"voice": {"profile": args.profile, "workers": args.workers},
                        "spoken": {"read_cover": False, "code_names": True,
                                   "say": [[r"(?<![A-Za-z])JSON(?![A-Za-z])", "Jason"]]},
                        "ingest": {"autofix": False}, "prevoice": {"enabled": False},
                        "export": {"format": "wav", "lead": 0, "tail": 0}}
            state["stages"].append(command(["new", str(project), "--input", str(source),
                "--title", package["title"], "--set", json.dumps(settings)], log_root, "create"))
            write_json(out / "adapter_source.json", package)
        else:
            if json.loads((out / "adapter_source.json").read_text()) != package:
                raise ValueError("Source package changed; use a new versioned output root")
            cfg = json.loads((project / "project.json").read_text())
            if cfg["voice"]["profile"] != args.profile:
                raise ValueError("Voice changed; use a new versioned output root")
        if args.stage != "validate":
            state["stages"].append(command(["run", str(project), "script", "--accept-draft", "--no-adopt"], log_root, "script"))
        mapping = cue_mapping(package, project)
        write_json(out / "cue_mapping.json", mapping)
        watches = watch_plan(watchlist, project, mapping) if watchlist.is_file() else None
        if watches:
            write_json(out / "pronunciation_watch.json", watches)
        if args.stage == "script":
            state.update(status="script_validated", cue_count=mapping["cue_count"])
        else:
            if args.stage == "narrate":
                if args.audit or args.autofix:
                    audit_settings = {"watch": watches["audit_watch"]}
                    if args.autofix:
                        audit_settings.update(max_fix_words=3, max_occurrences_per_word=2)
                    state["stages"].append(command(["settings", str(project), "--set", json.dumps({
                        "audit": audit_settings})], log_root, "watch_settings"))
                state["stages"].append(command(narration_args(project, args.audit, args.autofix), log_root, "narrate"))
                if args.audit:
                    state["stages"].append(command(["run", str(project), "report", "--skip", "autofix",
                        "--no-adopt"], log_root, "audit_report"))
                mapping = cue_mapping(package, project)
                write_json(out / "cue_mapping.json", mapping)
            audio = verify_native_audio(project, mapping)
            write_json(out / "raw_audio_manifest.json", audio)
            state.update(status="native_audio_validated", cue_count=audio["cue_count"],
                         human_listening_review="PENDING", alignment_review="PENDING")
            summary = None
            if args.audit or args.autofix:
                watches = watch_plan(watchlist, project, mapping)
                summary = verify_audit(project, mapping, watches)
                summary["mode"] = state["audit_mode"]
                write_json(out / "pronunciation_audit_summary.json", summary)
                state["audit_summary_sha256"] = sha(out / "pronunciation_audit_summary.json")
                state["audit_coverage"] = summary["status"]
            exported = verify_generic_export(project, summary)
            write_json(out / "generic_export_validation.json", exported)
            state["generic_export_validation_sha256"] = sha(out / "generic_export_validation.json")
            state["generic_export_status"] = exported["status"]
        write_json(out / "run_status.json", state)
        print(json.dumps({"status": state["status"], "cue_count": state["cue_count"], "root": str(out)}))
        return 0
    except Exception as error:
        state.update(status="failed", error=f"{type(error).__name__}: {error}")
        write_json(out / "run_status.json", state)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
