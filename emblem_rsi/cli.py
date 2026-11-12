# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Command line entry point: python -m emblem_rsi <command>."""
# postpone annotation evaluation so `list[str] | None` works on older Python 3 versions
from __future__ import annotations

import argparse  # standard library parser for subcommands, options and --help text
import json  # not used directly below; kept for subcommand modules that print JSON
import sys  # gives sys.stderr for error messages that should not mix with normal output
import tempfile  # temporary folders and files for the self-test's synthetic audio
from pathlib import Path  # object-oriented file paths that work the same on Windows and Linux

# the package version for --version, and the audio signature analyser imported under the short name sg
from . import __version__, signature as sg


# Handler for `emblem-rsi signature AUDIO [-o OUT]`: analyse one audio file and save its signature JSON.
# Parameter a is the parsed argparse namespace holding a.audio (input path) and a.out (optional output path).
# Returns 0 on success or 2 when the input is not usable audio; it writes one JSON file and prints a summary.
# This is the command a person runs to produce the signature that the viscous-letters page and the loop consume.
def cmd_signature(a: argparse.Namespace) -> int:
    try:  # analysis can fail for ordinary reasons (missing file, text stub, silence), so guard it
        # decode with ffmpeg and compute level, tempo, low fundamental, bands and the 10 Hz timeline
        sig = sg.analyse(a.audio)
    except sg.SignatureError as e:  # SignatureError carries a plain-language reason meant for the user, not a traceback
        # report the reason on stderr with a program prefix so scripts can tell it apart from output
        print(f"emblem-rsi: {e}", file=sys.stderr)
        return 2  # exit status 2 means bad input, matching the convention used across the toolkit
    # default output sits next to the audio: song.mp3 becomes song.signature.json
    out = Path(a.out) if a.out else Path(a.audio).with_suffix(".signature.json")
    sg.write(sig, out)  # save the signature as compact single-line JSON
    # unpack the two sections used in the printed summary: spectrum facts and tempo facts
    s, t = sig["spectrum"], sig["tempo"]
    # confirm the file location and its size in bytes so the user sees it is small
    print(f"wrote {out}  ({out.stat().st_size} bytes)")
    # length in seconds, beats per minute and the 0..1 confidence of the tempo estimate
    print(f"  duration {sig['decoded']['duration_s']} s   tempo {t['bpm']} BPM (confidence {t['confidence']})")
    # lowest strong pitch in Hz (the bass note), its confidence, and the spectral centroid (brightness) in Hz
    print(f"  low fundamental {s['low_fundamental_hz']} Hz (confidence {s['low_fundamental_confidence']})   centroid {s['centroid_hz']} Hz")
    # share of total energy in each of the six bands, printed as whole percentages
    print("  band share  " + "  ".join(f"{k} {v:.0%}" for k, v in s["band_share"].items()))
    return 0  # exit status 0 after writing and summarising the signature


