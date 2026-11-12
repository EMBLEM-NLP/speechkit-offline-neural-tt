"""Fast tests for the pronunciation loop glue (no synthesis, no recognizer)."""
import json  # builds the fake WAV records and measurement data
import tempfile  # temporary folders that are removed after each test
import unittest  # standard library test framework
from pathlib import Path  # Path objects for temporary files and folders

from emblem_rsi import loop  # the generic loop: seal, decide, schema constants
from emblem_rsi.pron import cli as pcli  # pronunciation CLI helpers (changed_segments)
from emblem_rsi.pron import measure as pm  # the pronunciation measurement and its rubric path
from emblem_rsi.pron import trial  # the acoustic trial helpers under test
from speechkit_narrate.text import docmodel  # applies the loop's pronunciation overrides to the script


# Tests of the acoustic trial helpers: applying a variant to a token, the word key, sentence lookup, reference tokens
# and the summary verdict. All of them are pure functions on small made-up data.
class TrialHelpers(unittest.TestCase):
    # A variant replaces the word but keeps its punctuation, and a respelling takes the token's leading capital. Raw
    # [[phonemes]] keep their case, and the CURRENT marker leaves the token unchanged.
    def test_apply_variant_keeps_punctuation_and_capital(self):
        # capital B carried over to the respelling, comma kept
        self.assertEqual(trial.apply_variant("Boaz,", "bo-az"), "Bo-az,")
        # lowercase token stays lowercase, full stop kept
        self.assertEqual(trial.apply_variant("doorkeeper.", "door-keeper"), "door-keeper.")
        self.assertEqual(trial.apply_variant("Ana", "[[ˈɑːnə]]"), "[[ˈɑːnə]]")  # raw phonemes are never capitalized
        # the control label (current reading) changes nothing
        self.assertEqual(trial.apply_variant("Ana's", trial.CURRENT), "Ana's")

    # word_key must normalize exactly like the recognizer scoring. Apostrophes and punctuation are dropped and letters
    # lowercased.
    def test_word_key_matches_asr_normalisation(self):
        # "Ana's" becomes "anas", matching how a recognizer transcript is scored
        self.assertEqual(trial.word_key("Ana's"), "anas")
        self.assertEqual(trial.word_key("doorkeeper."), "doorkeeper")  # trailing punctuation is removed from the key

    # sentence_of returns the tokens and ids of the sentence that contains a token. Sentences end at . ! or ? and at the
    # end of the segment.
    def test_sentence_of(self):
        # two complete sentences and an unfinished last one in a single segment
        seg = {"tokens": ["One", "two.", "Three", "doorkeeper!", "Four"], "tokenIndex": [10, 11, 12, 13, 14]}
        # token 13 lies in the second sentence
        self.assertEqual(trial.sentence_of(seg, 13), (["Three", "doorkeeper!"], [12, 13]))
        # token 14 is in the trailing piece without punctuation
        self.assertEqual(trial.sentence_of(seg, 14), (["Four"], [14]))

    # The reference request uses spoken_ref (the reading before overrides) from the docmodel when one is given. Without
    # a docmodel it keeps the voiced token.
    def test_reference_request_uses_spoken_ref(self):
        # a one-token segment voiced with the respelling "Bo-az."
        req = {"segments": [{"id": "s0", "tokens": ["Bo-az."], "tokenIndex": [5], "spokenText": "Bo-az."}]}
        # docmodel token whose reference reading is "Boaz."
        dm = {"tokens": [{"i": 5, "disp": "Boaz.", "spoken": "Bo-az.", "spoken_ref": "Boaz."}]}
        # with the docmodel: the listener should hear "Boaz."
        self.assertEqual(trial.reference_request(req, dm)["segments"][0]["tokens"], ["Boaz."])
        # without it the voiced token form is used
        self.assertEqual(trial.reference_request(req, None)["segments"][0]["tokens"], ["Bo-az."])

    # summarize() adopts a variant only for a clear gain without collateral damage. Here the current reading always
    # fails and "Bo-az" always passes, three takes each.
    def test_summarize_adopts_only_clear_gains(self):
        jobs, scores = [], {}  # trial jobs and their per-model scores
        for v, ok in ((trial.CURRENT, 0), ("Bo-az", 1)):  # the control (all wrong) and the candidate (all right)
            for k in range(3):  # three takes of each variant, like a real trial
                jid = f"{v}{k}"  # job id built from variant and take, like "Bo-az0"
                # a job record with no audio file, so the formant checks are skipped
                jobs.append({"id": jid, "word": "boaz", "variant": v, "text": "x"})
                st = "ok" if ok else "sub"  # recognizer status: "ok" for the candidate, a substitution for the control
                # the same score from both recognizers; the control is heard as "bows"
                scores[jid] = {m: {"status": st, "heard": "boaz" if ok else "bows", "p": 0.9,
                                   # no errors among five other words in the sentence
                                   "other_errors": 0, "other_words": 5} for m in ("ctc", "confirm")}
        s = trial.summarize(jobs, scores)["boaz"]  # summary entry for the word "boaz"
        # best variant is Bo-az and the verdict is adopt (gain 1.0, collateral 0)
        self.assertEqual(s["_best"], {"variant": "Bo-az", "verdict": "adopt"})
        self.assertEqual(s[trial.CURRENT]["ok_rate"], 0.0)  # the control was never heard right
        self.assertEqual(s["Bo-az"]["ok_rate"], 1.0)  # the candidate was always heard right


