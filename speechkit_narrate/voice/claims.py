# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Shares the segments of a narration between parallel voice workers while they run.

A fixed split (worker k takes segments k, k+n, k+2n, ...) leaves one worker idle at the end whenever the other was
handed the slow segments. A segment the voice has to redo at a slower pace costs several times what the same words
cost otherwise, and such segments are not spread evenly. Here every worker walks the same list, longest segments
first, and takes a segment only when it is the first to ask for it, so whoever is free takes the next one.

The asking is a marker file per segment in a folder that the workers share. Creating a file with O_CREAT | O_EXCL
succeeds for exactly one process, so no two workers can voice the same segment, and no lock or message is needed. The
folder must be new for each run (narrate.run_workers makes one), so markers of an earlier run cannot hide segments.

This module uses only the standard library, because karaoke_voice.py loads it inside the speech runtime's own Python.
"""
# lets annotations stay unevaluated strings
from __future__ import annotations

import os  # os.open with O_EXCL is the atomic "first to ask wins"
from collections.abc import Iterator  # the type of what take_in_turns returns
from pathlib import Path  # the marker folder as a Path


# Puts segments in the order in which workers ask for them: longest spoken text first. Taking the long ones first leaves
# only short ones for the end, so the worker that finishes last is late by little. Segments of equal length keep the
# order of the book. segments: the request's list of segment dicts; each has "spokenText" (or "text" when it has no
# spoken form). Returns a new list and leaves the argument as it was.
def longest_first(segments: list) -> list:
    # sorted() is stable, so equal lengths stay in book order; the minus sign makes the longest come first
    return sorted(segments, key=lambda s: -len(s.get("spokenText") or s["text"]))


# Yields the segments this worker voices, one at a time. segments: the request's segments; folder: the marker folder
# that every worker of the run shares (it must exist). A segment is yielded only when this call created its marker file,
# so each segment goes to exactly one of the workers. The asking happens when the caller wants the next segment, not
# before, which is what lets a worker that has finished take the next free one instead of a share fixed in advance.
def take_in_turns(segments: list, folder) -> Iterator[dict]:
    folder = Path(folder)  # accept a string as well
    for seg in longest_first(segments):  # every worker walks the same order
        try:  # creating the marker either works for this process alone or fails because another one made it
            fd = os.open(folder / seg["id"], os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # fails if the marker exists
        except FileExistsError:  # another worker asked first
            continue  # leave this segment to it and ask for the next
        os.close(fd)  # the marker file is all that is needed, so the handle can go
        yield seg  # this worker voices the segment, then asks again
