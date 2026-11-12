# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Phonemize strings exactly as the narration did (Piper's espeak-ng bridge).

Run with the speechkit runtime venv, not the system python:

    <runtime>/.venv/bin/python -I _espeak_helper.py [--voice en] [--voice en-us] < in.json > out.json

stdin: a JSON list of strings. stdout: with one --voice, a JSON list of phoneme strings in the same
order; with several, {voice: [phoneme strings]}. The default voice is "en", the espeak voice in the
pinned en_US-ljspeech-high config. Each string is phonemized with
EspeakPhonemizer(ESPEAK_DATA_DIR).phonemize(voice, text), the call PiperVoice.phonemize makes for this
voice (no vowel clusters); sentences are joined with a space and the per-codepoint lists with "".
"""
import json  # stdin carries a JSON list of strings in, stdout carries the phoneme strings out
import sys  # sys gives the raw argument list, the standard streams and the exit code


# Entry point of the helper; lexicon.espeak() and linking.py start it as a subprocess of the runtime venv.
# argv is the argument list without the script name, made only of "--voice <name>" pairs (espeak voice ids
# such as "en" or "en-us"). It reads a JSON list of strings from stdin and writes IPA phoneme strings as JSON
# to stdout: a plain list for one voice, or {voice: list} for several. Returns the process exit code: 0 on
# success, 2 on a usage error. Nothing is written to disk.
def main(argv):
    voices = []  # espeak voices requested on the command line, in the order given
    i = 0  # index of the argument currently being parsed
    # A minimal hand-written parser: the only accepted form is a series of "--voice NAME" pairs.
    while i < len(argv):
        if argv[i] == "--voice" and i + 1 < len(argv):  # a --voice flag must be followed by its value
            voices.append(argv[i + 1])  # remember the voice name that follows the flag
            i += 2  # skip both the flag and its value
        else:  # anything else is not a valid argument
            # print the usage line to stderr so stdout stays pure JSON
            sys.stderr.write("usage: _espeak_helper.py [--voice V]... < list.json\n")
            return 2  # exit code 2 tells the caller the arguments were wrong
    # With no --voice the narration voice "en" is used, the espeak voice named in the pinned ljspeech config.
    voices = voices or ["en"]
    texts = json.load(sys.stdin)  # parse the whole of stdin as one JSON document
    # Reject anything other than a list of strings before the slow piper import, so bad input fails fast.
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        sys.stderr.write("stdin must be a JSON list of strings\n")  # explain the expected input shape on stderr
        return 2  # same usage-error exit code as a bad argument

    # Imported late because piper exists only in the runtime venv; a usage error above never needs it.
    from piper.phonemize_espeak import EspeakPhonemizer  # the same espeak-ng bridge class that PiperVoice uses
    # espeak-ng data folder bundled with piper, so the dictionary matches the narration's exactly
    from piper.voice import ESPEAK_DATA_DIR

    import re  # regular expressions, needed only for the raw-phoneme block split below
    ph = EspeakPhonemizer(ESPEAK_DATA_DIR)  # one phonemizer instance reused for every string and voice
    # The capturing group makes re.split keep each [[...]] block as its own item between the plain-text parts.
    block = re.compile(r"(\[\[.*?\]\])")     # Piper raw-phoneme blocks pass through unchanged, as PiperVoice does

    # Phonemizes one string t with espeak voice v, the way PiperVoice.phonemize would. Plain text goes through
    # espeak; a [[...]] raw-phoneme block is copied through as written. Returns one IPA string with the
    # pieces separated by single spaces.
    def one(v, t):
        parts = []  # phoneme strings collected for each piece of t
        for part in block.split(t):  # alternate plain-text parts and [[...]] blocks, in their original order
            # split() leaves empty or whitespace-only gaps between blocks; they carry nothing to speak
            if not part.strip():
                continue  # skip them so no stray spaces end up in the output
            if part.startswith("[["):  # a raw-phoneme block that Piper would pass straight to the voice
                parts.append(part[2:-2].strip())  # strip the [[ and ]] brackets and keep the IPA inside unchanged
            else:  # ordinary text goes through espeak
                # phonemize returns one list per sentence, each a list of single phoneme codepoints: join the codepoints
                # with nothing and the sentences with a space, matching how the narration's phonemes were assembled.
                parts.append(" ".join("".join(sent) for sent in ph.phonemize(v, part)).strip())
        return " ".join(x for x in parts if x)  # drop empty pieces so the result never has doubled spaces

    out = {}  # voice name -> list of phoneme strings in input order
    for v in voices:  # every requested voice phonemizes the full list
        # blank strings map to "" without calling espeak, which keeps positions aligned with the input
        out[v] = [one(v, t) if t.strip() else "" for t in texts]
    # One voice gives a bare list (what lexicon.espeak() and linking.py expect), several voices a dict.
    # ensure_ascii=False writes IPA characters as UTF-8 instead of \u escapes.
    json.dump(out[voices[0]] if len(voices) == 1 else out, sys.stdout, ensure_ascii=False)
    return 0  # exit status 0 tells the calling process that stdout holds the complete JSON answer


if __name__ == "__main__":  # run main only when executed as a script, not when imported
    # pass the arguments after the script name and turn main's return value into the exit code
    sys.exit(main(sys.argv[1:]))
