"""Unit tests for the listening probe. Hand-made transcripts only, so no model or audio is needed."""
import unittest  # standard-library test framework; run with python -m unittest tests.narration.test_pron_asr

# the module under test: normalisation, word matching, alignment scoring, flagging, BPE grouping
from emblem_rsi.pron import asr

# Short alias for the text normaliser, because NormalizeTests calls it dozens of times.
# normalize_expected(token) returns the lowercase words a listener should hear for one spoken token.
N = asr.normalize_expected


# Builds a fake TTS request dict, the same shape as work/request.json that the probe normally reads.
# Each argument is a (segment id, list of spoken tokens) pair; any number of segments may be passed.
# Token ids are numbered across the whole request starting at 0, exactly as the real docmodel numbers them.
# Returns {"segments": [...]}, which asr.score() walks segment by segment. No files are written.
def req(*segs):
    """segs: (id, [spoken tokens]); token ids are numbered globally from 0."""
    out, tid = [], 0  # out collects segment dicts; tid is the next free global token id
    for sid, toks in segs:  # one fake segment per (id, tokens) pair given by the caller
        # text and spokenText are the tokens joined by spaces; tokens keeps the list as the narrator received it
        out.append({"id": sid, "text": " ".join(toks), "spokenText": " ".join(toks), "tokens": list(toks),
                    # tokenIndex gives each token its global id so score() can key occurrences by token
                    "tokenIndex": list(range(tid, tid + len(toks)))})
        tid += len(toks)  # advance the running id so the next segment continues the numbering
    return {"segments": out}  # wrap in the top-level shape score() expects


# Builds a fake transcript as asr.transcribe() would return it for one segment.
# text is what the recogniser "heard"; each space-separated word becomes a word dict with no timing.
# start/end are None because these tests never look at timestamps, only at word identity.
def tr(text):
    # words list mirrors the real format: {"w": word, "start": s, "end": s}
    return {"text": text, "words": [{"w": w, "start": None, "end": None} for w in text.split()]}


# Tests for normalize_expected(): how one script token becomes the words a listener should hear.
# The probe compares these words with what speech recognition wrote, so every rule here prevents a false error.
class NormalizeTests(unittest.TestCase):
    # Case, edge punctuation and quote marks must vanish, because recognisers output bare lowercase words.
    def test_plain_words_lowercase_and_punctuation(self):
        self.assertEqual(N("Doorkeeper."), ["doorkeeper"])  # trailing full stop stripped and capital lowered
        # a comma glued to the word is removed, since recognisers never write punctuation
        self.assertEqual(N("faith,"), ["faith"])
        self.assertEqual(N("“feel"), ["feel"])  # a curly opening quote is in the drop table, so it disappears
        self.assertEqual(N("it.”"), ["it"])  # full stop and closing curly quote both removed
        self.assertEqual(N("one's"), ["ones"])  # apostrophes are dropped, giving the form a recogniser writes ("ones")
        self.assertEqual(N("—"), [])  # an em dash alone is a word separator, so nothing at all is heard

    # Digits are spelled out the way an American English voice reads them.
    def test_numbers(self):
        # thousands separator removed, then num2words spells it; the comma num2words adds is ignored
        self.assertEqual(N("4,200"), ["four", "thousand", "two", "hundred"])
        # the sentence-final dot is stripped before the number pattern runs
        self.assertEqual(N("4,200."), ["four", "thousand", "two", "hundred"])
        # another grouped thousand, to confirm the comma handling is general
        self.assertEqual(N("1,600."), ["one", "thousand", "six", "hundred"])
        # num2words writes "twenty-one"; the hyphen splits it into two words
        self.assertEqual(N("21"), ["twenty", "one"])
        self.assertEqual(N("0."), ["zero"])  # "0." loses its dot and becomes the single word zero
        # "and" is removed: an en-US voice says "four hundred fifty", not "four hundred and fifty"
        self.assertEqual(N("450"), ["four", "hundred", "fifty"])
        self.assertEqual(N("10th"), ["tenth"])  # ordinal suffix produces the ordinal word
        # currency symbols are blanked, so the voice is expected to say just the amount
        self.assertEqual(N("$700"), ["seven", "hundred"])
        self.assertEqual(N("2.5"), ["two", "point", "five"])  # a decimal fraction is read digit by digit after "point"

    # Hyphens and slashes split compounds, and each piece is normalised on its own.
    def test_hyphens_and_compound_tokens(self):
        self.assertEqual(N("21-day"), ["twenty", "one", "day"])  # number then word: "twenty one day"
        self.assertEqual(N("day-21"), ["day", "twenty", "one"])  # word then number, the reverse order
        # a hyphenated compound becomes two lowercase words
        self.assertEqual(N("Self-punishment"), ["self", "punishment"])
        self.assertEqual(N("twenty-four-hour"), ["twenty", "four", "hour"])  # two hyphens give three words
        # a multi-word token (a Bible reference) normalises every space-separated part
        self.assertEqual(N("23 verse 1,"), ["twenty", "three", "verse", "one"])

    # Dotted letter abbreviations and accented letters.
    def test_abbreviations_and_accents(self):
        # "p.m.," loses the edge comma and dot, then the letter pattern spells it as single letters
        self.assertEqual(N("p.m.,"), ["p", "m"])
        # the same abbreviation without the trailing comma gives the same letters
        self.assertEqual(N("p.m."), ["p", "m"])
        self.assertEqual(N("11"), ["eleven"])  # a plain two-digit number, for contrast with the letter case above
        self.assertEqual(N("Lúcia,"), ["lucia"])  # NFKD splits the accent off and the combining mark is dropped
        # both accents removed, so the expected word matches unaccented ASR output
        self.assertEqual(N("résumé,"), ["resume"])


