"""Versioned synthesis identity and independent segment artifact checks.

This module is stdlib-only so the standalone voice worker and the authoring
environment use the same cache contract without importing the neural runtime.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import wave
from pathlib import Path

SCHEMA = "speechkit-segment-v2"


def sha256(path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def espeak_voice_override() -> str | None:
    """Match the worker: an empty override uses the voice from the config."""
    return os.environ.get("KARAOKE_ESPEAK_VOICE") or None


def runtime_metadata(runtime) -> dict:
    metadata = {}
    for base in (Path(runtime) / ".venv" / "lib").glob("python*/site-packages"):
        for pattern in ("piper*.dist-info", "onnxruntime*.dist-info", "numpy*.dist-info"):
            for dist in sorted(base.glob(pattern)):
                for name in ("METADATA", "RECORD"):
                    f = dist / name
                    if f.is_file():
                        metadata[f"{dist.name}/{name}"] = sha256(f)
    return metadata


def synthesis_identity(runtime, profile_path, aligned_model, speechkit_src, renderer_path=None) -> dict:
    """Hash the actual profile, models, renderer and installed runtime metadata."""
    runtime, profile_path, speechkit_src = map(Path, (runtime, profile_path, speechkit_src))
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    renderer = Path(renderer_path) if renderer_path else Path(__file__).with_name("karaoke_voice.py")
    components = {
        "profileSha256": sha256(profile_path),
        "modelSha256": sha256(runtime / profile["model"]),
        "configSha256": sha256(runtime / profile["config"]),
        "alignedModelSha256": sha256(aligned_model),
        "rendererSha256": sha256(renderer),
        "validatorSha256": sha256(__file__),
        "speechkitSourceSha256": sha256(speechkit_src / "voice.py"),
        "runtimeMetadata": runtime_metadata(runtime),
        "espeakVoiceOverride": espeak_voice_override(),
        "engine": profile.get("engine"), "engineVersion": profile.get("engineVersion"),
        "sampleRate": profile.get("sampleRate"),
    }
    lock = speechkit_src.parent / "runtime-lock.json"
    if lock.is_file():
        components["runtimeLockSha256"] = sha256(lock)
    return {"schemaVersion": SCHEMA, "sha256": digest(components), "components": components}


def segment_signature(identity: dict, spoken_text: str) -> str:
    return digest({"schemaVersion": SCHEMA, "synthesis": identity["sha256"], "spokenText": spoken_text})


def inspect_wav(path) -> dict:
    """Decode the whole PCM payload; a valid header alone is not completion."""
    with wave.open(str(path), "rb") as w:
        channels, width, rate, samples = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        if channels != 1 or width != 2 or rate <= 0 or samples <= 0 or w.getcomptype() != "NONE":
            raise ValueError("need nonempty mono PCM16 audio")
        payload = w.readframes(samples)
        if len(payload) != samples * channels * width:
            raise ValueError("truncated WAV payload")
    return {"sha256": sha256(path), "sampleRate": rate, "samples": samples, "durationSeconds": samples / rate}


def validate_record(record, wav_path, segment=None, identity=None) -> list[str]:
    """Return invalidation reasons, never adopt an unverified legacy record."""
    errors = []
    if not isinstance(record, dict):
        return ["record is not an object"]
    actual_identity = record.get("synthesisIdentity")
    if not isinstance(actual_identity, dict) or actual_identity.get("schemaVersion") != SCHEMA:
        errors.append("missing current synthesis identity")
    else:
        try:
            if actual_identity.get("sha256") != digest(actual_identity["components"]):
                errors.append("synthesis identity digest mismatch")
            if record.get("segmentSignature") != segment_signature(actual_identity, record["spokenText"]):
                errors.append("segment signature mismatch")
        except (KeyError, TypeError, ValueError):
            errors.append("malformed synthesis identity")
    if identity is not None and actual_identity != identity:
        errors.append("synthesis identity changed")
    try:
        audio = inspect_wav(wav_path)
        for key in ("sha256", "sampleRate", "samples"):
            if record.get(key) != audio[key]:
                errors.append(f"audio {key} mismatch")
        duration = float(record.get("durationSeconds", float("nan")))
        if not math.isfinite(duration) or abs(duration - audio["durationSeconds"]) > 1 / audio["sampleRate"]:
            errors.append("audio duration mismatch")
        if record.get("path") != Path(wav_path).name:
            errors.append("audio path mismatch")
        previous = -1.0
        words = record["words"]
        if record.get("timingsSha256") != digest(words):
            errors.append("timing record digest mismatch")
        if not isinstance(words, list) or not words:
            raise ValueError("missing timing words")
        for word in words:
            a, b = float(word["start"]), float(word["end"])
            if (not math.isfinite(a) or not math.isfinite(b) or a < 0 or b < a
                    or a < previous or b > audio["durationSeconds"] + 1 / audio["sampleRate"]):
                errors.append("invalid timing bounds")
                break
            previous = a
            if not isinstance(word["token"], int) or isinstance(word["token"], bool) or word["token"] < 0:
                errors.append("invalid timing token")
        if segment is not None:
            if record.get("id") != segment["id"] or record.get("spokenText") != segment["spokenText"]:
                errors.append("request text or identity mismatch")
            if [word["token"] for word in words] != segment["tokenIndex"]:
                errors.append("request token mapping mismatch")
    except (OSError, EOFError, wave.Error, KeyError, TypeError, ValueError, OverflowError) as e:
        errors.append(f"invalid audio or timings: {e}")
    return errors


def seal_record(record: dict, wav_path, identity: dict) -> dict:
    row = dict(record)
    row.update(inspect_wav(wav_path))
    row["path"] = Path(wav_path).name
    row["synthesisIdentity"] = identity
    row["segmentSignature"] = segment_signature(identity, row["spokenText"])
    row["timingsSha256"] = digest(row["words"])
    problems = validate_record(row, wav_path, identity=identity)
    if problems:
        raise ValueError("invalid completed segment: " + "; ".join(problems))
    return row
