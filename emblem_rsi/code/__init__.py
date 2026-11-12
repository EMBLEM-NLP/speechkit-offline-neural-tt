# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Code-comment audit for the emblem-rsi loop: every line of code explained, the code itself provably unchanged.

The audit seals a rubric and snapshots every code file as the baseline. Explanations are written as comment maps
(one per file, kept outside the code) and applied mechanically: comments are inserted, code is never edited.
Each measurement proves, per file, that the code is identical to the baseline with comments removed (using the
language's own tokenizer or syntax tree), that it still parses, that the test suites pass, and how many required
code lines are explained. The loop promotes a round only when nothing regressed; a veto is undone with `revert`.

    langs.py     per-language line analysis and code identity (Python, PowerShell, JavaScript, CSS, HTML, XML,
                 TOML, pip requirements)
    cmap.py      the comment-map format and the applier
    measure.py   seal, measure, check, apply, revert
    cli.py       `python -m emblem_rsi code ...`
"""