# Tests for match_kind(): how close a heard word must be to count as the expected word.
# EXACT means identical, NEAR means same CMU pronunciation or spelling similarity of at least 0.8, NONE otherwise.
class MatchTests(unittest.TestCase):
    # Spelling variants that sound the same must be accepted as NEAR, never as errors.
    def test_near_spellings(self):
        # same CMU pronunciation (needs cmudict installed); difflib alone scores this pair only 0.75
        self.assertEqual(asr.match_kind("grey", "gray"), asr.NEAR)
        # also matched through CMUdict, since the lengths differ a lot
        self.assertEqual(asr.match_kind("ok", "okay"), asr.NEAR)
        # British and American spelling; difflib ratio is about 0.86, above the 0.8 threshold
        self.assertEqual(asr.match_kind("theater", "theatre"), asr.NEAR)
        # a recogniser's odd spelling of a real word passes on spelling similarity (about 0.89)
        self.assertEqual(asr.match_kind("brothels", "brothelles"), asr.NEAR)
        self.assertEqual(asr.match_kind("faith", "faith"), asr.EXACT)  # identical strings short-circuit to EXACT

    # Genuinely different words must stay NONE, or real mispronunciations would be hidden.
    def test_different_words(self):
        self.assertEqual(asr.match_kind("faith", "spith"), asr.NONE)  # same length but only "ith" in common: ratio 0.6
        # the length check alone rejects this (2*4/14 is under 0.8)
        self.assertEqual(asr.match_kind("doorkeeper", "dork"), asr.NONE)
        # different CMU pronunciations (final Z vs S) and ratio 0.75, so not a near match
        self.assertEqual(asr.match_kind("ones", "once"), asr.NONE)


