"""Stands in for wsl.exe in tests: `fake_wsl.py wslpath -a PATH` maps the path (adds FAKE_WSL_PREFIX), `wslpath -w`
maps it back, anything else is run as-is."""
# Test stand-in for wsl.exe. Both test suites put "python fake_wsl.py" where a WSL launcher goes: test_registry.py
# through SPEECHKIT_DELEGATE (the Python side's delegate prefix) and SpeechKit Voice TTS Offline Pipeline.Tests.ps1 through $env:SPEECHKIT_WSL.
# It behaves like `wsl.exe --exec`: whatever follows the script name is a Linux program and its arguments, run
# directly with no shell, so the toolkit's delegation command lines can be exercised on Linux without WSL.
import os  # reads FAKE_WSL_PREFIX and FAKE_WSL_LOG from the environment
import subprocess  # runs the forwarded command as a real child process
import sys  # sys.argv holds the forwarded command; sys.exit returns the status

# Everything after the script's own path is what the caller asked WSL to run, for example
# ["wslpath", "-a", "/tmp/a b/c.pdf"] or ["env", "PYTHONPATH=...", "python3", "-m", "speechkit_narrate", "--json", ...].
args = sys.argv[1:]  # argv[0] is this file, so drop it
# Real wsl.exe is called as `wsl.exe [-d DISTRO] [-u USER] --exec COMMAND ...`. The module's installer builds that whole
# command line itself, so everything up to and including --exec is launcher options, and only the Linux command after
# it is passed on to the checks below. When the caller already left the options out (the SPEECHKIT_WSL and
# SPEECHKIT_DELEGATE prefixes end where the Linux command begins), there is no --exec and nothing changes.
if "--exec" in args:  # present only when a complete wsl.exe command line was given
    args = args[args.index("--exec") + 1:]  # keep the Linux command and its arguments
# `wslpath -a PATH` is how the PowerShell module (ConvertTo-NarrationWslPath) and registry.remote_path turn a
# Windows path into its absolute WSL form (C:\Books -> /mnt/c/Books).
if args[:2] == ["wslpath", "-a"]:  # compare only the first two words; args[2] is the path
    # Simulate the translation by prefixing FAKE_WSL_PREFIX ("/wsl" or "/proc/self/root" in the Pester tests). With no
    # prefix set the path comes back unchanged, which keeps every translated path usable on this Linux host.
    print(os.environ.get("FAKE_WSL_PREFIX", "") + args[2])  # one line on stdout, the format real wslpath prints
    sys.exit(0)  # exits before the log write below, so path lookups never show up in FAKE_WSL_LOG
# `wslpath -w PATH` is the reverse direction (WSL path back to a Windows path), used by registry.local_path for
# file names that come back in a delegated tool's result.
if args[:2] == ["wslpath", "-w"]:
    pre = os.environ.get("FAKE_WSL_PREFIX", "")  # the same prefix the -a branch would have added
    # Strip the prefix only when the path really starts with it; any other path is echoed as is, the way wslpath
    # treats a path that has no Windows form of its own.
    print(args[2][len(pre):] if pre and args[2].startswith(pre) else args[2])
    sys.exit(0)  # status 0 tells the caller the answer can be trusted
# Any other command is the real work. Append its command line to FAKE_WSL_LOG so a test can assert what was sent
# (for example "--only penguin --force penguin"); without the variable the line goes to os.devnull and is lost.
# append mode: several forwarded calls in one test each leave a line
with open(os.environ.get("FAKE_WSL_LOG", os.devnull), "a", encoding="utf-8") as f:
    f.write(" ".join(args) + "\n")  # arguments joined by single spaces, one line per forwarded call
# Run the command with its arguments unchanged and no shell, as `wsl --exec` would, and pass its exit code back
# so the toolkit sees success or failure exactly as it would from WSL. stdin, stdout and stderr are inherited,
# so the delegated command's JSON result and log lines reach the caller directly. The whole environment is
# inherited too, which real wsl.exe does not do (it only shares variables listed in WSLENV).
# This test launcher emulates Linux even when the test host is macOS.
args = ["SPEECHKIT_PLATFORM=linux" if arg == "SPEECHKIT_PLATFORM=" else arg for arg in args]
sys.exit(subprocess.call(args, env={**os.environ, "SPEECHKIT_PLATFORM": "linux"}, timeout=60))  # the child's exit status becomes this process's exit status
