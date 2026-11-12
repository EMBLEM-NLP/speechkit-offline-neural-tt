# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""The export stage: the finished narration as one audio file per chapter, with word timings and a manifest.

    out/narration/
      <slug>_ch-NN.wav|mp3|m4a    the chapter's narration: segments laid out with natural pauses, one gain for the
                                  whole text (speech at -19 dBFS RMS, peaks at most 0.97), mono 22050 Hz
      <slug>_ch-NN.words.json     every spoken word: display text, start and end in seconds, sentence number
      <slug>_script.md            the text that was narrated (work/book.md)
      manifest.json               title, voice, chapters (title, seconds, words, files) and every file's SHA-256;
                                  status NEURAL_AUDIO_GENERATED_REVIEW_PENDING, as for SpeechKit's own speech.json

The pause rules are those of the original read-along (gap_between): 0.5 s between paragraphs, 0.95 s before a
section heading, 1.4 s before a chapter heading, shorter inside verse, numbered lists, link lists and tables.
"""
from __future__ import annotations

import math
import tempfile
from pathlib import Path
from .locking import locked_project
from .voice.identity import validate_record, digest, inspect_wav

import hashlib  # SHA-256 of every exported file for the manifest
import json  # timings and manifest
import shutil  # finds ffmpeg; copies the script
import subprocess  # runs ffmpeg for mp3 and m4a
import wave  # reads segment WAVs and writes chapter WAVs

from . import chapters as chap  # chapter ranges and sentence numbers

RATE = 22050  # the voice's sample rate; every segment WAV must match it
TARGET_RMS_DB, PEAK_CAP = -19.0, 0.97  # loudness target for the speech and the highest allowed sample
ENCODERS = {"mp3": ["-c:a", "libmp3lame"], "m4a": ["-c:a", "aac", "-movflags", "+faststart"]}


# A token whose phonemes did not match on their own gets a zero-length span sharing the next word's start (see
# voice/karaoke_voice.py, which now avoids this for a sentence-opening "A"); give it the opening slice of that word so
# every word has a visible time. words is one segment's list of {"token", "start", "end"}; changed in place.
# Returns how many pairs were repaired.
def fix_collapsed(words) -> int:
    n = 0
    for k in range(len(words) - 1):  # every adjacent pair (the last word has no successor)
        a, b = words[k], words[k + 1]
        if b["start"] <= a["start"] + 0.04:  # b starts within 40 ms of a: a has no real duration of its own
            # 35% of the span from a's start to b's end (taken as at least 50 ms), never more than 120 ms
            share = min(0.12, 0.35 * max(b["end"] - a["start"], 0.05))
            a["end"] = a["start"] + share
            b["start"] = a["start"] + share  # no overlap, starts keep increasing
            a["estimated"] = b["estimated"] = True
            a["timingRepair"] = b["timingRepair"] = "export collapsed span split"
            n += 1
    return n


# Pause in seconds before seg when it follows prev_seg; blocks is the docmodel block list, both segments are docmodel
# segment dicts ("first_block" names the block each starts in).
def gap_between(blocks, prev_seg, seg) -> float:
    pk, k = blocks[prev_seg["first_block"]]["kind"], blocks[seg["first_block"]]["kind"]
    gap = 0.5  # ordinary pause between two paragraphs or list items
    if k in ("h2", "h3"):
        gap = 0.95  # before a section heading
    if k == "h1":
        gap = 1.4  # before a chapter heading
    if pk == "h1":
        gap = max(gap, 0.8)  # after a chapter heading the text waits too
    if pk == "cover":
        gap = 1.6  # after the title
    if k == "verse" and pk == "verse":
        gap = 0.42  # lines of a poem flow together
    if k in ("num", "link") and pk == k:
        gap = 0.32  # a numbered list or a list of links is read as a quick sequence
    if k in ("tr", "th"):
        gap = 0.55  # each table row is read as its own sentence
    return gap


# One segment WAV as a float32 numpy array in -1..1. Raises ValueError for anything but 22050 Hz mono 16-bit PCM,
# because the chapter mix places segments by sample index.
def read_wav(path):
    import numpy as np
    with wave.open(str(path)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != RATE:
            raise ValueError(f"{path}: need {RATE} Hz mono 16-bit")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768


# SHA-256 hex digest of a file.
def _sha(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# Write 16-bit PCM bytes as fmt ("wav", "mp3" or "m4a") to path. wav needs nothing; the others run ffmpeg.
def _write_audio(pcm: bytes, path, fmt: str, kbps: int) -> None:
    if fmt == "wav":
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        return
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError(f"export.format {fmt} needs ffmpeg on PATH (or use \"wav\")")
    subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1",
                    "-i", "pipe:0", *ENCODERS[fmt], "-b:a", f"{int(kbps)}k", str(path)], input=pcm, check=True)


# The voice record for the manifest: the profile name plus the pinned identity from the profile JSON, so the files
# can be traced to the exact model (the profile's own pins are checked by speechkit's preflight before voicing).
def _voice(name: str) -> dict:
    from . import env
    try:
        prof = env.profile(name)
    except (OSError, ValueError):  # a missing or unreadable profile still leaves the name on record
        return {"profile": name}
    return {"profile": name, **{k: prof.get(k) for k in ("id", "engine", "engineVersion", "model", "modelSha256",
                                                          "lengthScale", "voiceApproval") if k in prof}}


# The export stage. project is the open Project; fmt overrides export.format; log is a print-like callable.
# Reads work/docmodel.json and every segment's work/speech/<id>.wav(.json); writes out/narration (files of an earlier
# export that this one does not write are removed). Returns a summary dict.
def _export_to(project, out: Path, fmt: str | None = None, log=print) -> dict:
    import numpy as np
    cfg = project.cfg["export"]
    fmt = (fmt or cfg.get("format") or "wav").lower()
    if fmt not in ("wav", *ENCODERS):
        raise ValueError(f"unknown export format {fmt!r}: use wav, mp3 or m4a")
    lead, tail = float(cfg.get("lead", 0.6)), float(cfg.get("tail", 1.2))
    if not all(math.isfinite(x) and x >= 0 for x in (lead, tail)):
        raise ValueError("export lead and tail must be finite nonnegative seconds")
    dm = json.loads(project.p("docmodel").read_text(encoding="utf-8"))
    blocks, flat, segs = dm["blocks"], dm["tokens"], dm["segments"]
    speech = project.p("speech")
    from .voice.narrate import validate_outputs
    invalid = validate_outputs(project)
    if invalid:
        raise ValueError("invalid narration: " + "; ".join(invalid))
    recs = {}
    request = json.loads(project.p("request").read_text(encoding="utf-8"))
    requested = {s["id"]: s for s in request["segments"]}
    if [s["id"] for s in segs] != list(requested):
        raise ValueError("docmodel and request segment order differ; run script first")
    for s in segs:
        jp = speech / f"{s['id']}.wav.json"
        if not jp.exists():  # narration has not finished
            raise FileNotFoundError(f"{jp} missing: run narrate first")
        recs[s["id"]] = json.loads(jp.read_text(encoding="utf-8"))
        problems = validate_record(recs[s["id"]], speech / f"{s['id']}.wav", requested[s["id"]])
        if problems:
            raise ValueError(f"{s['id']}: invalid narration: {'; '.join(problems)}")
        if s["spokenText"] != requested[s["id"]]["spokenText"] or s["tokens"] != requested[s["id"]]["tokenIndex"]:
            raise ValueError(f"{s['id']}: stale docmodel; run script first")
    identities = {r["synthesisIdentity"]["sha256"] for r in recs.values()}
    if len(identities) != 1:
        raise ValueError("export requires nonempty narration with one synthesis identity")
    fixed = sum(fix_collapsed(recs[s["id"]]["words"]) for s in segs)
    sent_of, _ = chap.sentences(blocks, flat)

    gain = 1.0  # one gain for the whole text, from the speech RMS of every segment (silence excluded)
    if cfg.get("normalize", True):
        ss, cnt, peak = 0.0, 0, 0.0
        for s in segs:
            a = read_wav(speech / f"{s['id']}.wav")
            nz = a[a != 0].astype(np.float64)  # exact zeros are the inserted sentence pauses
            ss += float(np.sum(nz * nz))
            cnt += nz.size
            peak = max(peak, float(np.max(np.abs(a))) if a.size else 0.0)
        rms = (ss / cnt) ** 0.5 if cnt else 1e-3
        gain = min(10 ** (TARGET_RMS_DB / 20) / rms, PEAK_CAP / peak if peak else 1.0)
        log(f"export: speech rms {20 * np.log10(rms):.2f} dBFS, peak {peak:.3f}; gain x{gain:.3f}; "
            f"collapsed spans fixed {fixed}")

    out.mkdir(parents=True, exist_ok=True)
    stem = project.slug
    if Path(stem).name != stem or stem in (".", "..") or "\\" in stem:
        raise ValueError("export slug must be a plain filename component")
    written, chapters_out = [], []
    for c, ch in enumerate(chap.chapters(blocks, project)):
        b0, b1 = ch["b0"], ch["b1"]
        csegs = [s for s in segs if b0 <= s["first_block"] < b1]
        if not csegs:  # a chapter with nothing spoken (only code blocks, say) gets no audio file
            continue
        bad = [s["id"] for s in csegs if not b0 <= s["last_block"] < b1]
        if bad:  # one segment's audio cannot be split between two files
            raise ValueError(f"chapter {c}: segments {bad} cross a chapter boundary")
        t, offs = lead, []
        for n, s in enumerate(csegs):
            if n:
                t += gap_between(blocks, csegs[n - 1], s)
            offs.append(t)
            t += recs[s["id"]]["durationSeconds"]
        total = int(round((t + tail) * RATE))
        buf = np.zeros(total, dtype=np.float32)
        words = []
        for s, off in zip(csegs, offs):
            a = read_wav(speech / f"{s['id']}.wav")
            i = int(round(off * RATE))
            buf[i:i + len(a)] = a[:total - i]
            for w in recs[s["id"]]["words"]:
                tok = flat[w["token"]]
                words.append({"word": tok["disp"], "start": round(off + w["start"], 3),
                              "end": round(off + w["end"], 3), "sentence": sent_of.get(w["token"]),
                              "estimated": w.get("estimated", False), **({"timingRepair": w["timingRepair"]}
                                                                        if "timingRepair" in w else {})})
        pcm = np.clip(np.round(buf * gain * 32767), -32768, 32767).astype("<i2").tobytes()
        cid = f"ch-{c:02d}"
        af = out / f"{stem}_{cid}.{fmt}"
        _write_audio(pcm, af, fmt, cfg.get("kbps", 96))
        wf = out / f"{stem}_{cid}.words.json"
        wf.write_text(json.dumps(words, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        written += [af, wf]
        title = ch["title"] or project.title  # the first chapter opens with the title (the cover block)
        chapters_out.append({"id": cid, "title": title, "seconds": round(total / RATE, 2), "samples": total, "words": len(words),
                             "segments": len(csegs), "audio": af.name, "audioSha256": _sha(af),
                             "timings": wf.name, "timingsSha256": _sha(wf)})
        log(f"export: {cid} {total / RATE:8.2f}s {len(csegs):4d} segs  {title[:60]}")
    script = out / f"{stem}_script.md"
    shutil.copyfile(project.p("book"), script)
    written.append(script)
    pace = sum(1 for r in recs.values() if r.get("paceReview") == "REQUIRED")
    audit = project.p("rsi") / "pron" / "autofix-last.json"  # summary of the last pronunciation loop, if any
    manifest = {
        "schemaVersion": "1.1.0", "kind": "speechkit-narration", "title": project.title,
        "subtitle": project.subtitle, "author": project.cfg.get("author", ""), "id": project.slug,
        "voice": _voice(project.cfg["voice"]["profile"]),
        "synthesisIdentity": next(iter(recs.values()))["synthesisIdentity"],
        "inputIdentity": input_identity(project),
        "sampleRate": RATE, "format": fmt, "gain": round(gain, 4),
        "seconds": round(sum(c["seconds"] for c in chapters_out), 2),
        "words": sum(c["words"] for c in chapters_out),
        "chapters": chapters_out,
        "script": script.name, "scriptSha256": _sha(script),
        "status": "NEURAL_AUDIO_GENERATED_REVIEW_PENDING", "voiceApproval": "PENDING",
        "paceReviewSegments": pace,
        "alignmentReviewSegments": sum(bool(r.get("alignmentQuality", {}).get("reviewRequired")) for r in recs.values()),
        "pronunciationAudit": json.loads(audit.read_text(encoding="utf-8")) if audit.exists() else None,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"export: {len(chapters_out)} chapter(s), {manifest['seconds'] / 60:.1f} min, {fmt} -> {out}")
    return {"folder": str(out), "chapters": len(chapters_out), "minutes": round(manifest["seconds"] / 60, 1),
            "format": fmt, "words": manifest["words"], "pace_review": pace}


def input_identity(project) -> str:
    """Content identity of the narration, script, settings and final audit."""
    paths = {name: project.p(name) for name in ("book", "request", "docmodel")}
    paths["audit"] = project.p("rsi") / "pron" / "autofix-last.json"
    from . import env
    paths["profile"] = env.profile_path(project.cfg["voice"]["profile"])
    request = json.loads(project.p("request").read_text(encoding="utf-8"))
    for segment in request["segments"]:
        for suffix in (".wav", ".wav.json"):
            name = segment["id"] + suffix
            paths["speech/" + name] = project.p("speech") / name
    return digest({"schemaVersion": 2,
                   "files": {name: _sha(path) if path.is_file() else "missing" for name, path in paths.items()},
                   "settings": {key: project.cfg.get(key) for key in
                                ("title", "subtitle", "author", "slug", "voice", "export", "chapters")}})


def _owned_files(manifest: dict) -> set[str]:
    names = {manifest["script"]}
    for chapter in manifest["chapters"]:
        names.update((chapter["audio"], chapter["timings"]))
    if any(not isinstance(name, str) or Path(name).name != name or name in (".", "..") or "\\" in name
           for name in names):
        raise ValueError("manifest contains an unsafe artifact path")
    return names


def manifest_issues(project, folder=None) -> list[str]:
    """Validate every delivered artifact, not just the manifest's existence."""
    out = Path(folder) if folder is not None else project.p("out_narration")
    try:
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("kind") != "speechkit-narration" or not manifest.get("chapters"):
            return ["invalid narration manifest"]
        _owned_files(manifest)
        errors = []
        if manifest.get("inputIdentity") != input_identity(project):
            errors.append("export input identity changed")
        pairs = [(manifest["script"], manifest["scriptSha256"])]
        for chapter in manifest["chapters"]:
            pairs += [(chapter["audio"], chapter["audioSha256"]),
                      (chapter["timings"], chapter["timingsSha256"])]
        for name, expected in pairs:
            path = out / name
            if not path.is_file() or path.is_symlink() or _sha(path) != expected:
                errors.append(f"invalid export artifact {name}")
        if errors:
            return errors
        for chapter in manifest["chapters"]:
            if manifest["format"] == "wav":
                measured = inspect_wav(out / chapter["audio"])
                if measured["sampleRate"] != manifest["sampleRate"] or measured["samples"] != chapter["samples"]:
                    errors.append(f"invalid export audio shape {chapter['audio']}")
            words = json.loads((out / chapter["timings"]).read_text(encoding="utf-8"))
            if len(words) != chapter["words"]:
                errors.append(f"invalid export word count {chapter['timings']}")
            previous = -1.0
            for word in words:
                a, b = float(word["start"]), float(word["end"])
                if not all(map(math.isfinite, (a, b))) or a < 0 or a < previous or b < a or b > chapter["seconds"] + .011:
                    errors.append(f"invalid export timing bounds {chapter['timings']}")
                    break
                previous = a
        return errors
    except (OSError, EOFError, wave.Error, KeyError, TypeError, ValueError) as e:
        return [f"invalid narration export: {e}"]


