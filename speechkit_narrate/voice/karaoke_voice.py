# SPDX-License-Identifier: GPL-3.0-or-later
"""Karaoke narration driver for speechkit-offline-neural-tt.

Runs inside the installed speechkit runtime venv. It reuses speechkit's own adapter
(voice.py: validate_script, doctor, sha, save) and reproduces its synthesis loop exactly
(sentence split, 0.38 s sentence pauses, profile lengthScale with the 155 wpm pace retry,
volume 0.8, signal gate). The only addition is per-word timing: the pinned voice is loaded
from a verified derived graph with one extra output exposing phoneme durations. Token
mapping can be approximate; alignmentQuality and word estimated flags expose that fact.

usage: python karaoke_voice.py --runtime R --profile P --aligned-model M --script REQ --out DIR
                               --worker k/n [--claims DIR]

With --claims the n workers of one run share the segments (see claims.py): each takes the next free one, longest first.
Without it worker k voices every n-th segment starting at k, which is what the pronunciation loop's retake command uses.
"""
# standard modules: options, phoneme sequence matching, loading voice.py by path, JSON, regex, stderr, WAV
import argparse, difflib, importlib.util, json, re, sys, wave
from pathlib import Path  # Path objects for the runtime folder, the output folder and every file read or written

ap = argparse.ArgumentParser()  # command-line parser; narrate.run_workers supplies every option
# runtime folder, profile JSON, aligned ONNX model, request file, output folder, speechkit source folder
for a in ('--runtime', '--profile', '--aligned-model', '--script', '--out', '--speechkit-src'):
    ap.add_argument(a, required=True)  # all six are mandatory
ap.add_argument('--worker', default='0/1')  # "k/n": this process is worker k of n (default: the only worker)
# folder shared by the n workers of one run, in which they mark the segments they take; empty means fixed shares
ap.add_argument('--claims', default='')
args = ap.parse_args()  # read the options from sys.argv; a missing required one exits with a usage error

# speechkit's voice.py is a plain file in the checkout, not an installed package, so it is loaded from its
# path under the module name "sk_voice" to reuse its validation, preflight, hashing and saving helpers.
spec = importlib.util.spec_from_file_location('sk_voice', Path(args.speechkit_src) / 'voice.py')
# create the module object and run the file's code into it
sk = importlib.util.module_from_spec(spec); spec.loader.exec_module(sk)

# numpy and onnxruntime exist only inside the speechkit runtime venv, hence imported after the setup above
import numpy as np, onnxruntime as ort
from piper import PiperVoice, SynthesisConfig  # the Piper voice wrapper and its per-call synthesis settings
from piper.config import PiperConfig  # the voice's JSON configuration (phoneme map, sample rate, espeak language)

# runtime root, output folder for WAVs and records (created if missing)
runtime = Path(args.runtime); out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
# the voice profile: model paths, pinned hashes, lengthScale, pauses, pace limit
profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
# the request (work/request.json), checked by speechkit: schema 1.0.0, unique ids, non-empty text
script = sk.validate_script(json.loads(Path(args.script).read_text(encoding="utf-8")))
# speechkit's preflight: exact piper-tts and onnxruntime versions, SHA-256 of the pinned model and config, and
# safe pacing values; it raises on any mismatch and returns the engine record kept for the summary line.
preflight = sk.doctor(runtime, profile)                       # pinned model + deps verified
from identity import synthesis_identity, validate_record, seal_record
identity = synthesis_identity(runtime, args.profile, args.aligned_model, args.speechkit_src,
                              renderer_path=__file__)
k, n = map(int, args.worker.split('/'))  # worker number and worker count
# one ONNX thread per process when several workers run side by side, two (as speechkit uses) when alone
opts = ort.SessionOptions(); opts.intra_op_num_threads = 1 if n > 1 else 2; opts.inter_op_num_threads = 1
# load the aligned copy of the model (same weights plus the per-phoneme duration output) on the CPU
session = ort.InferenceSession(args.aligned_model, sess_options=opts, providers=['CPUExecutionProvider'])
# read the voice config through sk.inside, which refuses paths outside the runtime folder
cfg = PiperConfig.from_dict(json.loads(sk.inside(runtime, profile['config']).read_text(encoding="utf-8")))
# the config must use the profile's sample rate (22050 Hz for ljspeech-high)
if cfg.sample_rate != profile['sampleRate']: raise ValueError('Native sample rate mismatch')
# Experiment hook for the pronunciation loop: phonemize with another espeak language (pinned files unchanged).
import os  # used only for the environment variable below
if os.environ.get('KARAOKE_ESPEAK_VOICE'):  # set by the pronunciation experiments to try a different espeak language
    # phonemize with that espeak voice instead; the pinned files stay untouched
    cfg.espeak_voice = os.environ['KARAOKE_ESPEAK_VOICE']
