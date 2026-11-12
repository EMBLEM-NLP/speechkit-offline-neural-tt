# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# Entry point for `python -m speechkit_narrate`: Python runs this module when the package itself is executed.
import sys  # sys gives access to the process exit code

from .cli import main  # main() parses the command line and runs the requested command, returning an exit status

sys.exit(main())  # hand main()'s return value (0 success, 1 failure, 2 usage or project error) to the operating system
