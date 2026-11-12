# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Trial synthesis for pronunciation candidates. Run with the speechkit runtime venv, not system python.

stdin: JSON {"model": onnx path, "config": voice json path,
             "jobs": [{"text": "...", "scale": 1.24, "out": "path.wav"}]}
Each job is voiced the way the narration voices a segment: split into sentences, 0.38 s between them,
volume 0.8, the segment's own length scale, espeak phonemes from the voice config. Text may contain
Piper raw-phoneme blocks ([[...]]). One JSON line per finished job goes to stdout.
"""
import json  # the job specification arrives as JSON on stdin and progress lines leave as JSON
import re  # splits each job's text into sentence pieces
import sys  # stdin and stdout are the only channels to the parent process (trial.synthesize)
import wave  # standard-library writer for the PCM WAV files

import numpy as np  # audio buffers are numpy arrays of 16-bit samples
import onnxruntime as ort  # onnxruntime runs the voice model; it is configured directly here to limit threads
# PiperVoice turns text into audio; SynthesisConfig carries the per-call speed and volume
from piper import PiperVoice, SynthesisConfig
from piper.config import PiperConfig  # builds the voice settings object from the voice JSON

# Output settings that copy the narration: 22050 Hz is the ljspeech-high sample rate, 0.38 s is the
# silence the narration leaves between sentence pieces, and 0.8 is its output volume (1.0 = unscaled).
SR, PAUSE_S, VOLUME = 22050, 0.38, 0.8


# Voices every job in the spec read from stdin, one WAV per job, so trial.py can listen to candidate
# spellings in their real sentences. No parameters: stdin holds {"model", "config", "jobs": [{"text",
# "scale", "out"}]}. Side effects: writes each job's "out" WAV file and prints one JSON line per finished job.
# Runs inside the speechkit runtime venv, started by trial.synthesize, often as several workers at once.
def main():
    spec = json.load(sys.stdin)  # read the whole job list sent by the parent
    opts = ort.SessionOptions()  # session options for onnxruntime
    # One inference thread per process: trial.synthesize starts several of these workers in parallel, and more
    # threads each would only oversubscribe the CPU.
    opts.intra_op_num_threads = 1
    # The voice is assembled by hand (instead of PiperVoice.load) so the session options above take effect.
    # load the ONNX voice model with those options
    voice = PiperVoice(session=ort.InferenceSession(spec["model"], sess_options=opts,
                                                    # force the CPU execution provider so no GPU library is probed
                                                    providers=["CPUExecutionProvider"]),
                       # phoneme map, espeak voice and sample rate come from the voice config JSON
                       config=PiperConfig.from_dict(json.load(open(spec["config"], encoding="utf-8"))))
    for job in spec["jobs"]:  # each job is one take of one sentence with one candidate spelling
        bufs = []  # 16-bit sample arrays to be concatenated for this job
        # Split after . ! or ? followed by whitespace, the same sentence pieces the narration voiced separately;
        # empty pieces are skipped and k counts the pieces that remain.
        for k, piece in enumerate(p for p in re.split(r"(?<=[.!?])\s+", job["text"].strip()) if p):
            if k:  # before every piece except the first
                # insert the inter-sentence silence: round(0.38 * 22050) = 8379 zero samples
                bufs.append(np.zeros(round(PAUSE_S * SR), dtype="<i2"))
            # synthesize yields one audio chunk per espeak sentence, at the segment's own speed
            for ch in voice.synthesize(piece, SynthesisConfig(length_scale=job["scale"], volume=VOLUME)):
                # wrap the chunk's raw little-endian 16-bit bytes as an array without copying
                bufs.append(np.frombuffer(ch.audio_int16_bytes, dtype="<i2"))
        # join all pieces; a single silent sample keeps the WAV valid if nothing was voiced
        a = np.concatenate(bufs) if bufs else np.zeros(1, dtype="<i2")
        with wave.open(job["out"], "wb") as w:  # open the job's output path for writing as a WAV
            # mono, 2 bytes per sample, 22050 Hz, frame count fixed up on close, uncompressed PCM
            w.setparams((1, 2, SR, 0, "NONE", "not compressed"))
            w.writeframes(a.tobytes())  # write all samples in one call
        # Report the finished file and its duration in seconds; flush so the line leaves the pipe immediately.
        print(json.dumps({"out": job["out"], "seconds": round(len(a) / SR, 3)}), flush=True)


if __name__ == "__main__":  # run only when executed as a script
    main()  # no arguments: everything comes from stdin