# Tests for score(): alignment of expected words to heard words and the counts derived from it.
# Each test builds a request with req() and a transcript with tr(), then inspects totals, per_word and occurrences.
class ScoreTests(unittest.TestCase):
    # A perfect transcript must give zero word error rate and every occurrence "ok".
    def test_all_heard(self):
        # one segment of five tokens; token ids 0 to 4
        r = req(("s1", ["Desire", "is", "a", "terrible", "doorkeeper."]))
        # the recogniser heard every word, already lowercased and without punctuation
        s = asr.score(r, {"s1": tr("desire is a terrible doorkeeper")})
        self.assertEqual(s["totals"]["wer"], 0.0)  # WER = (substitutions + deletions + insertions) / expected words = 0
        self.assertEqual(s["totals"]["words_scored"], 5)  # five expected words were scored
        # every token occurrence has status ok
        self.assertEqual({o["status"] for o in s["occurrences"].values()}, {"ok"})
        # The per-word record for "doorkeeper": seen once, heard right once, no near match, heard exactly as itself,
        # in segment s1, so its hit rate is 1.0.
        self.assertEqual(s["per_word"]["doorkeeper"], {"count": 1, "ok": 1, "near": 0, "heard": {"doorkeeper": 1},
                                                       # rest of the expected per-word record
                                                       "segs": ["s1"], "rate": 1.0})

    # A word heard as two non-words must be a substitution plus an insertion, not a split match.
    def test_substitution_with_split_word(self):
        r = req(("s1", ["Desire", "is", "a", "terrible", "doorkeeper."]))  # same script as before
        # the last word comes back broken into "dork eeper"
        s = asr.score(r, {"s1": tr("desire is a terrible dork eeper")})
        o = s["occurrences"][4]  # token 4 is "doorkeeper." (the fifth token, numbered from 0)
        # the insertion next to the substitution is reported together with it as what was heard
        self.assertEqual((o["status"], o["heard"], o["seg"]), ("sub", "dork eeper", "s1"))
        # per_word keeps the two-word variant as one heard form
        self.assertEqual(s["per_word"]["doorkeeper"]["heard"], {"dork eeper": 1})
        self.assertEqual(s["per_word"]["doorkeeper"]["rate"], 0.0)  # no occurrence of the word was heard right
        # "dork" is aligned as an insertion and "eeper" as the substitution; merge_kind refuses the inexact join
        self.assertEqual(s["totals"]["sub"] + s["totals"]["ins"], 2)

    # A word heard as two real words that join back to it exactly counts as correct.
    def test_split_into_real_words_is_ok(self):
        r = req(("s1", ["a", "faithful", "doorkeeper."]))  # three tokens; "doorkeeper." is token 2
        s = asr.score(r, {"s1": tr("a faithful door keeper")})  # the recogniser wrote it as two dictionary words
        # merge alignment ("door"+"keeper" == "doorkeeper") gives status ok
        self.assertEqual(s["occurrences"][2]["status"], "ok")
        self.assertEqual(s["occurrences"][2]["heard"], "door keeper")  # the heard form records both pieces
        self.assertEqual(s["totals"]["wer"], 0.0)  # no error counted, since the merge cost is not an error

    # A missing word must be a deletion and must count in WER.
    def test_deletion(self):
        # one segment of six tokens numbered 0 to 5, so "associates" is token 2
        r = req(("s1", ["The", "brain", "associates", "place", "and", "gesture."]))
        s = asr.score(r, {"s1": tr("the brain place and gesture")})  # "associates" was not heard
        self.assertEqual(s["occurrences"][2]["status"], "del")  # token 2 ("associates") is a deletion
        # deletions are shown with the DEL marker "<del>" as the heard text
        self.assertEqual(s["occurrences"][2]["heard"], asr.DEL)
        self.assertAlmostEqual(s["totals"]["wer"], 1 / 6, places=3)  # one error out of six expected words
        self.assertEqual(s["totals"]["del"], 1)  # exactly one deletion in the totals

    # Near matches count as correct but are tracked separately so a person can review them.
    def test_near_match_counts_as_ok(self):
        r = req(("s1", ["the", "grey", "theater"]))  # both "grey" and "theater" have accepted spelling variants
        s = asr.score(r, {"s1": tr("the gray theatre")})  # the recogniser used the other spellings
        # all three tokens are ok
        self.assertEqual([s["occurrences"][i]["status"] for i in range(3)], ["ok", "ok", "ok"])
        self.assertTrue(s["occurrences"][1].get("near"))  # occurrence 1 ("grey") carries the near flag
        self.assertEqual(s["totals"]["near_matches"], 2)  # two words matched only by near spelling or pronunciation
        self.assertEqual(s["totals"]["wer"], 0.0)  # near matches do not raise WER

    # Numbers, abbreviations and hyphenated tokens must be tolerated in either written or spoken form.
    def test_numbers_and_pm_tolerance(self):
        # six tokens: at, 11, p.m., the, 4,200, 21-day (ids 0 to 5)
        r = req(("s1", ["at", "11", "p.m.,", "the", "4,200", "21-day"]))
        # the transcript mixes digits, a squashed "pm" and a hyphenated number word
        s = asr.score(r, {"s1": tr("at 11 pm the four thousand two hundred twenty-one day")})
        # everything aligns: digits normalise identically, "p m" merges with "pm", the hyphen splits
        self.assertEqual({o["status"] for o in s["occurrences"].values()}, {"ok"})
        self.assertEqual(s["occurrences"][2]["heard"], "pm")  # token 2 (p.m.,) was heard as the single word "pm"
        self.assertEqual(s["totals"]["wer"], 0.0)  # no errors at all

    # When a token expands to several words and only some are heard, the whole token is a substitution.
    def test_multi_word_token_partly_wrong_is_sub(self):
        r = req(("s1", ["day", "4,200"]))  # "4,200" expands to four thousand two hundred (four expected words)
        s = asr.score(r, {"s1": tr("day four thousand two")})  # "hundred" is missing at the end
        # token 1 mixes ok words and a deleted word, so its status becomes sub
        self.assertEqual(s["occurrences"][1]["status"], "sub")
        # per_word for the missing word records one deletion
        self.assertEqual(s["per_word"]["hundred"]["heard"], {asr.DEL: 1})

    # A segment with no transcript is skipped rather than scored as all deletions.
    def test_missing_transcript_is_skipped(self):
        r = req(("s1", ["one", "word"]), ("s2", ["two", "words"]))  # two segments, token ids 0-1 in s1 and 2-3 in s2
        s = asr.score(r, {"s1": tr("one word")})  # only s1 has a transcript
        self.assertEqual(s["totals"]["segments_scored"], 1)  # one segment was scored
        self.assertEqual(s["totals"]["segments_total"], 2)  # but the request had two
        self.assertNotIn(2, s["occurrences"])  # token 2 belongs to the skipped segment and gets no occurrence

    # A transcript whose words list is empty falls back to splitting its text field.
    def test_words_without_word_list_fall_back_to_text(self):
        # two ordinary words that need no normalising, so only the text fallback is exercised
        r = req(("s1", ["hello", "there"]))
        # words is an empty list, as some models return; text still holds the transcript
        s = asr.score(r, {"s1": {"text": "hello there", "words": []}})
        self.assertEqual(s["totals"]["wer"], 0.0)  # the fallback in _word_dicts() makes this a perfect match


