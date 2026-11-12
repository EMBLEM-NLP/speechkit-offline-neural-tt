# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# Entry point for `python -m emblem_rsi`: Python runs this module when the package itself is executed.
# It only forwards to the command line parser in cli.py, so all behaviour lives in one place.
from .cli import main  # main() builds the argument parser and runs the chosen subcommand, returning an exit status
raise SystemExit(main())  # pass that status (0 ok, 1 check failed or veto, 2 usage or input error) back to the shell
