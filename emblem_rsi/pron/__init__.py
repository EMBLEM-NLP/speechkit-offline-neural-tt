# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Pronunciation audit for narrated books (EMBLEM-NLP speechkit output).

Probes (each writes plain JSON so the loop can hash and compare them):
  inventory  every spoken word occurrence in the transcript, with segment and time
  lexicon    the voice's espeak phonemes per unique word vs. the CMU reference dictionary
  asr        offline speech recognition of the narration, scored word by word against the script

Before voicing (speechkit-voice-tts-offline-pipeline's prevoice stage):
  heteronyms  the reading espeak will give each heteronym in its sentence, and the reading the sentence wants
  tagger      optional part-of-speech tagger (spaCy en_core_web_sm when installed; rules only without it)
  wordaudit   the voice's own phonemes for each fixed word against the fix
"""
