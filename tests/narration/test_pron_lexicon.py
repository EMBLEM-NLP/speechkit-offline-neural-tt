"""Tests for the pronunciation lexicon probe and the fix-candidate generator.

Fast: everything is pure Python except one batched call to the espeak helper (skipped when the
speechkit runtime venv is not on this machine).
"""
import unittest  # TestCase classes and assertions; the file runs under unittest or pytest

# fix-candidate generator: respellings that steer espeak toward the CMU pronunciation
from emblem_rsi.pron import candidates as cd
# word inventory helpers: splitting spoken tokens, normalising words, sentence pieces
from emblem_rsi.pron import inventory as inv
# the dictionary probe: ARPAbet to IPA, the coarse alphabet, distance and stress comparison
from emblem_rsi.pron import lexicon as lx

# CMU pronouncing-dictionary entry for "doorkeeper" in ARPAbet: digits mark stress (1 primary, 2 secondary, 0 none).
# Used throughout as the reference pronunciation the narration voice should match.
DOORKEEPER = "D AO1 R K IY2 P ER0"


# Shorthand used by every test: converts an ARPAbet string to IPA with espeak-style stress marks.
# arpa is a space-separated string such as "D AO1 R"; the return value is an IPA string.
def cmu(arpa):
    return lx.arpabet_to_ipa(arpa)  # delegates to the converter under test


# Tests for arpabet_to_ipa(): CMU's ARPAbet must become IPA written the way espeak writes it,
# so the two sources can be compared symbol by symbol.
class ArpabetToIpa(unittest.TestCase):
    # The reference word: stress marks go before the vowel and the unstressed ER becomes the r-coloured schwa.
    def test_doorkeeper(self):
        # D AO1 R K IY2 P ER0 gives d, primary ɔ, ɹ, k, secondary i, p, ɚ
        self.assertEqual(cmu(DOORKEEPER), "dˈɔɹkˌipɚ")

    # AH and ER change symbol with stress, matching how espeak distinguishes them.
    def test_reduced_and_stressed_ah_er(self):
        self.assertEqual(cmu("AH1 B AH0 V"), "ˈʌbəv")  # AH1 is the stressed vowel ʌ, AH0 the reduced schwa ə ("above")
        self.assertEqual(cmu("HH ER1"), "hˈɝ")  # stressed ER is ɝ ("her")

    # An ER directly before another vowel carries a linking r, as espeak writes it.
    def test_linking_r_before_vowel(self):
        # interrupts: ER0 followed by AH1 is written ɚɹ, like espeak's əɹ
        self.assertEqual(cmu("IH2 N T ER0 AH1 P T S"), "ˌɪntɚɹˈʌpts")

    # Two vowels in a row are two syllables, not one diphthong; a syllable dot keeps them apart.
    def test_adjacent_vowels_are_not_a_diphthong(self):
        # drawing = AO + IH, two syllables; must not turn into ɔɪ
        syms, _ = lx.coarse(cmu("D R AO1 IH0 NG"))
        # coarse symbols: d, r (kept because a vowel follows), O, I, ŋ; no "oI" diphthong
        self.assertEqual(syms, ["d", "r", "O", "I", "ŋ"])

    # An ARPAbet symbol the table does not know must fail loudly rather than be skipped.
    def test_unknown_symbol_raises(self):
        with self.assertRaises(ValueError):  # the converter raises ValueError for unknown symbols
            cmu("QQ1")  # QQ is not an ARPAbet phone