# Tests for flag() and combine_confirm(): which words are reported as mispronounced.
class FlagTests(unittest.TestCase):
    # Builds a per_word record by hand: count occurrences, ok of them heard right, and the derived rate.
    # The other fields (near, heard, segs) carry neutral values because flag() only copies them through.
    def pw(self, count, ok):
        # rate = ok / count, the same formula score() uses
        return {"count": count, "ok": ok, "near": 0, "rate": ok / count, "heard": {}, "segs": ["s1"]}

    # A word is flagged when at least min_rate (0.6) of its occurrences were heard wrong.
    def test_thresholds(self):
        # alpha fails 3 of 5 (0.6), beta 2 of 5, gamma 1 of 1, delta 0 of 1, epsilon 10 of 10
        per = {"alpha": self.pw(5, 2), "beta": self.pw(5, 3), "gamma": self.pw(1, 0), "delta": self.pw(1, 1),
               "epsilon": self.pw(10, 0)}  # epsilon has the most occurrences and fails them all
        got = [e["word"] for e in asr.flag(per, 0.6)]  # min_rate given explicitly as 0.6, the default FAIL_RATE
        self.assertEqual(got, ["epsilon", "alpha", "gamma"])          # sorted by occurrences
        self.assertNotIn("beta", got)                                  # heard right 3 of 5
        # alpha sits exactly on the threshold; the 1e-9 tolerance in flag() keeps it in
        self.assertEqual(asr.flag(per, 0.6)[1]["fail_rate"], 0.6)

    # Words a recogniser routinely swallows are never flagged, however often they fail.
    def test_function_words_never_flagged(self):
        # "the" and "of" are in FUNCTION_WORDS; doorkeeper is not
        per = {"the": self.pw(50, 0), "of": self.pw(3, 0), "doorkeeper": self.pw(2, 0)}
        # only the content word survives, using the default threshold
        self.assertEqual([e["word"] for e in asr.flag(per)], ["doorkeeper"])

    # A word stays flagged only if the stronger confirm model also fails it.
    def test_confirmation_needs_both_models(self):
        # all three fail the first (CTC) model completely, so all are flagged
        flagged = asr.flag({"doorkeeper": self.pw(2, 0), "lucia": self.pw(2, 0), "paulo": self.pw(1, 0)})
        # the confirm model also fails doorkeeper but hears lucia right; paulo has no confirm data
        conf = {"doorkeeper": self.pw(2, 0), "lucia": self.pw(2, 2)}
        # combine_confirm sorts flagged words into three lists
        confirmed, cleared, unchecked = asr.combine_confirm(flagged, conf)
        self.assertEqual([e["word"] for e in confirmed], ["doorkeeper"])  # failed by both models: a real problem
        # the confirm model heard it right: probably a weakness of the small model
        self.assertEqual([e["word"] for e in cleared], ["lucia"])
        self.assertEqual([e["word"] for e in unchecked], ["paulo"])  # no second opinion available
        # both ok-rates are 0.0 for the confirmed word
        self.assertEqual((confirmed[0]["ctc_rate"], confirmed[0]["confirm_rate"]), (0.0, 0.0))


