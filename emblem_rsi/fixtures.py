# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Synthetic audio with a known answer, used to test the analyser. These are test tones, not music."""
from __future__ import annotations  # allows modern annotation syntax regardless of the Python 3 minor version

import subprocess  # runs ffmpeg to turn the test WAV into an MP3
import wave  # standard library WAV writer; no extra audio package is needed
from pathlib import Path  # filesystem paths that work on Windows and Linux alike

import numpy as np  # sine waves, noise and array arithmetic for the synthetic track

SR = 44100  # CD sample rate in Hz, so the analyser also exercises its 44.1 kHz to 22.05 kHz resampling


# Build a synthetic drum-and-bass loop with a known tempo and bass pitch for the analyser's self-test.
# Parameters: bpm is beats per minute, bass_hz the bass note in Hz, seconds the length, seed fixes the noise.
# Returns mono float32 samples at SR with peaks below full scale. Pure computation: nothing is written.
# A kick lands on every beat, a constant bass tone runs throughout, and short noise hats sit on the off-beats.
def make_signal(bpm: float = 120.0, bass_hz: float = 55.0, seconds: float = 24.0, seed: int = 7) -> np.ndarray:
    """Kick on every beat, a steady bass note, and quiet hats on the off-beats."""
    rng = np.random.default_rng(seed)  # seeded random generator, so every run produces the identical hat noise
    n = int(seconds * SR)  # total number of samples in the generated clip
    t = np.arange(n) / SR  # time in seconds of each sample, as a float array
    # bass: the fundamental plus a quieter second harmonic, like a real bass note's overtone
    x = 0.20 * np.sin(2 * np.pi * bass_hz * t) + 0.06 * np.sin(2 * np.pi * 2 * bass_hz * t)
    beat = 60.0 / bpm  # beat period in seconds derived from the tempo
    k = 0  # beat counter, starting at the first downbeat
    # keep adding beats until 0.4 s before the end, so the last kick (0.35 s long) fits inside the clip
    while k * beat < seconds - 0.4:
        i0 = int(k * beat * SR)  # sample index where this beat's kick starts
        tt = np.arange(int(0.35 * SR)) / SR  # time axis in seconds covering one 0.35 s kick
        # Kick drum pitch sweep in Hz: starts high for the click and falls fast towards a 45 Hz boom.
        f = 45 + 95 * np.exp(-tt * 30)                 # sweep from about 140 Hz down to 45 Hz
        # integrate the instantaneous frequency to get a continuous phase in radians
        phase = 2 * np.pi * np.cumsum(f) / SR
        # sine at that phase with a fast exponential decay (time constant about 0.11 s), peak amplitude 0.8
        kick = 0.8 * np.sin(phase) * np.exp(-tt * 9)
        # mix the kick into the signal; the slice of kick is trimmed if it would run past the end
        x[i0 : i0 + kick.size] += kick[: n - i0]
        j0 = int((k + 0.5) * beat * SR)  # sample index of the off-beat, halfway between this beat and the next
        if j0 + int(0.05 * SR) < n:  # only add a hat when its 50 ms burst fits completely
            # 50 ms of white noise with a quick decay (about 12.5 ms time constant), quiet at 0.08
            h = rng.standard_normal(int(0.05 * SR)) * np.exp(-np.arange(int(0.05 * SR)) / SR * 80) * 0.08
            x[j0 : j0 + h.size] += h  # mix the hat noise burst into the signal
        k += 1  # advance to the next beat of the grid
    # scale down only if the mix exceeds about 0.95, leaving 5% headroom; return float32
    return (x / max(1.0, np.abs(x).max() * 1.05)).astype(np.float32)


# Write mono samples as a 16-bit PCM WAV file at SR.
# Parameters: x holds float samples in -1..1, path is the destination. Returns path. Overwrites the file.
def write_wav(x: np.ndarray, path: Path) -> Path:
    # open the WAV writer in binary write mode; the with-block closes the file and fixes up the header
    with wave.open(str(path), "wb") as w:
        # one channel, 2 bytes per sample (16-bit), SR samples per second
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        # clip to the legal range, scale to the int16 range and write little-endian bytes
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return path  # return the path so the caller can chain the next step


# Create a known-answer MP3 for the self-test: synthesise the signal, write a WAV, encode it with ffmpeg.
# Parameter path is the MP3 destination; keyword arguments (bpm, bass_hz, seconds, seed) go to make_signal.
# Returns path. Side effects: runs ffmpeg, writes the MP3 and deletes the temporary WAV beside it.
def make_mp3(path: Path, **kw) -> Path:
    # the intermediate WAV has the same name with a .wav extension
    wav = write_wav(make_signal(**kw), path.with_suffix(".wav"))
    # encode with LAME at 128 kbit/s, overwriting quietly; check=True raises if ffmpeg fails
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-codec:a", "libmp3lame", "-b:a", "128k", str(path)], check=True)
    wav.unlink()  # remove the temporary WAV so only the MP3 remains
    return path  # the encoded file, ready for signature.analyse