@locked_project
def export(project, fmt: str | None = None, log=print) -> dict:
    """Build and verify off to the side, then replace the complete delivery."""
    out = project.p("out_narration")
    out.parent.mkdir(parents=True, exist_ok=True)
    backup = out.with_name("." + out.name + ".previous")
    if not out.exists() and backup.is_dir():
        backup.replace(out)
    with tempfile.TemporaryDirectory(prefix="." + out.name + ".build-", dir=out.parent) as temp:
        staged = Path(temp) / "delivery"
        result = _export_to(project, staged, fmt, log)
        problems = manifest_issues(project, staged)
        if problems:
            raise ValueError("export validation failed: " + "; ".join(problems))
        old_owned = set()
        if (out / "manifest.json").is_file():
            try:
                old_owned = _owned_files(json.loads((out / "manifest.json").read_text(encoding="utf-8")))
            except (OSError, KeyError, TypeError, ValueError):
                pass
        if out.is_dir():
            for old in out.iterdir():
                if old.name == "manifest.json" or old.name in old_owned:
                    continue
                target = staged / old.name
                if target.exists():
                    raise ValueError(f"export would overwrite unrelated file {old}")
                if old.is_dir() and not old.is_symlink():
                    shutil.copytree(old, target, symlinks=True)
                else:
                    shutil.copy2(old, target, follow_symlinks=False)
        if backup.exists():
            shutil.rmtree(backup)
        moved = False
        try:
            if out.exists():
                out.replace(backup)
                moved = True
            staged.replace(out)
        except BaseException:
            if moved and not out.exists():
                backup.replace(out)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        result["folder"] = str(out)
        return result
