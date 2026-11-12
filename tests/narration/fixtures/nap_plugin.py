# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Test plug-in: a "nap" tool that waits a given number of seconds and then writes one file.

The MCP tests use it as a job whose length they choose: 0 s for a tool that finishes within the call, a few
seconds for a background job, a minute for a job that is cancelled or must stop with the server.
"""
import time  # the wait
from pathlib import Path  # the output file

from speechkit_narrate.registry import tool


@tool("nap", params={"seconds": {"type": "number", "default": 0, "description": "how long to wait"},
                     "out": {"type": "string", "format": "path", "description": "the file to write afterwards"}},
      required=["out"], description="Wait, then write a file")
def nap(out, seconds=0, log=print):
    log(f"nap: {seconds} s")
    time.sleep(float(seconds))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(f"slept {seconds}\n", encoding="utf-8")
    return {"files": [str(out)], "slept": seconds}