# Tests for the coarse comparison: espeak output and CMU pronunciations that sound the same must
# reach distance 0, despite rhoticity, vowel length and British/American vowel differences.
class Normalization(unittest.TestCase):
    # Custom assertion: the espeak IPA and the CMU entry must have coarse distance exactly 0.0.
    # espeak_ipa is an IPA string from the narration voice; arpa is a CMU ARPAbet string.
    # On failure the message shows both coarse symbol lists, which makes the mismatch easy to read.
    def assertSame(self, espeak_ipa, arpa):
        # distance() compares the two after reduction to the coarse common alphabet
        self.assertEqual(lx.distance(espeak_ipa, cmu(arpa)), 0.0, (lx.coarse(espeak_ipa), lx.coarse(cmu(arpa))))

    # The narration voice (espeak "en", British) and the reference voice ("en-us") must both match CMU.
    def test_british_doorkeeper_equals_cmu(self):
        self.assertSame("dˈɔːkiːpə", DOORKEEPER)          # espeak en, non-rhotic
        self.assertSame("dˈɔːɹkiːpɚ", DOORKEEPER)         # espeak en-us, rhotic

    # Non-rhotic British vowels must equal CMU's vowel plus R, because R before a consonant or word end is dropped.
    def test_non_rhotic_vowels(self):
        self.assertSame("kˈɑː", "K AA1 R")                # car
        self.assertSame("pɹˈeə", "P R EH1 R")             # prayer (centring diphthong = vowel + r)
        self.assertSame("mˈɔː", "M AO1 R")                # more
        self.assertSame("fˈaɪə", "F AY1 ER0")             # fire

    # Vowel splits between accents, merged function words and reduced syllables all count as equal.
    def test_vowel_splits_and_reductions(self):
        self.assertSame("lˈɒst", "L AO1 S T")             # ɒ ~ ɔ
        self.assertSame("ɪnðə", "IH0 N DH AH0")           # merged function words
        self.assertSame("θˈiətə", "TH IY1 AH0 T ER0")     # theatre: iə across a syllable
        # interrupts with stress on the wrong syllable still has distance 0: distance ignores stress
        self.assertSame("ˈɪntəɹˌʌpts", "IH2 N T ER0 AH1 P T S")
        self.assertSame("ˌeɪtˌiːʲˈɛm", "EY1 T IY2 EH1 M")  # ʲ glide dropped

    # American-style flaps and glottal stops count as t, and a syllabic n counts as schwa plus n.
    def test_american_flaps_and_syllabics(self):
        self.assertSame("lˈɪɾəl", "L IH1 T AH0 L")  # "little": the flap ɾ is mapped to t
        self.assertSame("bˈʌʔn̩", "B AH1 T AH0 N")  # "button": ʔ becomes t and n̩ becomes ə + n

    # Pairs listed in NEAR (reduction or accent splits) cost half a substitution.
    def test_near_vowels_cost_half(self):
        self.assertAlmostEqual(lx.distance("ɒv", cmu("AH1 V")), 0.25)   # of: LOT vs STRUT
        # "here": the iə diphthong reads as I, CMU gives i; a half-cost pair over two symbols is 0.25, under the 0.34
        # flag
        self.assertLess(lx.distance("hˈiə", cmu("HH IY1 R")), lx.FLAG_DISTANCE)

    # Genuine pronunciation differences must stay at or above the flag threshold (FLAG_DISTANCE = 0.34).
    def test_real_differences_survive(self):
        # "zed" against CMU "zee": one full substitution and one extra symbol over three symbols
        self.assertGreaterEqual(lx.distance("zˈɛd", cmu("Z IY1")), lx.FLAG_DISTANCE)
        # "neesh" against "nitch": a half-cost vowel plus a full consonant change gives 0.5
        self.assertGreaterEqual(lx.distance("nˈiːʃ", cmu("N IH1 CH")), lx.FLAG_DISTANCE)
        # "ill" against the contraction "I'll" (AY1 L): a different vowel in a two-symbol word
        self.assertGreaterEqual(lx.distance("ˈɪl", cmu("AY1 L")), lx.FLAG_DISTANCE)
        # A one-vowel change in a three-symbol word scores 1/3, just under FLAG_DISTANCE, so only > 0.3 is asserted;
        # heteronyms like "live" are caught by the separate heteronym check, not by distance.
        self.assertGreater(lx.distance("lˈaɪv", cmu("L IH1 V")), 0.3)

    # compare() returns the smallest distance over all CMU variants and which variant it was.
    def test_distance_is_min_over_variants(self):
        # espeak said the verb "live" with ɪ; the adjective reading (aɪ) is passed first, the verb second
        c = lx.compare("lˈɪv", [cmu("L AY1 V"), cmu("L IH1 V")])
        self.assertEqual((c["distance"], c["best"]), (0.0, 1))  # distance 0 against variant index 1


