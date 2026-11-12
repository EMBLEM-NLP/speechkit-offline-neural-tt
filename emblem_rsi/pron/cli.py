# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""`emblem-rsi pron ...`: the audit loop applied to a narration's pronunciation.

    pron seal    --dir RUN                        seal the pronunciation rubric (before anything is measured)
    pron measure --dir RUN [--note TEXT]          hear the whole narration back; write measure-NNN.json
    pron decide  --dir RUN [--change TEXT]        promote or veto the latest measurement (rules in loop.decide)
    pron trial   --plan PLAN --out DIR            voice candidate fixes in their real sentences and rank them
    pron apply   --dir RUN --overrides FILE       snapshot, write the overrides, rebuild the script, re-voice changed segments
    pron retake  --dir RUN --segments IDS --words W [--takes 3]
                                                  voice segments again several times, keep the take both listeners hear best
    pron revert  --dir RUN                        undo the last apply or retake (overrides, script and audio)
    pron status  --dir RUN                        rubric and loop history
"""
from __future__ import annotations  # postponed annotations, so `str | None` hints work on every Python 3 in use

import json  # reads and writes requests, records, overrides and results
import shutil  # copies files into snapshots and back again on revert
import subprocess  # runs the rebuild, re-voice and voice commands of the narration project
import sys  # standard error stream for one-line failure messages
from pathlib import Path  # file paths that work on Windows and Linux

from .. import loop  # the generic loop: sealing, decide rules, log and LoopError
from . import measure as pm  # the pronunciation measurement, imported under the short name pm
from . import paths, trial  # narration locations and commands, and the trial (candidate testing) module

KARAOKE = pm.KARAOKE  # the narration project root, as resolved by paths.py


# List the segments whose audio no longer matches the script and must be voiced again.
# Parameters: request is the freshly rebuilt request dict; speech_dir holds <id>.wav.json timing records.
# Returns segment ids (in request order) that have no record, different spoken text, or a different token list.
def changed_segments(request: dict, speech_dir: Path) -> list:
    out = []  # segment ids that need fresh audio, in request order
    for s in request["segments"]:  # check every segment of the newly rebuilt script
        rec = speech_dir / f"{s['id']}.wav.json"  # the record written when this segment was last voiced
        if not rec.exists():  # no timing record means the segment was never voiced
            out.append(s["id"])  # queue it for voicing because no audio exists yet
            continue  # move on to the next segment of the script
        r = json.loads(rec.read_text(encoding="utf-8"))  # what was voiced last time
        # the spoken text or the token ids differ from what is now in the script
        if r["spokenText"] != s["spokenText"] or [w["token"] for w in r["words"]] != s["tokenIndex"]:
            out.append(s["id"])  # the existing audio no longer matches the script, so queue it
    return out  # segment ids whose audio must be regenerated


# Create the next numbered snapshot folder and copy the narration's script files into it.
# Parameters: run_dir is the run folder; work is the project's work/ folder. Returns the new snapshot path
# (RUN/snapshots/NNN, with an empty speech/ subfolder that callers fill with the audio they replace).
def _new_snapshot(run_dir: Path, work: Path) -> Path:
    snaps = run_dir / "snapshots"  # parent folder holding every numbered snapshot of this run
    # next number from the existing count; reverted ones (NNN-reverted) still count, so numbers never repeat
    n = len(list(snaps.glob("[0-9]*"))) + 1 if snaps.exists() else 1
    snap = snaps / f"{n:03d}"  # zero-padded folder name such as 003
    # create it with a speech subfolder (fails if it already exists, so nothing is overwritten)
    (snap / "speech").mkdir(parents=True)
    # the three files a pronunciation change can alter
    for f in ("pronunciations.json", "request.json", "docmodel.json"):
        if (work / f).exists():  # overrides may not exist yet on the first apply
            shutil.copy2(work / f, snap / f)  # copy the file with its timestamps preserved for later comparison
    return snap  # snapshot folder path, where revert will look for backups


# Apply a set of pronunciation overrides: snapshot, write pronunciations.json, rebuild, re-voice changed segments.
# Parameters: run_dir is the run folder; overrides is the override dict (bare word table or full file shape);
# karaoke is the project root; log reports progress. Returns {"snapshot", "changed"}. Side effects: overwrites
# work/pronunciations.json, runs the rebuild and re-voice commands, and replaces WAVs of changed segments.
def apply(run_dir: Path, overrides: dict, karaoke: Path = KARAOKE, log=print) -> dict:
    """Write overrides, rebuild the script and re-voice only the segments whose spoken text changed.

    Everything replaced is first copied to RUN/snapshots/NNN so `revert` can put it back after a veto.
    """
    work, speech = karaoke / "work", karaoke / "work" / "speech"  # the project's work folder and its speech folder
    snap = _new_snapshot(run_dir, work)  # snapshot the script files before anything changes
    ov_path = work / "pronunciations.json"  # the overrides file the rebuild command reads
    # a bare {word: respelling} table is wrapped into the file's expected shape
    if "overrides" not in overrides and "tokens" not in overrides:
        overrides = {"overrides": overrides}  # wrap the bare table under the "overrides" key the rebuild expects
    # write the overrides as readable UTF-8 JSON
    ov_path.write_text(json.dumps(overrides, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    # rebuild request.json and docmodel.json in the project folder; check=True stops on failure
    subprocess.run(paths.need(paths.REBUILD_CMD, "EMBLEM_REBUILD_CMD", "the script rebuild command"), cwd=karaoke, check=True)
    # load the rebuilt script to compare it with existing audio
    req = json.loads((work / "request.json").read_text(encoding="utf-8"))
    changed = changed_segments(req, speech)  # which segments now differ from their audio
    for sid in changed:  # back up their current audio before it is replaced
        for suf in (".wav", ".wav.json"):  # back up both the WAV audio and its JSON timing record
            if (speech / f"{sid}{suf}").exists():  # a segment that was never voiced has nothing to back up
                # copy into the snapshot's speech folder
                shutil.copy2(speech / f"{sid}{suf}", snap / "speech" / f"{sid}{suf}")
    # record what changed and which overrides were applied, for later reading
    (snap / "changed.json").write_text(json.dumps({"segments": changed, "overrides": overrides}, indent=1,
                                                   # written beside the snapshot's copies
                                                   ensure_ascii=False), encoding="utf-8")
    # progress message naming the snapshot and the re-voice count
    log(f"snapshot {snap}; {len(changed)} segment(s) to re-voice")
    if changed:  # re-voicing only runs when something changed
        # run the project's re-voice command and capture its output as UTF-8 text
        r = subprocess.run(paths.need(paths.REVOICE_CMD, "EMBLEM_REVOICE_CMD", "the re-voice command"), cwd=karaoke, capture_output=True, text=True, encoding="utf-8", errors="replace")
        log(r.stdout.strip()[-800:])  # show the last 800 characters of the re-voice output
        if r.returncode != 0:  # a failed re-voice leaves the narration half-updated, so stop with a clear error
            # include the last 1500 characters of its error output
            raise loop.LoopError("re-voicing failed:\n" + (r.stderr or r.stdout)[-1500:])
    return {"snapshot": str(snap), "changed": changed}  # snapshot location and the list of segments that were re-voiced


# Re-voice chosen segments several times and keep the take that both recognizers hear best.
# Parameters: run_dir (run folder), segments (ids), words (words that must be heard right), takes (how many
# new takes), karaoke (project root), models_dir (ASR models), log. Returns {segment id: chosen take record}.
# Side effects: voices into RUN/retakes/NNN, transcribes every take, and copies the winners into work/speech/
# after snapshotting the files they replace.
def retake(run_dir: Path, segments: list, words: list, takes: int = 3, karaoke: Path = KARAOKE,
           # project root, recognizer models folder, and the logging function
           models_dir=pm.asr.DEFAULT_MODELS, log=print) -> dict:
    """Voice each segment `takes` more times and keep, per segment, the take (the current one included) in
    which both recognizers hear the most occurrences of `words` correctly, then the fewest other errors.
    The voice is stochastic, so a word that is phonemically right but came out unclear in one take is
    usually clear in another. Replaced files are snapshotted for `revert`."""
    # Use an absolute path, because the voice command runs inside the project folder.
    run_dir = Path(run_dir).resolve()        # karaoke_voice runs in the karaoke folder
    work, speech = karaoke / "work", karaoke / "work" / "speech"  # the project's work and speech folders
    # current narration script with segments, tokens and ids
    req = json.loads((work / "request.json").read_text(encoding="utf-8"))
    # document model, needed for each token's reference spelling
    dm = json.loads((work / "docmodel.json").read_text(encoding="utf-8"))
    # the request with overridden tokens swapped back to what a listener should hear
    ref = trial.reference_request(req, dm)
    keep = set(segments)  # requested segment ids as a set for quick membership tests
    # a request holding only those segments, for the voice command
    sub = {**req, "segments": [s for s in req["segments"] if s["id"] in keep]}
    ref_by = {s["id"]: s for s in ref["segments"] if s["id"] in keep}  # reference tokens of those segments, keyed by id
    rdirs = run_dir / "retakes"  # parent folder holding every numbered retake round of this run
    rdir = rdirs / f"{len(list(rdirs.glob('[0-9]*'))) + 1 if rdirs.exists() else 1:03d}"  # next numbered retake folder
    (rdir / "cands").mkdir(parents=True)  # create it with a cands folder for the candidate links
    # the reduced request the voice command will read
    (rdir / "request.json").write_text(json.dumps(sub, ensure_ascii=False), encoding="utf-8")
    cands = {sid: [("current", speech)] for sid in keep}  # every segment starts with its current audio as a candidate
    for k in range(takes):  # one synthesis round per requested take
        out = rdir / f"take{k}"  # output folder receiving this take's WAVs and timing records
        n = 2 if len(keep) > 1 else 1  # split the voicing across two worker processes when several segments are retaken
        # start each worker with "{worker}" set to "index/count", e.g. "0/2"
        procs = [subprocess.Popen(paths.voice_cmd(rdir / "request.json", out, f"{w}/{n}"), cwd=karaoke,
                                  # merge stderr into stdout, decoded as UTF-8
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace") for w in range(n)]
        for p in procs:  # wait for every worker process of this round
            o, _ = p.communicate()  # collect its output and exit status
            if p.returncode != 0:  # a failed worker stops the retake
                # include the last 1500 characters of the worker's output
                raise loop.LoopError("retake synthesis failed:\n" + o[-1500:])
        for sid in keep:  # every segment gained a candidate from this take
            cands[sid].append((f"take{k}", out))  # remember the take's label and folder
        log(f"retake: take {k + 1}/{takes} voiced")  # progress message after each finished take round
    wavs, rows = [], []  # WAV paths to transcribe and (segment, label, folder) rows in the same order
    for sid, cs in cands.items():  # each segment together with its candidate take folders
        for label, d in cs:  # each take including the current audio
            link = rdir / "cands" / f"{sid}_{label}.wav"  # link name segment_label.wav, so its transcript id is unique
            if not link.exists():  # reuse a link left by an earlier interrupted retake
                # symlink to the real WAV instead of copying it (on Windows this needs symlink rights)
                link.symlink_to((d / f"{sid}.wav").resolve())
            wavs.append(link)  # queue this candidate's WAV for transcription
            rows.append((sid, label, d))  # remember which segment, take label and folder it came from
    # transcribe every candidate with both models, sharing the run's transcript cache
    tr = {m: pm.asr.transcribe(wavs, m, cache_dir=run_dir / "asr-cache", models_dir=models_dir) for m in ("ctc", "confirm")}
    # give each candidate its own token id range, since the same tokens appear in every take
    off = {(sid, label): (i + 1) * 10_000_000 for i, (sid, label, _) in enumerate(rows)}
    # a scoring request with one pseudo-segment per candidate, holding reference tokens and offset ids
    sreq = {"segments": [{"id": f"{sid}_{label}", "tokens": ref_by[sid]["tokens"],
                          # token ids shifted by this candidate's offset so they never collide
                          "tokenIndex": [off[(sid, label)] + t for t in ref_by[sid]["tokenIndex"]]} for sid, label, _ in rows]}
    sc = {m: pm.asr.score(sreq, tr[m]) for m in tr}  # score each model's transcripts against the reference
    target = {w.lower() for w in words}  # target words compared in lowercase
    result = {}  # per segment, one result row per candidate
    for sid, label, d in rows:  # score every candidate row against the target words
        o = off[(sid, label)]  # its id offset (computed but never used below)
        hit = err = 0  # target words heard right, and errors on the other words
        for m in sc:  # both recognizers contribute to the candidate's counts
            for tid, occ in sc[m]["occurrences"].items():  # every scored token occurrence reported by that recognizer
                if occ["seg"] != f"{sid}_{label}":  # ignore token occurrences belonging to other candidates
                    continue  # skip occurrences from other pseudo-segments
                if set(occ["expected"].split()) & target:  # a token containing a target word
                    hit += occ["status"] == "ok"  # count a target-word hit when the recognizer heard it right
                elif occ["status"] != "ok":  # any non-target word the recognizer heard wrong
                    err += 1  # counts as collateral damage against this candidate take
        # record the candidate's hits, errors and source folder
        result.setdefault(sid, []).append({"take": label, "target_ok": hit, "other_errors": err, "dir": str(d)})
    snap = _new_snapshot(run_dir, work)  # snapshot before replacing any audio, so revert can undo the retake
    chosen = {}  # the winning take per segment
    for sid, opts in result.items():  # choose a winner separately for each segment
        # most target words right, then fewest other errors, and on a tie the current audio
        best = max(opts, key=lambda r: (r["target_ok"], -r["other_errors"], r["take"] == "current"))
        chosen[sid] = best  # remember the chosen candidate for the result report
        if best["take"] != "current":  # only replace audio when a new take won
            for suf in (".wav", ".wav.json"):  # both the WAV audio and its JSON timing record
                # back up the current narration file into the snapshot
                shutil.copy2(speech / f"{sid}{suf}", snap / "speech" / f"{sid}{suf}")
                # install the winning take's file into the narration
                shutil.copy2(Path(best["dir"]) / f"{sid}{suf}", speech / f"{sid}{suf}")
        # per-segment report: each take's hits and errors
        log(f"retake {sid}: " + ", ".join(f"{r['take']} {r['target_ok']}/{r['other_errors']}" for r in opts)
            + f"  -> keep {best['take']}")  # and finally the take that was kept for this segment
    # full result for later reading
    (rdir / "result.json").write_text(json.dumps({"words": words, "takes": takes, "result": result,
                                                  # chosen takes and snapshot path, written as indented JSON
                                                  "chosen": chosen, "snapshot": str(snap)}, indent=1), encoding="utf-8")
    return chosen  # the chosen take per segment


# Undo the most recent apply or retake by copying the newest snapshot's files back into the project.
# Parameters: run_dir (run folder), karaoke (project root), log. Returns the snapshot path that was restored.
# Side effects: overwrites pronunciations.json, request.json, docmodel.json and the backed-up WAVs, then renames
# the snapshot to NNN-reverted, which later reverts skip.
def revert(run_dir: Path, karaoke: Path = KARAOKE, log=print) -> str:
    # numbered snapshot folders not yet restored, in name order: a name of digits only, so the NNN-reverted folders
    # an earlier revert renamed are skipped and a second revert walks one apply further back
    snaps = sorted(p for p in (run_dir / "snapshots").glob("[0-9]*") if p.is_dir() and p.name.isdigit())
    if not snaps:  # no unreverted snapshot exists, so nothing applied in this run is left to undo
        raise loop.LoopError("nothing to revert")  # report that instead of failing obscurely
    snap = snaps[-1]  # the newest snapshot, by zero-padded folder name
    work, speech = karaoke / "work", karaoke / "work" / "speech"  # work/ and work/speech/ of the project being restored
    if (snap / "pronunciations.json").exists():  # the overrides existed before the change
        # restore the overrides file exactly as it was before the change
        shutil.copy2(snap / "pronunciations.json", work / "pronunciations.json")
    else:  # the snapshot holds no overrides, meaning none existed beforehand
        (work / "pronunciations.json").unlink(missing_ok=True)  # so remove the file the change created
    for f in ("request.json", "docmodel.json"):  # restore the script and the document model
        shutil.copy2(snap / f, work / f)  # copy each back (the snapshot must hold both)
    for f in (snap / "speech").iterdir():  # every audio and timing file backed up in the snapshot
        shutil.copy2(f, speech / f.name)  # overwrite the changed WAV and timing record
    snap.rename(snap.with_name(snap.name + "-reverted"))  # rename the snapshot so its folder name shows it was used
    log(f"reverted to the state before {snap.name}")  # confirm which snapshot's changes were undone
    return str(snap)  # path of the restored snapshot, for the caller's report


# Judge one pronunciation measurement, log the decision with a note of what changed, and print the result.
# Parameters: run_dir (run folder), mpath (measurement JSON), change (text, or None to use the measurement's note).
# Returns 0 for promote and 1 for veto. Side effect: appends the entry to RUN/log.jsonl.
def _decide(run_dir: Path, mpath: Path, change: str | None) -> int:
    sealed = loop.load_sealed(run_dir)  # verify the sealed rubric before judging anything
    m = json.loads(mpath.read_text(encoding="utf-8"))  # load the measurement JSON produced by `pron measure`
    entry = loop.decide(sealed, m, loop.read_log(run_dir))  # apply the loop's fixed rules against the full history
    entry["change"] = change or m.get("note")  # record what changed, from --change or from the measurement's --note
    entry["measurement"] = mpath.name  # and which measurement file was judged
    loop.append_log(run_dir, entry)  # append the decision record to the run's history log
    # headline: loop number, decision, score
    print(f"loop {entry['loop']}: {entry['decision'].upper()}  score {entry['score']}  "
          f"gates {'ok' if entry['gates_ok'] else 'FAILED'}  ({entry['change']})")  # gate state and the change note
    for name, row in entry["rows"].items():  # print one line per rubric metric
        # pass mark, name padded to 26 characters, value and requirement
        print(f"  {'ok  ' if row['pass'] else 'FAIL'} {name:<26} {str(row['value']):<10} need {row['need']}")
    for r in entry["reasons"]:  # then list every veto reason recorded by the rules
        print(f"  veto reason: {r}")  # one indented veto reason per printed line
    return 0 if entry["decision"] == "promote" else 1  # exit status 0 when promoted and 1 when vetoed


# Dispatcher for every `pron` subcommand: runs the chosen action and turns expected failures into status 2.
# Parameter a is the argparse namespace (a.pron_cmd names the action). Returns the exit status.
def main_cmd(a) -> int:
    d = Path(a.dir) if getattr(a, "dir", None) else None  # the run directory, for the subcommands that have --dir
    try:  # catch the expected failures once for all actions
        if a.pron_cmd == "seal":  # seal the pronunciation rubric into the run directory
            # the given rubric or the bundled pronunciation.json
            s = loop.seal(Path(a.rubric) if a.rubric else pm.RUBRIC, d)
            # name, hash prefix and sealing time
            print(f"sealed rubric {s['rubric']['name']!r}  sha256 {s['sha256'][:16]}...  at {s['sealed_at']}")
            return 0  # exit status 0 after a successful seal
        if a.pron_cmd == "measure":  # hear the narration back and write a measurement
            # paths from the options, and the optional comma-separated segment subset
            pm.measure(d, a.request, a.docmodel, a.speech_dir, a.overrides, segments=a.segments.split(",") if a.segments else None,
                       # the lexicon probe can be turned off; the note is stored in the file
                       use_lexicon=not a.no_lexicon, note=a.note)
            return 0  # measurement written; the metrics were already logged
        if a.pron_cmd == "decide":  # promote or veto a measurement under the sealed rules
            # an explicit file, or the newest measure-NNN.json
            mpath = Path(a.measurement) if a.measurement else sorted(d.glob("measure-*.json"))[-1]
            return _decide(d, mpath, a.change)  # judge the measurement, append to the log, and report
        if a.pron_cmd == "trial":  # test candidate spellings in their real sentences
            # run the trial plan with the requested number of takes
            res = trial.run(json.loads(Path(a.plan).read_text(encoding="utf-8")), Path(a.out), a.takes)
            print(trial.table(res["summary"]))  # print the per-word ranking table of candidate spellings
            return 0  # the trial never changes the narration, so it always ends with 0
        if a.pron_cmd == "apply":  # write overrides and re-voice what changed
            # the overrides JSON given with --overrides
            apply(d, json.loads(Path(a.overrides_file).read_text(encoding="utf-8")))
            return 0  # overrides applied and changed segments re-voiced
        if a.pron_cmd == "retake":  # re-voice segments and keep the clearest take
            # comma-separated segment ids and target words
            retake(d, a.segments.split(","), a.words.split(","), a.takes)
            return 0  # retake finished and winners installed
        if a.pron_cmd == "revert":  # undo the latest apply or retake
            revert(d)  # restore the files saved in the newest snapshot
            return 0  # exit status 0 once the revert has completed
        if a.pron_cmd == "status":  # show the sealed rubric and the full decision history
            s = loop.load_sealed(d)  # verify the seal and load the sealed rubric
            # rubric name, sealing time and hash prefix
            print(f"rubric {s['rubric'].get('name', '(unnamed)')!r} sealed {s['sealed_at']} sha256 {s['sha256'][:16]}...")
            for h in loop.read_log(d):  # each logged loop decision, oldest first
                # loop number, decision, score and the change note
                print(f"  loop {h['loop']:>2}  {h['decision']:<7} score {h['score']}  {h.get('change') or ''}"
                      # veto reasons in brackets when present
                      + (f"  ({'; '.join(h['reasons'])})" if h["reasons"] else ""))
            return 0  # status printed without changing anything
    # rule broken, missing file, no measurement yet, bad JSON or a failed project command
    except (loop.LoopError, OSError, IndexError, json.JSONDecodeError, subprocess.CalledProcessError) as e:
        print(f"emblem-rsi: {e}", file=sys.stderr)  # one prefixed error line on the standard error stream
        return 2  # exit status 2 signals an expected, explained failure
    return 2  # an unknown action (argparse normally prevents this)


# Add the `pron` command group to the main emblem-rsi parser.
# Parameter sub is the subparsers object from cli.main. Returns nothing; every action shares the main_cmd handler.
def register(sub) -> None:
    # the `pron` command group with its help summary
    p = sub.add_parser("pron", help="pronunciation audit loop for a narration (seal, measure, trial, apply, decide)")
    ps = p.add_subparsers(dest="pron_cmd", required=True)  # pron's own subcommands; choosing one of them is mandatory

    # Give a subparser the required --dir option and return it, so calls can be chained.
    def with_dir(x):
        # the run directory with the seal, measurements, log and snapshots
        x.add_argument("--dir", required=True, help="run directory (sealed rubric, measurements, log, snapshots)")
        return x  # return the same parser so calls can be chained

    s = with_dir(ps.add_parser("seal"))  # `pron seal` subcommand, which needs the run directory
    s.add_argument("--rubric")  # optional rubric file overriding the bundled pronunciation rubric
    m = with_dir(ps.add_parser("measure"))  # `pron measure` subcommand, which needs the run directory
    m.add_argument("--request", default=str(pm.asr.DEFAULT_REQUEST))  # script to measure (default from paths.py)
    # document model with each token's reference reading
    m.add_argument("--docmodel", default=str(pm.asr.DEFAULT_DOCMODEL))
    # folder holding the narration WAVs and timing records
    m.add_argument("--speech-dir", default=str(pm.asr.DEFAULT_SPEECH))
    # overrides file whose hash is part of the page fingerprint
    m.add_argument("--overrides", default=str(pm.DEFAULT_OVERRIDES))
    m.add_argument("--segments")  # optional comma-separated segment ids
    m.add_argument("--no-lexicon", action="store_true")  # skip the slow espeak dictionary comparison
    m.add_argument("--note")  # free-text note stored in the measurement
    dd = with_dir(ps.add_parser("decide"))  # `pron decide` subcommand, which needs the run directory
    dd.add_argument("--measurement")  # optional explicit measurement file
    # text describing the change, stored in the log
    dd.add_argument("--change", help="what changed since the last loop (recorded in the log)")
    t = ps.add_parser("trial")  # `pron trial`, which needs no run directory
    t.add_argument("--plan", required=True)  # the plan JSON listing words and candidate spellings
    t.add_argument("--out", required=True)  # output folder for the trial WAVs and trial.json
    t.add_argument("--takes", type=int, default=3)  # takes per variant and occurrence (the voice is stochastic)
    ap_ = with_dir(ps.add_parser("apply"))  # `pron apply` subcommand, which needs the run directory
    # stored as overrides_file so it does not clash with measure's --overrides
    ap_.add_argument("--overrides", dest="overrides_file", required=True)
    rt = with_dir(ps.add_parser("retake"))  # `pron retake` subcommand, which needs the run directory
    rt.add_argument("--segments", required=True)  # comma-separated segment ids to voice again
    # comma-separated target words that must be heard correctly
    rt.add_argument("--words", required=True, help="comma-separated words that must be heard correctly")
    rt.add_argument("--takes", type=int, default=3)  # new takes per segment
    with_dir(ps.add_parser("revert"))  # `pron revert`, which needs only --dir
    with_dir(ps.add_parser("status"))  # `pron status`, which also needs only --dir
    p.set_defaults(fn=main_cmd)  # every action is routed through main_cmd