# Tests for words_from_tokens(): grouping sub-word (BPE) tokens from the model into timed words.
class TokenGroupingTests(unittest.TestCase):
    # A leading space starts a new word; tokens without one are glued to the previous word.
    def test_bpe_tokens_to_words(self):
        # no durations given, so each token ends one 0.04 s frame (the CTC model's frame) after it starts
        w = asr.words_from_tokens([" a", " door", "ke", "eper"], [0.0, 0.2, 0.4, 0.48], [], [], frame_s=0.04)
        self.assertEqual([x["w"] for x in w], ["a", "doorkeeper"])  # "door"+"ke"+"eper" become one word
        # the word starts at its first token and ends at the last token start plus one frame (0.48 + 0.04)
        self.assertEqual((w[1]["start"], w[1]["end"]), (0.2, 0.52))

    # Transducer models report token durations and log-probabilities; both must be used.
    def test_durations_and_probabilities(self):
        import math  # for log(); imported locally because only this test needs it
        # durations 0.4 s and 0.1 s; log-probs of 0.9 and 0.5
        w = asr.words_from_tokens([" Desire", " is"], [0.0, 0.5], [0.4, 0.1], [math.log(0.9), math.log(0.5)])
        self.assertEqual((w[0]["end"], w[1]["end"]), (0.4, 0.6))  # end = start + duration: 0.0 + 0.4 and 0.5 + 0.1
        # p is the lowest token probability in the word, converted back from the log value
        self.assertEqual((w[0]["p"], w[1]["p"]), (0.9, 0.5))


# Lets the file be run directly (python3 tests/narration/test_pron_asr.py) as well as through unittest discovery.
if __name__ == "__main__":
    unittest.main()  # runs every TestCase in this module