# Tests for stress_match(): espeak's primary stress must land on a syllable CMU stresses.
class Stress(unittest.TestCase):
    # Distance ignores stress, so this separate check catches a correct sound with the wrong emphasis.
    def test_same_syllable(self):
        # British doorkeeper puts the stress on the first syllable, like CMU
        self.assertTrue(lx.stress_match("dˈɔːkiːpə", cmu(DOORKEEPER)))

    # The same phonemes stressed on the wrong syllable must fail.
    def test_wrong_syllable(self):
        # primary on syllable 0, while CMU's primary is on syllable 2 (AH1)
        self.assertFalse(lx.stress_match("ˈɪntəɹˌʌpts", cmu("IH2 N T ER0 AH1 P T S")))
        # moving the primary mark to the ʌ makes it match
        self.assertTrue(lx.stress_match("ˌɪntəɹˈʌpts", cmu("IH2 N T ER0 AH1 P T S")))

    # Compounds can carry two CMU primaries; espeak matching either one is accepted.
    def test_two_cmu_primaries_accept_either(self):
        # "outside": espeak stresses the second part, CMU marks both parts as primary
        self.assertTrue(lx.stress_match("aʊtsˈaɪd", cmu("AW1 T S AY1 D")))

    # Two primaries from a hyphenated respelling must not stress a syllable CMU leaves unstressed.
    def test_extra_primary_on_unstressed_syllable_rejected(self):
        # hyphenated respellings give each part a primary; ruh-kord stresses CMU's unstressed syllable
        self.assertFalse(lx.stress_match("ɹˈʌkˈɔːd", cmu("R IH0 K AO1 R D")))
        # secondary on the first syllable, primary on the second: acceptable for "record" the verb
        self.assertTrue(lx.stress_match("ɹˌiːkˈɔːd", cmu("R IH0 K AO1 R D")))

    # With fewer than two vowels there is no stress position to get wrong.
    def test_monosyllables_always_match(self):
        # "the" has one vowel, so the check returns True without comparing
        self.assertTrue(lx.stress_match("ðə", cmu("DH AH0")))

    # stress_syllable() returns the 0-based syllable index of the primary stress.
    def test_stress_syllable(self):
        # espeak form: two vowels (ɪ, ə) come before the stressed ʌ
        self.assertEqual(lx.stress_syllable("ɪntəɹˈʌpts"), 2)
        # the CMU-derived form gives the same index, so the two can be compared directly
        self.assertEqual(lx.stress_syllable(cmu("IH2 N T ER0 AH1 P T S")), 2)


