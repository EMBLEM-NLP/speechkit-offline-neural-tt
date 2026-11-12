# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Narrate a project: one WAV + word-timing record per segment in work/speech/.

Resumable: finished segments are skipped. When the script changes (edited text, pronunciation fixes),
only segments whose spoken text changed are voiced again:
  - a segment whose spoken text is unchanged keeps its audio even when its token numbers or its segment id moved
    (a word or a paragraph added earlier in the text renumbers everything after it); its record is renumbered;
  - a change that leaves the voice's phonemes identical (e.g. "Gods" -> "God's") keeps its audio and only the
    record's text is updated.
"""
from __future__ import annotations  # annotations stay as text, so `int | None` hints work on older Pythons

import json  # reads the request and the speech records; rewrites records on text-only updates
import os  # os.replace moves reused audio between segment names
import shutil  # clears a half-finished reuse folder
import subprocess  # starts the voice workers and the espeak helper in the runtime venv
import sys  # command-line argument when run as a script
import tempfile  # the folder in which parallel workers mark the segments they take
from concurrent.futures import ThreadPoolExecutor  # one reader per worker, so no worker waits on a full pipe
from pathlib import Path  # Path objects for this module's folder, the request file and the speech folder

from .. import env  # locations of the speechkit checkout, the runtime venv and the aligned voice model
from .identity import synthesis_identity, validate_record, seal_record, sha256, runtime_metadata, espeak_voice_override
from ..locking import locked_project

HERE = Path(__file__).resolve().parent  # folder of this module, where karaoke_voice.py lives
# the espeak phonemizer bridge from the pronunciation toolkit, run inside the runtime venv
HELPER = env.REPO / "emblem_rsi" / "pron" / "_espeak_helper.py"


def project_identity(project) -> dict:
    profile = project.cfg["voice"]["profile"]
    return synthesis_identity(env.RUNTIME, env.profile_path(profile), env.aligned_model(profile), env.speechkit_src())


def cache_inputs(project) -> dict:
    """Read-only planner identity; constructing the aligned graph belongs to execution."""
    profile_path = env.profile_path(project.cfg["voice"]["profile"])
    files = {"profile": profile_path, "runtimeLock": env.speechkit_src().parent / "runtime-lock.json"}
    if profile_path.is_file():
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        for name in ("model", "config"):
            files[name] = env.RUNTIME / profile[name]
    return {"runtime": str(env.RUNTIME), "runtimeMetadata": runtime_metadata(env.RUNTIME),
            "espeakVoiceOverride": espeak_voice_override(), "files": {k: sha256(p) if p.is_file() else "missing"
                                                  for k, p in files.items()}}


def _read_record(path):
    try:
        value = path.read_text(encoding="utf-8")
        return json.loads(value)
    except (OSError, ValueError):
        return None


def _write_record(path, record):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def validate_outputs(project) -> list[str]:
    try:
        request = json.loads(project.p("request").read_text(encoding="utf-8"))
        errors = []
        profile_path = env.profile_path(project.cfg["voice"]["profile"])
        profile_sha = sha256(profile_path) if profile_path.is_file() else None
        for segment in request["segments"]:
            sid = segment["id"]
            record = _read_record(project.p("speech") / f"{sid}.wav.json")
            problems = validate_record(record, project.p("speech") / f"{sid}.wav", segment)
            if not problems and profile_sha and record["synthesisIdentity"]["components"].get("profileSha256") != profile_sha:
                problems.append("voice profile changed")
            if problems:
                errors.append(f"{sid}: {'; '.join(problems)}")
        if not request["segments"]:
            errors.append("narration request has no segments")
        return errors
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [f"invalid narration outputs: {e}"]


# Builds the command-line options for karaoke_voice.py and returns them as a list of strings. `project` gives
# the voice profile name, `script` is the request file (work/request.json) and `out` the speech folder. Asking
# env.aligned_model() for the model path builds the aligned copy of the voice into the cache on first use.
def voice_args(project, script: Path, out: Path) -> list[str]:
    prof = project.cfg["voice"]["profile"]  # profile name from project.json, e.g. "ljspeech-high"
    # runtime folder and the profile JSON's path
    return ["--runtime", str(env.RUNTIME), "--profile", str(env.profile_path(prof)),
            # the duration-exposing model, the request file and the output folder
            "--aligned-model", str(env.aligned_model(prof)), "--script", str(script), "--out", str(out),
            "--speechkit-src", str(env.speechkit_src())]  # where speechkit's voice.py can be found


# Voices a request with `workers` parallel karaoke_voice.py processes. With more than one, the workers share the
# segments while they run: each takes the next free one, longest first (claims.py), so a worker that gets the slow
# segments never leaves the others waiting. A single worker needs no sharing. `script` is the request file, `out` the
# speech folder (created if missing) and `log` is accepted for a uniform signature but not used. Waits for every worker
# and raises RuntimeError with the tail of each failing worker's error output.
def run_workers(project, script: Path, out: Path, workers: int, log=print) -> None:
    """Run karaoke_voice.py over a request file with N workers that share the segments."""
    out.mkdir(parents=True, exist_ok=True)  # make sure the speech folder exists
    py = str(env.runtime_python())  # the runtime venv's python, which has piper and onnxruntime
    args = voice_args(project, script, out)  # options shared by every worker
    errs = []  # error excerpts of failed workers
    # The marker folder is new for every run, so markers of an earlier run cannot hide segments, and it is removed
    # when the run ends, whether the workers succeeded or not.
    with tempfile.TemporaryDirectory(prefix="speechkit-voice-tts-offline-pipeline-claims-") as claims:
        share = ["--claims", claims] if workers > 1 else []  # a lone worker has nobody to share with
        # start all workers at once, each told its number as "k/n" and where to mark the segments it takes
        procs = [subprocess.Popen([py, str(HERE / "karaoke_voice.py"), *args, "--worker", f"{k}/{workers}", *share],
                                  # output captured as UTF-8 text, undecodable bytes replaced
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                  # one worker process for each number from 0 up to workers - 1
                                  errors="replace") for k in range(workers)]
        # Every worker's output is read at the same time. Reading them one after another would leave the others blocked
        # as soon as their output filled the pipe (a long book makes a line per segment), and a blocked worker does no
        # work.
        with ThreadPoolExecutor(len(procs)) as pool:  # one reader per worker
            # waits for each; (stdout, stderr), in worker order
            outputs = list(pool.map(lambda p: p.communicate(), procs))
        for p, (o, e) in zip(procs, outputs, strict=True):  # each worker with what it printed (one output per worker)
            if p.returncode != 0:  # a non-zero exit code means this worker crashed or reported an error
                # keep the last 2000 characters of its error output (or of stdout if stderr is empty)
                errs.append(e[-2000:] or o[-2000:])
    if errs:  # any failure at all
        raise RuntimeError("narration failed:\n" + "\n".join(errs))  # report every failing worker's excerpt together


# Compares a request with the speech records on disk. `request` is the parsed request.json and `speech` the
# speech folder. Returns (changed, recs): the ids of segments that need attention (no record yet, or a record
# whose spoken text or token list differs) and, for those that have an outdated record, the loaded record.
def changed_segments(request: dict, speech: Path, identity=None) -> tuple[list, dict]:
    changed, recs = [], {}  # segment ids needing work, and id -> outdated record
    for s in request["segments"]:  # every segment of the request
        rec = speech / f"{s['id']}.wav.json"  # its timing record, written last when a segment is voiced
        r = _read_record(rec)
        if validate_record(r, speech / f"{s['id']}.wav", identity=identity):
            changed.append(s["id"])  # a segment without a record has never been voiced, so it goes on the list
            continue  # nothing more to check
        # load the timing record written when the segment was last voiced
        # the spoken text changed, or the record covers different docmodel tokens
        if (r["spokenText"] != s["spokenText"] or r.get("id") != s["id"]
                or r.get("text") != s.get("text") or [w["token"] for w in r["words"]] != s["tokenIndex"]):
            changed.append(s["id"])  # the record no longer matches the request: voice it again, or update its text only
            recs[s["id"]] = r  # keep the record so the text-only check can compare old and new text
    return changed, recs  # ids to look at, and the outdated records


# Decides for each (old, new) pair of spoken texts whether the voice would receive identical phonemes, in which
# case the existing audio is still valid. Runs the espeak helper once in the runtime venv for all texts and
# returns one bool per pair. An empty phoneme string is never treated as identical.
def same_phonemes(pairs: list[tuple[str, str]]) -> list[bool]:
    """For (old, new) spoken texts: does the voice receive identical phonemes?"""
    if not pairs:  # no outdated records to compare, so the helper is not started at all
        return []  # no helper run needed
    texts = [t for ab in pairs for t in ab]  # flatten the pairs into one list: old0, new0, old1, new1, ...
    # run the helper isolated (-I) with the texts as JSON on stdin
    r = subprocess.run([str(env.runtime_python()), "-I", str(HELPER)], input=json.dumps(texts, ensure_ascii=False),
                       # capture its JSON output as text; raise if it fails
                       capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")
    ph = json.loads(r.stdout)  # one phoneme string per text, in the same order
    # pair k is identical when its old phonemes are non-empty and equal to its new ones
    return [bool(ph[2 * k]) and ph[2 * k] == ph[2 * k + 1] for k in range(len(pairs))]


# Every record in the speech folder that a changed segment may take its audio from: records of segments that are no
# longer in the request, or whose own segment changed. Returns {(spokenText, token count): [segment ids]} in id order.
def _donors(speech: Path, changed: list, request: dict, identity=None) -> dict:
    keep = {s["id"] for s in request["segments"]} - set(changed)  # unchanged segments keep their own audio
    out = {}
    for rec in sorted(speech.glob("*.wav.json")):
        sid = rec.name[:-len(".wav.json")]
        if sid in keep or not (speech / f"{sid}.wav").exists():
            continue
        r = _read_record(rec)
        if validate_record(r, speech / f"{sid}.wav", identity=identity):
            continue
        out.setdefault((r.get("spokenText"), len(r.get("words") or [])), []).append(sid)
    return out


# Works out what `narrate` would do without voicing anything (apart from running the espeak helper). Returns
# a dict: total segment count, ids to voice, ids that reuse another record's audio (with the donor id), ids that only
# need a text update, the outdated records and the request segments by id. Reads work/request.json and work/speech.
def plan(project) -> dict:
    """What `narrate` would do: segments to voice, audio reused after renumbering, text-only updates."""
    req = json.loads(project.p("request").read_text(encoding="utf-8"))  # the current request (text/docmodel.build)
    speech = project.p("speech")
    identity = project_identity(project)
    changed, recs = changed_segments(req, speech, identity)
    by_id = {s["id"]: s for s in req["segments"]}
    # 1. the same spoken text with the same number of tokens exists in some record: reuse its audio, renumbered
    donors = _donors(speech, changed, req, identity) if changed else {}
    reuse = {}
    for sid in changed:
        pool = donors.get((by_id[sid]["spokenText"], len(by_id[sid]["tokenIndex"])))
        if pool:
            reuse[sid] = sid if sid in pool else pool[0]  # its own record first (only the token numbers moved)
            pool.remove(reuse[sid])
    taken = set(reuse.values())  # a record given away cannot also be updated in place
    # 2. text-only updates: the segment's own outdated record covers as many tokens and gives the same phonemes
    cand = [sid for sid in changed if sid not in reuse and sid in recs and sid not in taken
            and len(recs[sid]["words"]) == len(by_id[sid]["tokenIndex"])]
    same = [sid for sid, ok in zip(cand, same_phonemes([(recs[s]["spokenText"], by_id[s]["spokenText"]) for s in cand]))
            if ok]
    return {"segments": len(req["segments"]), "voice": [s for s in changed if s not in reuse and s not in same],
            "reuse": reuse, "text_only": same, "recs": recs, "by_id": by_id, "identity": identity}


# Rewrite a record for request segment seg: its id, texts, file name and token numbers become the segment's (the audio
# and the timings stay). note is stored under "reused" or "textOnlyUpdate" to say why the audio was kept.
def _renumber(r: dict, seg: dict, key: str, note: dict) -> dict:
    r = dict(r)
    r[key] = note
    r.update(id=seg["id"], text=seg["text"], spokenText=seg["spokenText"], path=f"{seg['id']}.wav")
    r["words"] = [dict(w, token=t) for w, t in zip(r["words"], seg["tokenIndex"])]
    return r


# Narrates `project`: applies text-only updates, deletes outdated audio, voices every missing segment with
# `workers` processes (default project.json voice.workers, 2) and checks the result. `log` is a print-like
# callable. Writes work/speech/sNNNN.wav and sNNNN.wav.json. Returns counts of voiced segments, text-only
# updates and segments flagged for a pace review. Raises RuntimeError when voicing fails or leaves gaps.
@locked_project
def narrate(project, log=print, workers: int | None = None) -> dict:
    pl = plan(project)  # decide what to do
    speech = project.p("speech")  # the speech folder where WAVs and records live
    speech.mkdir(parents=True, exist_ok=True)  # create it if needed
    if pl["reuse"]:  # audio that only moved: first take every donor aside, then put each in its new place
        aside = speech / ".reuse"
        if aside.exists():  # left by a run that was killed half-way: its audio has no record any more
            shutil.rmtree(aside)
        aside.mkdir()
        moved = {}
        for donor in set(pl["reuse"].values()):
            moved[donor] = json.loads((speech / f"{donor}.wav.json").read_text(encoding="utf-8"))
            os.replace(speech / f"{donor}.wav", aside / f"{donor}.wav")
            (speech / f"{donor}.wav.json").unlink()  # without its record the segment counts as not voiced
        for sid, donor in pl["reuse"].items():
            seg = pl["by_id"][sid]
            r = _renumber(moved[donor], seg, "reused", {"from": donor, "reason": "same spoken text; renumbered"})
            (speech / f"{sid}.wav.json").unlink(missing_ok=True)  # an outdated record of this id, if any
            os.replace(aside / f"{donor}.wav", speech / f"{sid}.wav")
            # the record is written after its WAV, as the voice workers do: its presence marks the segment done
            _write_record(speech / f"{sid}.wav.json", seal_record(r, speech / f"{sid}.wav", pl["identity"]))
        aside.rmdir()
    for sid in pl["text_only"]:  # segments whose audio stays valid
        seg = pl["by_id"][sid]
        r = _renumber(pl["recs"][sid], seg, "textOnlyUpdate",
                      {"previousSpokenText": pl["recs"][sid]["spokenText"], "reason": "identical espeak phonemes"})
        _write_record(speech / f"{sid}.wav.json", seal_record(r, speech / f"{sid}.wav", pl["identity"]))
    for sid in pl["voice"]:  # segments that must be voiced again
        for suf in (".wav", ".wav.json"):  # the WAV and its record
            # delete both; a missing record is what tells the worker to voice the segment
            (speech / f"{sid}{suf}").unlink(missing_ok=True)
    # summary of the plan
    log(f"narrate: {pl['segments']} segments; {len(pl['voice'])} to voice, {len(pl['reuse'])} reused after "
        f"renumbering, {len(pl['text_only'])} text-only updates")
    if pl["voice"]:  # there is something to voice
        n = workers or int(project.cfg["voice"].get("workers", 2))  # worker count from the argument or the settings
        # at least one worker, and never more workers than segments
        run_workers(project, project.p("request"), speech, max(1, min(n, len(pl["voice"]))), log)
    # segments that still have no record after the workers finished
    missing = [sid for sid, segment in pl["by_id"].items()
               if validate_record(_read_record(speech / f"{sid}.wav.json"), speech / f"{sid}.wav",
                                  segment, pl["identity"])]
    if missing:  # some were not voiced
        raise RuntimeError(f"segments not voiced: {missing[:10]}")  # name the first ten
    # the paceReview flag of every newly voiced segment ("REQUIRED" when it stayed too fast)
    pace = [_read_record(speech / f"{sid}.wav.json").get("paceReview") for sid in pl["by_id"]]
    # counts for the stage summary
    return {"voiced": len(pl["voice"]), "reused": len(pl["reuse"]), "text_only": len(pl["text_only"]),
            "pace_review": sum(1 for p in pace if p)}


# Reports narration progress for `project` without changing anything: the number of segments in the request,
# how many have a record, and the total voiced length in minutes.
def status(project) -> dict:
    # the current request.json, listing every segment the book needs
    req = json.loads(project.p("request").read_text(encoding="utf-8"))
    speech = project.p("speech")  # folder of the timing records
    done = 0
    secs = 0.0  # running total of narrated audio length, in seconds
    for s in req["segments"]:  # look up each segment of the request in the speech folder
        p = speech / f"{s['id']}.wav.json"  # path of the segment's timing record, which holds its durationSeconds
        record = _read_record(p)
        if not validate_record(record, speech / f"{s['id']}.wav", s):
            # add the length of the segment's WAV, as stored in its timing record
            done += 1
            secs += record["durationSeconds"]
    # counts and minutes rounded to one decimal
    return {"segments": len(req["segments"]), "voiced": done, "minutes": round(secs / 60, 1)}


if __name__ == "__main__":  # lets `python -m speechkit_narrate.voice.narrate <project folder>` narrate a project directly
    from ..project import Project  # imported here because only the command-line use needs it
    print(narrate(Project(sys.argv[1])))  # narrate the project and print the summary
