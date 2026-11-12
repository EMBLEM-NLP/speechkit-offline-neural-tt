# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Audio signature extraction for emblem-rsi.

Turns an audio file (mp3, wav, anything ffmpeg can decode) into a small JSON "signature":
level, tempo, the low fundamental, six log-spaced band energies, and a 10 Hz timeline.
The page uses it to drive the vibration of the liquid. Only numpy and ffmpeg are needed.

The signature describes the sound. It never contains the audio itself.
"""
# lets the `str | Path` and `tuple[float, float]` annotations parse on every supported Python 3
from __future__ import annotations

import hashlib  # SHA-256 fingerprint of the source file, stored so a signature can be traced to its audio
import json  # serialises the finished signature dictionary
import math  # log10 for decibel conversions on plain floats
import subprocess  # launches ffmpeg, which does all audio decoding
from pathlib import Path  # filesystem paths that behave the same on Windows and Linux

import numpy as np  # vectorised maths: FFTs, windows, sums and percentiles over sample arrays

# schema tag written into every signature so readers can refuse a format they do not know
SCHEMA = "emblem-rsi.signature/1"
# Every file is resampled to 22050 Hz: half of CD rate is plenty for bass, tempo and the bands up to 11 kHz,
# and it halves the work compared with 44.1 kHz.
SR = 22050            # analysis sample rate
N_FFT = 2048  # FFT size in samples: about 93 ms per frame at 22050 Hz, giving roughly 10.8 Hz per bin
# Step between successive analysis frames, in samples; 22050 / 512 gives the frame rate below.
HOP = 512             # 43.07 frames per second
TIMELINE_HZ = 10  # the output timeline carries 10 values per second, one every 100 ms
# The six frequency bands, low to high, spaced roughly logarithmically like hearing; each entry is (name, Hz, Hz).
BANDS = (             # name, low Hz, high Hz
    ("sub", 20, 60),  # sub-bass: felt more than heard, kick drum thump
    ("bass", 60, 250),  # bass: bass guitar and the body of the kick
    ("lowmid", 250, 500),  # low mids: warmth and the lower half of most voices
    ("mid", 500, 2000),  # mids: the core of vocals and most instruments
    ("highmid", 2000, 6000),  # upper mids: presence, consonants, snare crack
    ("air", 6000, 11000),  # air: cymbals and sparkle, up to just under the 11025 Hz Nyquist limit
)


# Raised whenever the input cannot be analysed (missing file, text stub, undecodable, silent).
# Callers catch it to print the plain-language message instead of a Python traceback.
# subclass of Exception so a bare `except SignatureError` catches only analysis problems
class SignatureError(Exception):
    """The input is not usable audio. The message says why, in plain words."""


# Hash a file of any size with SHA-256, returning the 64-character hexadecimal digest.
# Parameter path is the file to read; it is streamed in 1 MiB chunks so a large WAV never sits in memory twice.
# The digest goes into the signature's "source" block to identify exactly which audio was analysed.
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()  # incremental hasher that is fed one chunk at a time
    with open(path, "rb") as f:  # open in binary mode because audio bytes are not text
        # iter() with a sentinel keeps calling read(1 MiB) until it returns b"" at end of file
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)  # fold this chunk into the running digest
    return h.hexdigest()  # lowercase hex string, the usual way checksums are shown


# Decide whether the first bytes of a file look like printable ASCII text rather than binary audio.
# Parameter head holds up to 64 leading bytes; returns True when every byte is printable or a tab or line break.
# check_input uses this to catch error pages and "Not Found" bodies saved under an audio file name.
def _looks_like_text(head: bytes) -> bool:
    # empty input is not text; bytes 32..126 are printable ASCII, 9, 10 and 13 are tab, LF and CR
    return len(head) > 0 and all(32 <= b < 127 or b in (9, 10, 13) for b in head)


# Cheap sanity checks run before ffmpeg is started, so obviously broken inputs fail fast with a clear message.
# Parameter path is the candidate audio file. Returns nothing; raises SignatureError when the file is missing,
# is a small plain-text file, or is under 1 KiB. Nothing is written.
def check_input(path: Path) -> None:
    """Reject files that cannot be audio before spending time on them."""
    if not path.is_file():  # a directory or a missing path cannot be decoded
        raise SignatureError(f"{path} does not exist")  # name the path so the user can see a typo at once
    size = path.stat().st_size  # file size in bytes, used by both size rules below
    # only the first 64 bytes are inspected (note: read_bytes loads the whole file before slicing)
    head = path.read_bytes()[:64]
    # real audio has binary headers; short all-text content is almost certainly an HTML or error body
    if _looks_like_text(head) and size < 4096:
        raise SignatureError(  # the message shows the text itself so the user recognises what happened
            # !r quotes the decoded snippet, with undecodable bytes replaced by a marker
            f"{path.name} is {size} bytes of plain text ({head.decode('ascii', 'replace')!r}), not audio. "
            # explain the usual cause and the fix in plain words
            "It looks like a failed download saved under an audio name. Re-export or re-upload the real file."
        )
    if size < 1024:  # anything under 1 KiB cannot hold even a fraction of a second of compressed music
        # reject the file, quoting its size in the message
        raise SignatureError(f"{path.name} is only {size} bytes, far too small to be a song")


# Decode any ffmpeg-readable file into one channel of 32-bit float samples in -1..1 at sample rate sr.
# Parameter path is the audio file; sr is the target rate in Hz (default SR, 22050).
# Returns a 1-D numpy float32 array. Starts an ffmpeg process; raises SignatureError when ffmpeg is missing,
# fails to decode, or produces less than half a second of audio.
def decode(path: Path, sr: int = SR) -> np.ndarray:
    """Decode to mono float32 at `sr` using ffmpeg."""
    # quiet logging, raw little-endian float32 to stdout, downmixed to mono and resampled to sr
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"]
    try:  # ffmpeg problems are translated into SignatureError below
        # capture stdout (the samples) and stderr (diagnostics); check=True raises on a non-zero exit
        out = subprocess.run(cmd, capture_output=True, check=True)
    except FileNotFoundError as e:  # the ffmpeg executable could not be found on PATH at all
        # chain the original exception so a debugger still sees the root cause
        raise SignatureError("ffmpeg is not installed or not on PATH") from e
    except subprocess.CalledProcessError as e:  # ffmpeg ran but rejected the file
        # stderr bytes to text lines, keeping only meaningful output
        msg = e.stderr.decode("utf-8", "replace").strip().splitlines()
        # the last ffmpeg error line is usually the most specific one
        raise SignatureError(f"ffmpeg could not decode {path.name}: {msg[-1] if msg else 'unknown error'}") from e
    # view the raw bytes as little-endian float32 samples and make sure the dtype is native float32
    x = np.frombuffer(out.stdout, dtype="<f4").astype(np.float32)
    if x.size < sr // 2:  # fewer than sr/2 samples means under 0.5 s, too little for tempo or spectrum work
        # report the decoded length in seconds with two decimals
        raise SignatureError(f"{path.name} decodes to {x.size / sr:.2f} s of audio, too short to analyse")
    return x  # the mono sample array ready for analysis


# Short-time Fourier transform magnitudes of a mono signal, one row per frame of N_FFT samples every HOP samples.
# Parameter x is float32 audio at SR. Returns a float32 array of shape (frames, N_FFT/2 + 1) with linear
# magnitudes. This single spectrogram feeds the band energies, the onset envelope and the per-frame level.
def stft_mag(x: np.ndarray) -> np.ndarray:
    """Magnitude spectrogram, shape (frames, bins)."""
    if x.size < N_FFT:  # a clip shorter than one frame would produce no frames at all
        x = np.pad(x, (0, N_FFT - x.size))  # pad with silence at the end up to exactly one frame
    # Hann window tapers each frame's edges to reduce spectral leakage between bins
    win = np.hanning(N_FFT).astype(np.float32)
    n = 1 + (x.size - N_FFT) // HOP  # number of whole frames that fit when stepping HOP samples at a time
    # index matrix: row k holds the sample positions HOP*k .. HOP*k + N_FFT - 1 (broadcast, no Python loop)
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    # window every frame, real FFT along each row, keep the magnitude as float32 to halve memory
    return np.abs(np.fft.rfft(x[idx] * win, axis=1)).astype(np.float32)


# Total energy per frame inside each of the six BANDS, from a magnitude spectrogram.
# Parameter mag has shape (frames, bins) as returned by stft_mag. Returns shape (frames, 6), in squared
# magnitude units (relative power, not calibrated). Used for band shares and the per-band timeline.
def band_matrix(mag: np.ndarray) -> np.ndarray:
    """Per-frame energy in each named band, shape (frames, bands)."""
    # centre frequency in Hz of every FFT bin, matching the N_FFT and SR used for mag
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    cols = []  # one column of energies per band, collected in BANDS order
    for _, lo, hi in BANDS:  # the band name is not needed here, only its low and high edges in Hz
        sel = (freqs >= lo) & (freqs < hi)  # boolean mask of bins whose frequency lies in [lo, hi)
        # power is magnitude squared; summing over the selected bins gives each frame's band energy
        cols.append((mag[:, sel] ** 2).sum(axis=1))
    return np.stack(cols, axis=1)  # combine the columns into one (frames, bands) matrix


# Estimate the lowest strong pitch (the bass note) between 30 and 220 Hz, with a confidence from 0 to 1.
# Parameter x is the mono signal at SR. Returns (frequency in Hz, confidence); (0.0, 0.0) means nothing usable.
# The page compares its own vibration pitch with this value, and the loop's measure.mjs scores how close it is.
def low_fundamental(x: np.ndarray) -> tuple[float, float]:
    """Strongest fundamental between 30 and 220 Hz, with a 0..1 confidence.

    Uses long frames (about 1.3 Hz resolution) and a harmonic product spectrum, so a bass note
    with strong overtones still reports its fundamental.
    """
    n_fft = 16384  # 16384-sample frames give about 1.35 Hz per bin at 22050 Hz, fine enough to tell 55 Hz from 58 Hz
    if x.size < n_fft:  # clips shorter than one long frame
        x = np.pad(x, (0, n_fft - x.size))  # pad them with silence so at least one frame exists
    win = np.hanning(n_fft).astype(np.float32)  # Hann window for the long frames, as float32 to match the signal
    hop = n_fft // 2  # 50% overlap between long frames
    n = 1 + (x.size - n_fft) // hop  # number of whole long frames in the signal
    # running sum of the power spectrum, in float64 to avoid precision loss over many frames
    acc = np.zeros(n_fft // 2 + 1, dtype=np.float64)
    for i in range(n):  # walk the long frames one by one (few of them, so a loop is cheap)
        seg = x[i * hop : i * hop + n_fft] * win  # slice frame i and apply the window
        acc += np.abs(np.fft.rfft(seg)) ** 2  # add its power spectrum (magnitude squared)
    acc /= max(n, 1)  # turn the sum into an average power spectrum; max() guards against dividing by zero
    freqs = np.fft.rfftfreq(n_fft, 1 / SR)  # centre frequency in Hz of each long-FFT bin
    # search range: the lowest note of a bass guitar sits near 41 Hz, and 220 Hz is the top of the bass register
    lo, hi = 30.0, 220.0
    sel = np.where((freqs >= lo) & (freqs <= hi))[0]  # indices of bins inside the search range
    if sel.size == 0 or acc[sel].max() <= 0:  # no bins fall in range, or the range holds no energy at all
        return 0.0, 0.0  # report "unknown" as zero frequency with zero confidence
    amp = np.sqrt(acc)  # amplitude spectrum, so products of harmonics do not explode numerically
    hps = np.zeros(sel.size)  # one harmonic-product score per candidate bin
    for k, bin_ in enumerate(sel):  # try every candidate bin as a possible fundamental
        v = amp[bin_]  # start with the candidate's own amplitude
        for h in (2, 3):  # multiply in the second and third harmonic amplitudes
            j = bin_ * h  # bin index where that harmonic would fall
            if j < amp.size:  # skip a harmonic that would fall past the end of the spectrum
                # take the strongest of the three bins around it, since a harmonic rarely lands exactly on one bin
                v *= amp[j - 1 : j + 2].max()
        # a high product means the bin and its harmonics are all strong, the signature of a real pitched note
        hps[k] = v
    best = int(np.argmax(hps))  # position of the best candidate inside sel
    f0 = float(freqs[sel[best]])  # its bin-centre frequency, before sub-bin refinement
    # refine with parabolic interpolation on the plain spectrum
    b = int(sel[best])  # the bin index in the full spectrum
    if 1 <= b < amp.size - 1:  # refinement needs a neighbour on each side
        a0, a1, a2 = amp[b - 1], amp[b], amp[b + 1]  # amplitudes of the best bin and its two neighbouring bins
        d = (a0 - 2 * a1 + a2)  # curvature of the parabola through those three points
        if d != 0:  # a flat top would divide by zero, so keep the bin-centre value in that case
            # vertex of the parabola: shift by a fraction of the bin width freqs[1] - freqs[0]
            f0 = float(freqs[b] + 0.5 * (a0 - a2) / d * (freqs[1] - freqs[0]))
    band = hps  # the harmonic product scores serve as the band used for the confidence
    # how far the peak stands above the median score, as a share of the peak; 1e-12 avoids a divide by zero
    conf = float((band.max() - np.median(band)) / (band.max() + 1e-12))
    return f0, max(0.0, min(1.0, conf))  # clamp the confidence into 0..1 before returning


# Onset strength per frame, measured as spectral flux: how much new energy appears from one frame to the next.
# Parameter mag is the magnitude spectrogram. Returns float32 values, one per frame (the first is 0).
# Peaks in this curve mark drum hits and note starts; the tempo estimator and the timeline both use it.
def onset_envelope(mag: np.ndarray) -> np.ndarray:
    """Spectral flux: summed positive change of the log spectrum, one value per frame."""
    # log compression (log1p of a scaled magnitude) so quiet hits count, not only the loudest bins
    logm = np.log1p(mag * 10.0)
    # frame-to-frame increase per bin, ignoring decreases, summed across all bins
    flux = np.maximum(logm[1:] - logm[:-1], 0).sum(axis=1)
    # prepend 0 for the first frame, which has no predecessor, keeping one value per frame
    return np.concatenate([[0.0], flux]).astype(np.float32)


# Estimate the tempo from the onset envelope by autocorrelation, and find where the first beat falls.
# Parameter onset is the per-frame onset curve at SR/HOP frames per second.
# Returns (beats per minute, confidence 0..1, offset of the first beat in seconds); all zeros if unknown.
def estimate_tempo(onset: np.ndarray) -> tuple[float, float, float]:
    """Return (bpm, confidence 0..1, first-beat offset in seconds)."""
    fps = SR / HOP  # frames per second of the onset curve, about 43.07
    o = onset - onset.mean()  # remove the mean so the autocorrelation measures periodicity, not overall level
    # fewer than 4 seconds of frames, or a perfectly flat curve, cannot reveal a tempo
    if o.size < fps * 4 or not np.any(o):
        return 0.0, 0.0, 0.0  # report an unknown tempo with zero confidence and offset
    n = o.size  # number of onset frames available for the autocorrelation
    f = np.fft.rfft(o, 2 * n)  # FFT with zero padding to 2n, so the correlation below is linear rather than circular
    # power spectrum back to the time domain gives the autocorrelation for lags 0..n-1
    ac = np.fft.irfft(f * np.conj(f))[:n]
    ac /= ac[0] + 1e-12  # normalise so lag 0 equals 1; the tiny constant prevents a divide by zero
    lags = np.arange(n)  # candidate lag values, measured in onset frames
    # tempo implied by each lag: 60 * frames per second / lag; lag 0 is mapped to 0
    bpm_of_lag = np.where(lags > 0, 60.0 * fps / np.maximum(lags, 1), 0)
    ok = (bpm_of_lag >= 60) & (bpm_of_lag <= 190)  # only consider tempos between 60 and 190 beats per minute
    cand = np.where(ok)[0]  # indices of the candidate lags inside that tempo window
    # Favour tempos near 120 BPM, the middle of the range most dance and pop music sits in. There is no hard
    # preference window inside 60..190: the weight is a bell curve on a log2 scale centred on 120 BPM with a
    # spread of 0.6 octave, so when two lags fit about equally well the one nearer 120 BPM wins, which reduces
    # double-tempo and half-tempo mistakes. It is 1.0 at 120 BPM and about 0.25 at 60 BPM, one octave away.
    weight = np.exp(-0.5 * ((np.log2(bpm_of_lag[cand] / 120.0)) / 0.6) ** 2)
    score = ac[cand] * weight  # periodicity strength times the tempo preference
    best = int(cand[np.argmax(score)])  # lag (in frames) with the best weighted score
    # refine the lag with a parabola
    if 1 <= best < n - 1:  # need a neighbour on each side for the parabola
        a0, a1, a2 = ac[best - 1], ac[best], ac[best + 1]  # autocorrelation at the best lag and its neighbours
        d = a0 - 2 * a1 + a2  # curvature of the parabola fitted through the three values
        shift = 0.5 * (a0 - a2) / d if d != 0 else 0.0  # sub-frame shift of the vertex; zero when the top is flat
    else:  # at the edge of the range there is nothing to refine
        shift = 0.0  # keep the integer lag without any sub-frame refinement
    lag = best + shift  # fractional beat period in frames
    bpm = 60.0 * fps / lag  # convert the period to beats per minute
    # how far the chosen peak stands above typical candidates, scaled to 0..1 and clamped
    conf = float(max(0.0, min(1.0, (ac[best] - np.median(ac[cand])) / (1 - np.median(ac[cand]) + 1e-9))))
    # beat offset: comb with the beat period, pick the phase that collects most onset energy
    period = lag  # beat period in frames (fractional)
    phases = np.arange(0, period, 0.25)  # candidate phases from 0 to one period, in quarter-frame steps
    totals = []  # onset energy collected by each phase
    for p in phases:  # try each candidate phase of the beat grid
        pos = np.arange(p, n, period).astype(int)  # frame positions of a beat grid starting at phase p
        pos = pos[pos < n]  # drop positions past the end (a safety check, since arange already stops at n)
        totals.append(o[pos].sum())  # sum the onset strength on that grid
    offset = float(phases[int(np.argmax(totals))] / fps)  # the best phase, converted from frames to seconds
    return float(bpm), conf, offset  # plain Python floats so the values serialise cleanly to JSON


# Round every value of an array to `digits` decimals and return a plain Python list.
# Parameter a is any numeric array; digits defaults to 3. Keeps the JSON small (0.123 rather than 0.12345678).
def _q(a: np.ndarray, digits: int = 3) -> list[float]:
    return [round(float(v), digits) for v in a]  # float() converts numpy scalars, which json cannot serialise directly


# Build the 10 Hz timeline: downsample per-frame level, onset and band energies into 0.1 s bins.
# Parameters are rms (frames), bands (frames x 6) and onset (frames), all at about 43 frames per second.
# Returns a dict with "hz" and lists scaled 0..1 (each series by its own 99th percentile) for the page to animate.
def timeline(rms: np.ndarray, bands: np.ndarray, onset: np.ndarray) -> dict:
    """Average the per-frame series into exact TIMELINE_HZ bins and normalise each series to 0..1."""
    fps = SR / HOP  # analysis frames per second
    n_bins = max(1, int(rms.size / fps * TIMELINE_HZ))  # whole number of 0.1 s bins that fit in the clip, at least one
    # frame index where each bin starts; rounding keeps the bins exactly 0.1 s apart on average
    edges = np.round(np.arange(n_bins + 1) * fps / TIMELINE_HZ).astype(int)
    edges[-1] = min(edges[-1], rms.size)  # never let the last edge point past the final frame

    # Average a per-frame series inside each timeline bin; works for 1-D series and the 2-D band matrix.
    # Note: reduceat sums the last bin up to the end of the array while counts stop at edges[-1], so frames after
    # that edge inflate the last value slightly.
    def down(a: np.ndarray) -> np.ndarray:
        sums = np.add.reduceat(a, edges[:-1], axis=0)  # sum of frames from each start edge up to the next one
        counts = np.diff(edges).astype(np.float64)  # number of frames in each bin, as float for the division
        # divide to get the mean; for 2-D input the counts are broadcast over the band columns
        return sums / (counts if a.ndim == 1 else counts[:, None])

    # Scale a series to 0..1 using its 99th percentile, so a single loud spike does not flatten everything else.
    def norm(a: np.ndarray) -> np.ndarray:
        hi = np.percentile(a, 99) if a.size else 0  # robust "loud" reference level; an empty series gets 0
        # divide and clip into 0..1, or return zeros when the series is silent
        return np.clip(a / hi, 0, 1) if hi > 0 else np.zeros_like(a)

    r, o, b = down(rms), down(onset), down(bands)  # downsample level, onset and band energies to the 10 Hz grid
    # The timeline block of the signature.
    return {
        "hz": TIMELINE_HZ,  # values per second, so the page knows how to index the lists
        "rms": _q(norm(r)),  # normalised loudness envelope, scaled to the range 0..1
        "onset": _q(norm(o)),  # normalised onset (hit) strength, scaled to 0..1
        # one 0..1 list per band name, each normalised on its own
        "bands": {name: _q(norm(b[:, i])) for i, (name, _, _) in enumerate(BANDS)},
    }


# Analyse one audio file and return its signature dictionary (the sound described, never the audio itself).
# Parameter path is a str or Path to any ffmpeg-readable file. Raises SignatureError for unusable input.
# Side effects: runs ffmpeg and reads the file twice (decode and hash). Used by the CLI and the self-test.
def analyse(path: str | Path) -> dict:
    path = Path(path)  # accept plain strings from argparse as well as Path objects
    check_input(path)  # fail fast on missing, tiny or text files
    x = decode(path)  # mono float32 samples resampled to 22050 Hz
    dur = x.size / SR  # clip duration in seconds from the sample count
    peak = float(np.abs(x).max())  # largest absolute sample value, 0..1 full scale
    if peak < 1e-4:  # a peak under 0.0001 (about -80 dBFS) is effectively digital silence
        # silent input has no tempo or pitch, so refuse it explicitly
        raise SignatureError(f"{path.name} decodes to silence (peak {peak:.1e})")
    mag = stft_mag(x)  # spectrogram shared by the steps below
    bm = band_matrix(mag)  # summed power per frame in the six bands from sub (20 to 60 Hz) to air (6 to 11 kHz)
    # Per-frame level from Parseval's theorem: summed power divided by the Hann window's energy (0.375 * N_FFT).
    # The result is proportional to RMS rather than calibrated, which is fine because the timeline normalises it.
    frame_rms = np.sqrt((mag ** 2).sum(axis=1) / (N_FFT * 0.375)) / 1.0  # the trailing division by 1.0 changes nothing
    onset = onset_envelope(mag)  # spectral flux curve marking hits and note starts
    bpm, tconf, offset = estimate_tempo(onset)  # tempo, its confidence and the first-beat offset in seconds
    f0, f0conf = low_fundamental(x)  # bass pitch and confidence from the long-frame harmonic analysis
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)  # bin centre frequencies of the normal-size FFT
    avg_pow = (mag ** 2).mean(axis=0)  # average power per bin across the whole clip
    # power-weighted mean frequency in Hz: higher means a brighter sound
    centroid = float((freqs * avg_pow).sum() / (avg_pow.sum() + 1e-12))
    cum = np.cumsum(avg_pow)  # running total of power from low to high frequency
    # frequency below which 85% of the power lies (spectral rolloff)
    rolloff = float(freqs[int(np.searchsorted(cum, 0.85 * cum[-1]))])
    tot = bm.sum(axis=0)  # total energy per band over the whole clip
    shares = tot / (tot.sum() + 1e-12)  # each band's share of the total; the shares sum to about 1
    # overall RMS level in dBFS, computed in float64; 1e-12 keeps log10 away from zero
    rms_db = 20 * math.log10(float(np.sqrt((x.astype(np.float64) ** 2).mean())) + 1e-12)
    period = 60.0 / bpm if bpm > 0 else 0  # beat period in seconds, or 0 when no tempo was found
    # up to 64 predicted beat times in seconds, starting at the first-beat offset
    beats = [round(offset + k * period, 3) for k in range(min(64, int(dur / period)))] if period else []
    # Assemble the signature. Field names carry their units (_s, _hz, _db) so the page needs no extra schema.
    sig = {
        "schema": SCHEMA,  # format tag checked by consumers
        # file name, size in bytes and SHA-256 tie the signature to the exact audio file
        "source": {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)},
        "decoded": {"sr": SR, "duration_s": round(dur, 3)},  # analysis sample rate and decoded length in seconds
        # RMS and peak level in dBFS, plus crest factor (peak minus RMS) showing how punchy the sound is
        "level": {"rms_db": round(rms_db, 2), "peak_db": round(20 * math.log10(peak), 2), "crest_db": round(20 * math.log10(peak) - rms_db, 2)},
        # tempo with confidence, first beat offset and the predicted beat grid
        "tempo": {"bpm": round(bpm, 2), "confidence": round(tconf, 3), "first_beat_s": round(offset, 3), "beat_times_s": beats},
        "spectrum": {  # spectral summary block with brightness and bass pitch
            "centroid_hz": round(centroid, 1),  # spectral centroid (brightness) in Hz, one decimal place
            "rolloff85_hz": round(rolloff, 1),  # frequency below which 85% of the energy sits
            "low_fundamental_hz": round(f0, 2),  # bass fundamental frequency in Hz, rounded to hundredths
            "low_fundamental_confidence": round(f0conf, 3),  # 0..1 confidence; the probe trusts it from 0.25 upward
            # energy share of each named band, four decimals
            "band_share": {name: round(float(shares[i]), 4) for i, (name, _, _) in enumerate(BANDS)},
        },
        "timeline": timeline(frame_rms, bm, onset),  # the ten-per-second animation curves for the page
    }
    return sig  # the complete signature dictionary, ready for writing


# Save a signature dictionary as compact JSON (no spaces after separators) with a trailing newline.
# Parameters: sig is the dict from analyse(); out is the destination path. Returns the output Path.
# Overwrites any existing file; the result is small because it never contains audio samples.
def write(sig: dict, out: str | Path) -> Path:
    out = Path(out)  # accept a string path too
    # compact separators keep the file a few kilobytes even with the full timeline
    out.write_text(json.dumps(sig, separators=(",", ":")) + "\n", encoding="utf-8")
    return out  # hand back the path so the caller can print its size