# Tests for the text helpers: number spelling, inventory word splitting, in-context phonemes, heteronym roles.
class TextHelpers(unittest.TestCase):
    # Numbers are spelled the way espeak "en" (British) reads digits, with "and" after hundred.
    def test_number_words(self):
        # British reading keeps "and" before the last part (asr.py deliberately drops it for en-US)
        self.assertEqual(lx.number_words(2105), "two thousand one hundred and five")
        self.assertEqual(lx.number_words(1600), "one thousand six hundred")  # no "and" when the remainder is zero
        # spell_number accepts the grouped form with a comma
        self.assertEqual(lx.spell_number("1,600"), "one thousand six hundred")
        self.assertEqual(lx.spell_number("10th"), "tenth")  # ordinal suffix turns the last word into its ordinal
        self.assertEqual(lx.spell_number("21"), "twenty one")  # twenty-one is written as two words without a hyphen
        # the largest number it spells, and the first one it does not: a million and up is unspellable (None) instead
        # of an error that stopped the whole measurement (a title with a date such as 20261031 did)
        self.assertEqual(lx.spell_number("999,999"),  # spelled in full, both halves with "and"
                         "nine hundred and ninety nine thousand nine hundred and ninety nine")
        self.assertIsNone(lx.spell_number("1,000,000"))  # one million, written with commas
        self.assertIsNone(lx.spell_number("20261031"))  # a date written as one number
        self.assertIsNone(lx.spell_number("1000000th"))  # an ordinal that large is unspellable too

    # Inventory helpers that build the per-word table from spoken tokens.
    def test_inventory_words(self):
        # spaces split words; the trailing comma is stripped from "1"
        self.assertEqual(inv.split_words("23 verse 1,"), ["23", "verse", "1"])
        # hyphens split too, so each part is looked up on its own
        self.assertEqual(inv.split_words("twenty-minute"), ["twenty", "minute"])
        # norm_word lowercases and strips edge punctuation; numbers keep digits and commas; accents are kept here
        # (unlike the ASR normaliser) because CMUdict lookup tries an accent-free form itself.
        self.assertEqual([inv.norm_word(w) for w in ["Ill", "Doorkeeper.", "1,600.", "Lúcia"]],
                         ["ill", "doorkeeper", "1,600", "lúcia"])  # the expected normalised forms, in the same order
        # a token ending in . closes its piece; the final "d?" opens no empty piece after it
        self.assertEqual(inv.sentence_pieces(["A", "b.", "C", "d?"]), [[0, 1], [2, 3]])
        # "C?" ends a piece mid-segment, so "d" forms a piece of its own
        self.assertEqual(inv.sentence_pieces(["A", "b.", "C?", "d"]), [[0, 1], [2], [3]])

    # context_phonemes() splits a whole sentence's phoneme string among its words.
    def test_context_split(self):
        # the sentence as espeak phonemized it (function words run together) and each word's isolated phonemes
        parts = lx.context_phonemes("ɪnðə hˈaʊs ɒvðə lˈɔːd", ["ˈɪn", "ðə", "hˈaʊs", "ˈɒv", "ðə", "lˈɔːd"])
        self.assertEqual(parts[2], "hˈaʊs")  # "house" receives exactly its own phonemes
        self.assertEqual(parts[5], "lˈɔːd")  # the last word, "lord", also gets the right slice
        # nothing is lost or duplicated: the pieces rejoin to the sentence without spaces or stress marks
        self.assertEqual("".join(parts).replace("ˈ", ""), "ɪnðəhaʊsɒvðəlɔːd")

    # Heteronym helpers: which CMU variant is the verb reading, and which role the context expects.
    def test_heteronym_roles(self):
        self.assertTrue(lx.verb_reading("use", "Y UW1 Z"))  # "use" with a final Z is the verb
        self.assertFalse(lx.verb_reading("use", "Y UW1 S"))  # "use" with a final S is the noun
        # "record" stressed on the second syllable is the verb
        self.assertTrue(lx.verb_reading("record", "R IH0 K AO1 R D"))
        # "separate" ending in AH0 T is the adjective, not the verb (which ends EY1 T)
        self.assertFalse(lx.verb_reading("separate", "S EH1 P R AH0 T"))
        # sentence-initial word followed by an object word: an imperative verb
        self.assertEqual(lx.expected_role(["use", "this", "drawing"], 0, "use"), "verb")
        # after a determiner ("the") the word is a noun or adjective
        self.assertEqual(lx.expected_role(["the", "perfect", "spreadsheet"], 1, "perfect"), "noun-adj")
        # The comma after "refuse" (punct_after[1]) starts a new clause, so "use" before "a" is read as a verb.
        self.assertEqual(lx.expected_role(["to", "refuse", "use", "a"], 2, "use", [False, True, False, False]), "verb")
        # a comma right after the word blocks the object-follows rule, so no role is guessed
        self.assertIsNone(lx.expected_role(["house", "the", "talk"], 0, "house", [True, False, False]))


# Tests for the respelling generator in candidates.py.
class CandidateText(unittest.TestCase):
    # Syllables follow the maximal-onset rule, and respellings come in joined and hyphenated forms.
    def test_syllabify_and_respell(self):
        # R K is not a legal onset, so R stays with the first syllable and K starts the second
        self.assertEqual(cd.syllabify(DOORKEEPER.split()), [["D", "AO1", "R"], ["K", "IY2"], ["P", "ER0"]])
        self.assertIn("dorkeeper", cd.respellings(DOORKEEPER))  # joined respelling of the CMU pronunciation
        # the hyphenated form, one piece per syllable, is offered as well
        self.assertIn("dor-kee-per", cd.respellings(DOORKEEPER))
        # CH after a short vowel is spelled "tch" so espeak does not read "nich"
        self.assertIn("nitch", cd.respellings("N IH1 CH"))
        self.assertIn("zee", cd.respellings("Z IY1"))  # a single long-vowel syllable

    # propose() offers compound splits and restores apostrophes the spoken text dropped.
    def test_propose_compound_and_apostrophe(self):
        # the row supplies the CMU variants, as a lexicon_measure.json row would
        texts = [c["text"] for c in cd.propose("doorkeeper", {"cmu_arpabet": [DOORKEEPER]})]
        self.assertIn("door keeper", texts)  # door and keeper are both CMU words, so the compound split is offered
        self.assertIn("door-keeper", texts)  # the hyphenated compound form too
        self.assertNotIn("doorkeeper", texts)  # the original spelling is never proposed as its own fix
        # spoken text "ill" for the display form "i'll": the first candidate restores the apostrophe
        first = cd.propose("i'll", {"spoken_as": "ill", "cmu_arpabet": ["AY1 L"]})[0]
        # capital I restored; display fixes are added before any other kind
        self.assertEqual(first, {"text": "I'll", "kind": "display"})