# Handler for `emblem-rsi selftest`: proves the analyser works by feeding it audio whose answer is known.
# Parameter a is the argparse namespace (unused). It builds a 120 BPM track with a 55 Hz bass, analyses it,
# and also checks that a 9-byte text stub is rejected. Returns 0 when every check passes, 1 otherwise.
# Side effects: runs ffmpeg twice and creates temporary files; useful after installing ffmpeg or numpy.
def cmd_selftest(a: argparse.Namespace) -> int:
    from . import fixtures  # imported here so the normal signature command does not pay for loading the test generator
    bpm, bass = 120.0, 55.0  # the known answers: tempo in beats per minute and bass pitch in Hz
    with tempfile.TemporaryDirectory() as d:  # a throwaway folder that is deleted, with its files, when the block ends
        # synthesise kick, bass and hats, write a WAV and encode it to MP3 with ffmpeg
        mp3 = fixtures.make_mp3(Path(d) / "fixture.mp3", bpm=bpm, bass_hz=bass)
        sig = sg.analyse(mp3)  # run the real analyser on the encoded file, exactly as a user's song would be handled
    t, s = sig["tempo"], sig["spectrum"]  # pull out the tempo and spectrum sections for the comparisons below
    # Each check is a tuple of (description, passed flag, measured value shown in brackets).
    checks = [
        # tempo estimate must land within 2 BPM of the 120 BPM click track
        ("tempo within 2 BPM of 120", abs(t["bpm"] - bpm) <= 2.0, t["bpm"]),
        # the harmonic product spectrum must find the 55 Hz bass within 3 Hz
        ("low fundamental within 3 Hz of 55", abs(s["low_fundamental_hz"] - bass) <= 3.0, s["low_fundamental_hz"]),
        # the test signal is mostly kick and bass, so more than 80% of energy should sit below 250 Hz
        ("sub+bass hold most of the energy", s["band_share"]["sub"] + s["band_share"]["bass"] > 0.8, round(s["band_share"]["sub"] + s["band_share"]["bass"], 3)),
        # the downsampled timeline must report its fixed rate of 10 values per second
        ("timeline is 10 Hz", abs(sig["timeline"]["hz"] - 10) < 0.5, sig["timeline"]["hz"]),
        # decoded length must match the 24 s that make_signal produced, within 0.2 s
        ("duration within 0.2 s of 24", abs(sig["decoded"]["duration_s"] - 24) < 0.2, sig["decoded"]["duration_s"]),
    ]
    # mkdtemp makes a folder that is not cleaned up automatically, so the stub file survives the check
    bad = Path(tempfile.mkdtemp()) / "stub.mp3"
    # a 9-byte text file shaped like a failed download saved under an .mp3 name
    bad.write_text("Not Found", encoding="utf-8")
    try:  # analyse should refuse this file before ever calling ffmpeg
        sg.analyse(bad); rejected = False  # if analyse returns normally the stub was wrongly accepted
    except sg.SignatureError:  # the expected outcome: check_input recognised plain text
        rejected = True  # remember that the stub was correctly rejected
    # add the rejection test to the list; its shown value is the same boolean
    checks.append(("rejects a 9-byte 'Not Found' stub", rejected, rejected))
    ok = all(c[1] for c in checks)  # overall result is true only when every check's passed flag is true
    for name, passed, val in checks:  # walk the checks in order to print one line each
        # pad "ok" to the width of "FAIL" so the descriptions line up in a column
        print(f"{'ok  ' if passed else 'FAIL'} {name}  [{val}]")
    return 0 if ok else 1  # exit status 0 for a clean pass, 1 if any check failed


# Command line entry point for the whole emblem-rsi toolkit (`python -m emblem_rsi <command>`).
# Parameter argv is an optional list of argument strings; None means read them from sys.argv.
# It builds the parser, registers the optional command groups (loop, pron, code) when their modules import,
# then calls the handler stored on the chosen subcommand and returns its integer exit status.
def main(argv: list[str] | None = None) -> int:
    # program name shown in help text; the module docstring becomes the description
    p = argparse.ArgumentParser(prog="emblem-rsi", description=__doc__)
    # `--version` prints the package version and exits
    p.add_argument("--version", action="version", version=__version__)
    # one subparser per command; required=True makes a bare `emblem-rsi` an error with usage help
    sub = p.add_subparsers(dest="cmd", required=True)
    # the audio analysis subcommand with its help text
    s = sub.add_parser("signature", help="analyse an audio file and write <name>.signature.json")
    # positional input path, plus an optional output path given with -o or --out
    s.add_argument("audio"); s.add_argument("-o", "--out")
    s.set_defaults(fn=cmd_signature)  # store the handler function on the namespace so main can call a.fn(a)
    # the known-answer check of the analyser
    t = sub.add_parser("selftest", help="check the analyser against synthetic audio with a known answer")
    t.set_defaults(fn=cmd_selftest)  # the self-test takes no arguments, only its handler
    try:  # each optional group below is skipped quietly if its module or dependencies are missing
        from . import loop  # the sealed-rubric audit loop for the viscous-letters page
        loop.register(sub)  # adds `loop seal`, `loop measure`, `loop decide`, `loop run` and `loop status`
    except ImportError:  # missing optional pieces should not break the core signature command
        pass  # nothing gets registered when the loop module is unavailable
    try:  # the pronunciation audit commands are a separate optional group
        from .pron import cli as pron_cli  # imported under an alias because this module already defines main
        # adds the `pron` subcommand with seal, measure, decide, trial, apply, retake, revert, status
        pron_cli.register(sub)
    except ImportError:  # pronunciation tooling may lack its own dependencies on some machines
        pass  # leave the pron commands out when they cannot load
    try:  # the code-comment audit (show, check, apply and the rest) is the third optional group
        from .code import cli as code_cli  # loaded the same guarded way as the others
        code_cli.register(sub)  # adds the `code` subcommand used by this commenting workflow
    except ImportError:  # tolerate an incomplete install without the code-audit module
        pass  # skip the code commands silently in that case
    a = p.parse_args(argv)  # parse argv (or the real command line); argparse exits with status 2 on usage errors
    return a.fn(a)  # run the selected handler and return its exit status to __main__