# Tests of the measurement helpers: page hash, tracked words, confirmed occurrences and the seal check. They use tiny
# files in temporary folders.
class MeasureHelpers(unittest.TestCase):
    # page_sha256 covers the overrides file as well as the request. A new override therefore counts as a changed page
    # for the loop.
    def test_page_hash_follows_overrides(self):
        with tempfile.TemporaryDirectory() as d:  # temporary folder for the test files
            d = Path(d)  # wrap the folder name as a Path object
            (d / "request.json").write_text("{}")  # an empty JSON request file ("{}")
            a = pm.page_sha256(d / "request.json", d / "missing.json")  # hash with no overrides file present
            # an overrides file with one respelling entry
            (d / "ov.json").write_text('{"overrides": {"boaz": "Bo-az"}}')
            b = pm.page_sha256(d / "request.json", d / "ov.json")  # page hash with the overrides file present
            self.assertNotEqual(a, b)  # the page hashes with and without overrides must differ

    # words_tracked counts spoken tokens that have a non-empty time span and were scored. Punctuation-only tokens such
    # as a dash are not words at all.
    def test_words_tracked(self):
        with tempfile.TemporaryDirectory() as d:  # temporary folder for the WAV record
            d = Path(d)  # folder name as a Path for joining
            # WAV timing record: token 1 spans 0.0 to 0.4 s
            (d / "s0.wav.json").write_text(json.dumps({"words": [{"token": 1, "start": 0.0, "end": 0.4},
                                                                  # token 2 has a zero-length span, so it is not tracked
                                                                  {"token": 2, "start": 0.4, "end": 0.4}]}))
            # one segment holding two words and a dash
            req = {"segments": [{"id": "s0", "tokens": ["Boaz", "came", "—"], "tokenIndex": [1, 2, 3]}]}
            # 1 tracked out of 2 words (the dash is not counted)
            self.assertEqual(pm.words_tracked(req, d, {1: {}, 2: {}}), (1, 2))

    # confirmed_occurrences counts occurrences of confirmed words that both recognizers got wrong. A word the confirming
    # model heard right, or a word that is not confirmed, does not count.
    def test_confirmed_occurrences_needs_both_models(self):
        am = {"confirmed": [{"word": "boaz"}],  # one confirmed word: "boaz" both models fail
              # both models wrong on a confirmed word: counts
              "occurrences": {1: {"status": "sub", "expected": "boaz", "confirm": {"status": "sub"}},
                              # the confirming model heard it right: does not count
                              2: {"status": "sub", "expected": "boaz", "confirm": {"status": "ok"}},
                              # both wrong, but "ruth" is not a confirmed word: does not count
                              3: {"status": "sub", "expected": "ruth", "confirm": {"status": "sub"}}}}
        self.assertEqual(pm.confirmed_occurrences(am), 1)  # exactly one occurrence qualifies in this data

    # Measuring needs a sealed rubric. measure() must refuse in an empty run folder before doing any work.
    def test_measure_refuses_without_seal(self):
        with tempfile.TemporaryDirectory() as d:  # empty run folder without any sealed rubric
            with self.assertRaises(loop.LoopError):  # expect the loop's error about the seal
                pm.measure(Path(d))  # no rubric.sealed.json in the folder