# These tests call the real espeak through the speechkit runtime venv; skipped when it is not installed.
@unittest.skipUnless(lx.espeak_available(), "speechkit runtime venv with piper not present")
# They prove the probe's numbers on live espeak output and that the helper subprocess runs only once.
class WithEspeak(unittest.TestCase):
    # unrelated or wrong spellings mixed in to check that only improvements survive scoring
    JUNK = ["neesh", "knee", "nice", "notch", "doorkeeper"]

    @classmethod  # one setup for the whole class, so espeak is started once
    # Phonemizes every word and candidate the tests will need in a single helper call.
    # Side effects: one subprocess run of _espeak_helper.py, results kept in lexicon's module cache.
    def setUpClass(cls):
        # candidate lists for the two test words, with CMU variants looked up automatically
        cls.props = {w: cd.propose(w) for w in ("doorkeeper", "niche")}
        # everything to phonemize: the two words, the junk spellings and every candidate text
        texts = ["doorkeeper", "niche", *cls.JUNK] + [c["text"] for p in cls.props.values() for c in p]
        before = lx.ESPEAK_CALLS  # remember the call counter so the number of helper runs can be measured
        cls.ph = dict(zip(texts, lx.espeak(texts)[lx.NARRATION_VOICE]))   # the one helper call
        cls.calls = lx.ESPEAK_CALLS  # the counter after the batch call
        assert cls.calls - before <= 1  # at most one new run (zero if an earlier test already cached everything)

    # Runs after each test: no test may start espeak again, which proves score() reuses the cache.
    def tearDown(self):
        # the counter must not have moved since setUpClass
        self.assertEqual(lx.ESPEAK_CALLS, self.calls, "the espeak helper must be called only once")

    # Live espeak "en" output for doorkeeper must match CMU exactly.
    def test_doorkeeper_espeak_matches_cmu(self):
        e = self.ph["doorkeeper"]  # the narration voice's phonemes for the word
        self.assertEqual(e, "dˈɔːkiːpə")  # the known non-rhotic British form
        c = lx.compare(e, [cmu(DOORKEEPER)])  # compare against the single CMU variant
        self.assertEqual(c["distance"], 0.0)  # distance 0 after coarse reduction
        self.assertTrue(c["stress_ok"])  # and the primary stress is on the same syllable

    # When the original already matches CMU, no candidate can be strictly better, so score() returns nothing.
    def test_no_candidate_beats_a_perfect_match(self):
        # the extra string "door keeper" is accepted as a plain candidate too
        self.assertEqual(cd.score("doorkeeper", self.props["doorkeeper"] + ["door keeper"]), [])

    # Every surviving candidate must be closer to CMU than the original and keep the stress right.
    def test_candidates_are_never_worse(self):
        cands = self.props["niche"] + self.JUNK  # real candidates for "niche" plus the junk spellings
        ranked = cd.score("niche", cands)  # score() phonemizes them (from the cache) and ranks the ones that improve
        self.assertTrue(ranked)  # espeak says "neesh" for niche, so at least one fix must exist
        # Recompute the starting distance against the first listed CMU variant; [:1] drops the second entry,
        # which CMUdict does not actually contain for this word.
        before = lx.compare(self.ph["niche"], [cmu(a) for a in ["N IH1 CH", "N IY1 SH"]][:1])["distance"]
        for r in ranked:  # each survivor in turn
            self.assertLess(r["distance_after"], r["distance_before"])  # strictly closer than before
            self.assertTrue(r["stress_ok_after"])  # and its primary stress is right
        # the reported starting distance matches the independent calculation
        self.assertEqual(ranked[0]["distance_before"], before)
        # the ranking key: distance, then kind (display < compound < hyphenate < respell), then length
        keys = [(r["distance_after"], cd.KIND_RANK[r["kind"]], len(r["respell"])) for r in ranked]
        self.assertEqual(keys, sorted(keys))  # the list is already in that order
        self.assertEqual(ranked[0]["respell"], "nitch")  # "nitch" reaches distance 0 and ranks first


# Lets the file be run directly as a script.
if __name__ == "__main__":
    unittest.main()  # runs every test in this module