# sample rate in Hz, and the Piper voice built on the aligned session
rate = cfg.sample_rate; voice = PiperVoice(session=session, config=cfg)
PUNCT = set(',.;:!?—–-"\'()…¡¿')  # punctuation that the phonemizer passes through as pause symbols rather than sounds
SKIP = {'^', '$', '_'}  # Piper's begin (^), end ($) and padding (_) symbols, which carry no word sound


STRESS = set('ˈˌ')  # IPA primary and secondary stress marks; they are timing markers, not sounds of their own
_ph_cache = {}  # token text (or (token, next token)) -> phoneme characters, memoised because the same words repeat


# Returns the list of phoneme characters a token produces when phonemized on its own, without stress marks,
# spaces or punctuation. `text` is one spoken token such as "Psalm" or "verse,". These are the expected sounds
# that assign_words looks for in the sentence's actual phoneme stream. Results are cached.
def token_phones(text):
    """Phoneme characters a token produces on its own (no stress, spaces or punctuation)."""
    if text not in _ph_cache:  # this token text has not been phonemized yet in this process
        # phonemize returns one list of phoneme strings per sentence; flatten them into characters
        _ph_cache[text] = [c for sent in voice.phonemize(text) for c in ''.join(sent)
                           if c != ' ' and c not in PUNCT and c not in STRESS]  # keep only real sounds
    return _ph_cache[text]  # the cached phoneme list


# Converts the alignments Piper reports for one sentence piece into [phoneme, start, end] entries, sample
# positions within the segment. `chunks` are the AudioChunk objects of the piece and `base` the sample position
# where the piece starts. A stress mark's time is folded into the phoneme after it. Raises ValueError when Piper
# returned no alignments (the model lacks the duration output or the ids did not line up).
def piece_phones(chunks, base):
    """(phoneme, start, end) for every voiced phoneme of one sentence piece; stress marks fold into the next phoneme."""
    seq = []; pos = base  # result list, and the running sample position
    for ch in chunks:  # each audio chunk Piper produced
        if not ch.phoneme_alignments:  # alignments missing for this chunk
            raise ValueError('alignment unavailable')  # word timing would be impossible, so fail the segment
        pending = None  # start position of stress marks waiting for their phoneme
        for al in ch.phoneme_alignments:  # each phoneme with its sample count
            ph = al.phoneme; dur = int(al.num_samples)  # the symbol and how many audio samples it occupies
            if ph in STRESS:  # a stress mark, which takes time but is not a sound of its own
                pending = pos if pending is None else pending  # remember where the first of them started
            elif ph == ' ' or ph in SKIP or ph in PUNCT:  # a space, a Piper control symbol or punctuation
                pending = None  # belongs to no phoneme, so forget any waiting stress
            else:  # an actual phoneme that the token mapping can use
                # record it, starting at a preceding stress mark if there was one
                seq.append([ph, pos if pending is None else pending, pos + dur]); pending = None
            pos += dur  # advance past this symbol's samples
    return seq  # the piece's phonemes in time order


# The expected phonemes of the token at position t of the segment, looked up in its sentence. A one-letter word is
# sounded out with the word after it, because espeak reads a lone "A" as the letter name (eɪ) but the article inside a
# sentence (ɐ): sounded alone, the article's real sound matched nothing, so it got no time of its own and its
# highlight collapsed onto the next word. The part before the first word break of "<token> <next>" is the token's own
# sound; when espeak joins the two words with no break (as in "I am"), the token is sounded alone as before.
def token_phones_at(t):
    tok = tokens[t]
    nxt = tokens[t + 1] if t + 1 < len(tokens) else None  # the next word of the segment, if any
    letters = re.sub(r"[^A-Za-z]", "", tok)
    if len(letters) != 1 or nxt is None or re.search(r"[.!?]$", tok):  # not a one-letter word inside a sentence
        return token_phones(tok)
    key = (tok, nxt)
    if key not in _ph_cache:
        flat = ''.join(c for sent in voice.phonemize(f"{tok} {nxt}") for c in ''.join(sent))
        head = flat.split(' ', 1)[0] if ' ' in flat else ''  # the token's sound up to the first word break
        ph = [c for c in head if c not in PUNCT and c not in STRESS]
        _ph_cache[key] = ph or token_phones(tok)
    return _ph_cache[key]