# Decisions under the real pronunciation rubric. A fix that removes misheard words is promoted; a later change that
# raises the word error rate is vetoed.
class LoopDecisions(unittest.TestCase):
    # Fake measurement for the pronunciation rubric. wer is the recognizer word error rate, words the count of confirmed
    # misheard words (also used as the occurrence count), and page doubles as the signature hash.
    def _m(self, sealed, wer, words, page):
        # right schema and rubric hash, measured now
        return {"schema": loop.MEASURE_SCHEMA, "rubric_sha256": sealed["sha256"], "measured_at": loop.now(),
                "page_sha256": page, "signature_sha256": page,  # page and signature hashes both set to the page label
                # gate metrics at 1.0 and the given word error rate
                "metrics": {"words_tracked_ratio": 1.0, "asr_segments_coverage": 1.0, "asr_wer": wer,
                            # confirmed word and occurrence counts equal
                            "asr_confirmed_words": words, "asr_confirmed_occurrences": words,
                            # fixed dictionary values so only the recognizer metrics vary
                            "lexicon_flagged_words": 3, "lexicon_mean_distance": 0.04}}

    # Three loops: a baseline, a fix that cuts misheard words from 11 to 4 with a tiny WER change, and a change that
    # lowers misheard words further but raises WER to 9%. The third change is vetoed because the WER guard regressed.
    def test_fix_promoted_and_wer_regression_vetoed(self):
        with tempfile.TemporaryDirectory() as d:  # temporary run folder for the sealed rubric
            d = Path(d)  # run folder as a Path for sealing
            sealed = loop.seal(pm.RUBRIC, d)  # seal the pronunciation rubric (emblem_rsi/rubrics/pronunciation.json)
            hist = []  # decision history passed to each decide call
            # baseline: 2.58% WER and 11 misheard words
            e1 = loop.decide(sealed, self._m(sealed, 0.0258, 11, "a"), hist)
            self.assertEqual(e1["decision"], "promote")  # the first passing loop is promoted
            hist.append(e1)  # record the first decision in the history
            # 2.62% WER is within the guard slack, and the score rises
            e2 = loop.decide(sealed, self._m(sealed, 0.0262, 4, "b"), hist)
            self.assertEqual(e2["decision"], "promote", e2["reasons"])  # promoted; reasons shown on failure
            hist.append(e2)  # record the second decision in the history
            e3 = loop.decide(sealed, self._m(sealed, 0.090, 3, "c"), hist)    # e.g. a global phoneme switch
            self.assertEqual(e3["decision"], "veto")  # vetoed: the score falls and the WER guard got worse
            self.assertTrue(any("asr_wer" in r for r in e3["reasons"]))  # some reason names the asr_wer metric

    # changed_segments lists segments whose spoken text or token list differs from their WAV record, plus segments that
    # have no record. Those are the ones that must be voiced again.
    def test_changed_segments(self):
        with tempfile.TemporaryDirectory() as d:  # temporary speech folder with WAV records
            d = Path(d)  # speech folder as a Path for the helper
            # timing record of s0, voiced as "Boaz."
            (d / "s0.wav.json").write_text(json.dumps({"spokenText": "Boaz.", "words": [{"token": 1}]}))
            # timing record of s1, voiced as "Ruth."
            (d / "s1.wav.json").write_text(json.dumps({"spokenText": "Ruth.", "words": [{"token": 2}]}))
            # s0 now has the respelling "Bo-az.", so its text changed
            req = {"segments": [{"id": "s0", "spokenText": "Bo-az.", "tokenIndex": [1]},
                                # s1 text and token list are unchanged
                                {"id": "s1", "spokenText": "Ruth.", "tokenIndex": [2]},
                                # s2 is new and has no timing record yet
                                {"id": "s2", "spokenText": "New.", "tokenIndex": [3]}]}
            # s0 changed and s2 is missing; s1 needs nothing
            self.assertEqual(pcli.changed_segments(req, d), ["s0", "s2"])


# Tests of the pronunciation overrides as the script builder applies them (the fixes the loop writes to
# work/pronunciations.json).
class DocmodelOverrides(unittest.TestCase):
    # An override keeps the token's punctuation and leading capital, raw [[phonemes]] are used as written, a token
    # override beats the word-wide one, and spoken_ref keeps the reading before overrides.
    def test_override_keeps_punctuation_case_and_raw_phonemes(self):
        words = {"boaz": "bo-az", "ana": "[[ˈɑːnə]]"}  # one respelling and one raw-phoneme entry
        self.assertEqual(docmodel.apply_override("Boaz,", 0, words, {}), "Bo-az,")  # capitalized, comma kept
        self.assertEqual(docmodel.apply_override("Ana", 0, words, {}), "[[ˈɑːnə]]")  # raw phonemes unchanged
        self.assertEqual(docmodel.apply_override("Ruth.", 0, words, {}), "Ruth.")  # no override: unchanged
        self.assertEqual(docmodel.apply_override("Boaz", 7, words, {7: "bo-ahz"}), "Bo-ahz")  # this occurrence only
        flat = docmodel.tokenize([{"kind": "p", "text": "Ruth and Boaz —"}], {}, words, {})  # ends with a dash
        # the dash is silent and its pause becomes a comma after the overridden word
        self.assertEqual([t["spoken"] for t in flat], ["Ruth", "and", "Bo-az,", ""])
        # spoken_ref keeps the original word with the same comma
        self.assertEqual([t["spoken_ref"] for t in flat], ["Ruth", "and", "Boaz,", ""])


if __name__ == "__main__":  # run the tests when the file is executed directly
    unittest.main()  # unittest finds the test classes in this module