# Maps the actual phonemes of one sentence piece onto its tokens. `tids` are the positions (in the segment's
# token list) of the piece's tokens, `piece_seq` the piece_phones output, `start` and `end` the piece's sample
# range. Returns ({token position: [start, end] in samples}, match quality 0..1). Every token gets a span, in
# order; a token whose sounds were not found gets a short slot between its neighbours.
def assign_words(tids, piece_seq, start, end):
    """Align per-token phonemes to the piece's phoneme stream -> {token: [start, end]} in samples."""
    tchars = []; tlab = []  # expected phoneme characters and, for each, the token it belongs to
    for t in tids:  # each token of the piece
        # its phonemes alone (from the segment's `tokens`, a global set in the main loop), appended and labelled
        ph = token_phones_at(t); tchars += ph; tlab += [t] * len(ph)
    pchars = [p[0] for p in piece_seq]  # the actual phoneme characters, in order
    lab = [None] * len(pchars)  # token label for each actual phoneme, unknown at first
    # difflib alignment of expected against actual; autojunk off because phoneme letters repeat a lot
    sm = difflib.SequenceMatcher(None, tchars, pchars, autojunk=False)
    matched = 0  # number of phonemes matched
    for a, b, size in sm.get_matching_blocks():  # each run of identical phonemes in both sequences
        # the actual phonemes in the run take the token labels of the expected ones
        for q in range(size): lab[b + q] = tlab[a + q]
        matched += size  # add the run length to the count behind the match quality
    # unmatched piece phonemes inherit the previous label (or next for a leading run)
    last = None  # the latest token label seen, copied onto unmatched phonemes after it
    for i in range(len(lab)):  # forward pass over the actual phonemes
        if lab[i] is None: lab[i] = last  # an unmatched phoneme belongs to the token before it
        else: last = lab[i]  # remember the latest label
    nxt = None  # the nearest later label, for unmatched phonemes before the first match
    for i in range(len(lab) - 1, -1, -1):  # walk the phonemes from last to first
        if lab[i] is None: lab[i] = nxt  # unmatched phonemes at the very start belong to the first labelled token
        else: nxt = lab[i]  # remember the next label
    spans = {}  # token position -> [first sample, last sample] of its phonemes
    for (ph, a, b), t in zip(piece_seq, lab):  # each actual phoneme with its token
        if t is None: continue  # only when nothing matched at all
        if t not in spans: spans[t] = [a, b]  # the token's first phoneme opens its span
        else: spans[t][1] = b  # later phonemes extend its end
    # enforce order; tokens with no phonemes get squeezed between neighbours
    # result, and the end of the previous token (the first phoneme's start initially)
    out = {}; estimated = []; prev_end = piece_seq[0][1] if piece_seq else start
    for idx, t in enumerate(tids):  # each token in reading order
        # it has sounds, starting no earlier than the previous token ended (1 sample tolerance)
        if t in spans and spans[t][0] >= prev_end - 1:
            out[t] = spans[t]  # the token keeps the span its matched phonemes gave it
        else:  # no sounds of its own, or out of order
            # start of the next token that does have a valid span, else the piece end
            nxt_start = next((spans[u][0] for u in tids[idx + 1:] if u in spans and spans[u][0] >= prev_end), end)
            # a flagged estimated slot clamped to the actual piece; zero length when no space remains
            a = min(end, max(start, prev_end))
            out[t] = [a, min(end, max(a, min(nxt_start, a + (nxt_start - a) // 2 + 1)))]
            estimated.append(t)
        prev_end = out[t][1]  # the next token must start after this one
    # spans, and matched phonemes divided by the longer sequence length
    return out, matched / max(len(tchars), len(pchars), 1), estimated


# totals across this worker: pieces voiced, pieces under 80% match, the lowest match seen
stats = {'pieces': 0, 'lowMatch': 0, 'minMatch': 1.0}
if args.claims and n > 1:  # workers that run side by side take the next free segment, so none is left waiting
    # claims.py sits beside this script; it is loaded by path, like speechkit's voice.py above
    cspec = importlib.util.spec_from_file_location('narrate_claims', Path(__file__).with_name('claims.py'))
    claims = importlib.util.module_from_spec(cspec)  # an empty module for the file's contents
    cspec.loader.exec_module(claims)  # run claims.py inside it, which defines take_in_turns
    # asks for one segment at a time, as the loop pulls
    segments = claims.take_in_turns(script['segments'], args.claims)
else:  # alone, or called without a shared folder
    segments = script['segments'][k::n]  # this worker's share: every n-th segment starting at k
for s in segments:  # each segment to voice
    wav_path = out / (s['id'] + '.wav'); rec_path = out / (s['id'] + '.wav.json')  # output WAV and its JSON record
    spoken = s.get('spokenText') or s['text']  # the text the voice reads (the display text if no spoken form was given)
    if rec_path.exists():
        try:
            previous = json.loads(rec_path.read_text(encoding="utf-8"))
            valid = not validate_record(previous, wav_path, dict(s, spokenText=spoken), identity)
        except (OSError, ValueError):
            valid = False
        if valid:
            continue
        rec_path.unlink()  # an incomplete or changed artifact is never treated as completed
    # the spoken token strings; joined with spaces they must give the spoken text exactly
    tokens = s['tokens']; assert ' '.join(tokens) == spoken
    # same sentence split as speechkit voice.py, tracked per token
    piece_tokens = [[]]  # token positions grouped by sentence piece
    for ti, t in enumerate(tokens):  # each token with its position
        piece_tokens[-1].append(ti)  # it belongs to the current piece
        # a token ending in . ! or ? closes the piece, unless it is the last token
        if re.search(r'[.!?]$', t) and ti + 1 < len(tokens): piece_tokens.append([])
    pieces = [' '.join(tokens[i] for i in pt) for pt in piece_tokens]  # the text of each piece
    # must equal speechkit's own sentence split, so the audio matches what speechkit would make
    assert pieces == re.split(r'(?<=[.!?])\s+', spoken), (pieces, s['id'])
    # start at the profile's lengthScale (1.24 for ljspeech-high); attempts log; pace flag
    scale = profile['lengthScale']; attempts = []; pace_review = None
    while True:  # retry slower until the pace is acceptable
        # per attempt: audio pieces, raw speech samples, position in samples, per-token spans
        buffers = []; raw_count = 0; pos = 0; words = [None] * len(tokens)
        quality_pieces = []; estimated_words = set()
        for pi, text in enumerate(pieces):  # voice each sentence piece separately, as speechkit does
            if pi:  # not the first piece
                # 0.38 s of silence between sentences, appended, with the position moved past it
                gap = np.zeros(round(profile['sentencePauseSeconds'] * rate), dtype='<i2'); buffers.append(gap); pos += len(gap)
            # synthesize at the current scale and volume 0.8 (as speechkit does), with phoneme alignments
            chunks = list(voice.synthesize(text, SynthesisConfig(length_scale=scale, volume=.8), include_alignments=True))
            # Piper returned no audio for the piece, so the segment fails
            if not chunks: raise ValueError('Neural model returned no waveform')
            start = pos  # sample position where this piece's audio begins
            for chunk in chunks:  # each audio chunk Piper yielded for this piece
                # its 16-bit samples, copied out of the read-only buffer and appended
                samples = np.frombuffer(chunk.audio_int16_bytes, dtype='<i2').copy(); buffers.append(samples)
                raw_count += len(samples); pos += len(samples)  # count the speech samples and advance the position
            seq = piece_phones(chunks, start)  # the piece's phoneme timings
            spans, q, estimated = assign_words(piece_tokens[pi], seq, start, pos)
            for t, sp in spans.items(): words[t] = sp
            estimated_words.update(estimated)
            aligned_samples = sum(int(al.num_samples) for ch in chunks for al in ch.phoneme_alignments)
            quality_pieces.append({"tokenIndices": [s['tokenIndex'][t] for t in piece_tokens[pi]],
                                   "matchRatio": q,
                                   "fallbackTokenIndices": [s['tokenIndex'][t] for t in estimated],
                                   "audioSamples": pos - start, "alignmentSamples": aligned_samples,
                                   "alignmentSampleMismatch": aligned_samples != pos - start})
            # count the piece and track the lowest match quality
            stats['pieces'] += 1; stats['minMatch'] = min(stats['minMatch'], round(q, 3))
            if q < 0.8: stats['lowMatch'] += 1  # a match under 80% is worth noticing
        # the segment audio, its length in seconds, and the speaking rate in words per minute
        speech = np.concatenate(buffers); duration = len(speech) / rate; wpm = len(spoken.split()) * 60 / duration
        # keep a record of this attempt
        attempts.append({'lengthScale': scale, 'durationSeconds': duration, 'deliveryWpm': wpm})
        if wpm <= profile['maxDeliveryWpm']: break  # at or below the 155 wpm limit: accept
        if scale >= 1.399:  # speechkit would stop here; keep the slowest take and flag it for review
            pace_review = 'REQUIRED'; break  # mark the segment for a human pace review and keep this take
        scale = min(1.4, scale + .06)  # slow down by 0.06, up to 1.4
    # peak sample (int32 so abs(-32768) does not overflow) and RMS on a 0..1 scale
    peak = int(np.max(np.abs(speech.astype(np.int32)))); rms = float(np.sqrt(np.mean((speech.astype(float) / 32768) ** 2)))
    # speechkit's gate: no audio, quieter than 0.002 RMS (about -54 dBFS), or over 0.1% of samples clipped
    if not len(speech) or rms < .002 or np.count_nonzero(np.abs(speech.astype(np.int32)) >= 32760) > len(speech) * .001:
        raise ValueError('Speech signal gate failed')  # refuse a broken take
    tmp = wav_path.with_suffix('.wav.tmp')  # write to a temporary file first
    with wave.open(str(tmp), 'wb') as w:  # 16-bit PCM WAV writer
        # mono, 2 bytes, the voice's rate, uncompressed; then all samples
        w.setparams((1, 2, rate, 0, 'NONE', 'not compressed')); w.writeframes(speech.tobytes())
    tmp.replace(wav_path)  # rename into place so a crash never leaves a half-written WAV under the real name
    # the record: ids, texts, file name and its SHA-256
    row = {'id': s['id'], 'text': s['text'], 'spokenText': spoken, 'path': wav_path.name, 'sha256': sk.sha(wav_path),
           # sample rate, sample count, duration in seconds, delivered words per minute
           'sampleRate': rate, 'samples': len(speech), 'durationSeconds': duration, 'deliveryWpm': wpm,
           # rate over speech only (pauses excluded), loudness, peak, and the attempts made
           'rawChunkWpm': len(spoken.split()) * 60 / (raw_count / rate), 'rms': rms, 'peak': peak, 'attempts': attempts,
           # per token: its docmodel token index and start/end converted from samples to seconds
           'words': [{'token': s['tokenIndex'][i], 'start': a / rate, 'end': b / rate,
                      'estimated': i in estimated_words} for i, (a, b) in enumerate(words)],
           # pronunciation approval starts PENDING; paceReview is REQUIRED only when the pace retries ran out
           'pronunciationApproval': 'PENDING', 'paceReview': pace_review}
    # how the word timings were obtained
    row['alignment'] = 'piper phoneme durations mapped to tokens; estimated spans explicitly flagged'
    row['alignmentQuality'] = {
        'schemaVersion': 'speechkit-alignment-quality-v1', 'pieces': quality_pieces,
        'minimumMatchRatio': min((p['matchRatio'] for p in quality_pieces), default=0.0),
        'fallbackTokenIndices': [s['tokenIndex'][i] for i in sorted(estimated_words)],
        'reviewRequired': any(p['matchRatio'] < .8 or p['fallbackTokenIndices']
                              or p['alignmentSampleMismatch'] for p in quality_pieces),
        'approval': 'PENDING',
    }
    row = seal_record(row, wav_path, identity)
    sk.save(rec_path, row)  # speechkit's atomic JSON save; its presence marks the segment as done
    print(s['id'], f'{duration:.1f}s', f'{wpm:.0f}wpm', stats, file=sys.stderr, flush=True)  # progress line on stderr
# final summary for narrate.py on stdout
print(json.dumps({'worker': args.worker, 'stats': stats, 'engine': preflight}), flush=True)
