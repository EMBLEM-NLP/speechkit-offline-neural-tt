# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Windows paths for a toolkit that runs inside WSL (Windows Subsystem for Linux).

An MCP client on Windows, such as Claude Desktop, can start the server inside WSL with
`wsl.exe -d <distro> --exec python -m speechkit_narrate mcp`. The client then talks in Windows paths (C:\\Users\\me\\Book),
which a Linux Python cannot open. This module translates them on the way in (to /mnt/c/Users/me/Book) and turns
/mnt/c/... paths in the results back into C:\\... on the way out.

SPEECHKIT_WINPATHS chooses when that happens:
    auto  (default) only when the toolkit runs inside WSL. Results use the Windows form only after the client has sent
          a Windows path, so a client that works in Linux paths keeps seeing Linux paths.
    on    always. For a container or a test that mounts the drives somewhere else (see SPEECHKIT_WINPATHS_ROOT).
    off   never.
SPEECHKIT_WINPATHS_ROOT is the folder that holds the drive folders: /etc/wsl.conf's [automount] root, else /mnt.

Only absolute Windows paths are translated: C:\\dir, C:/dir, \\\\?\\C:\\dir, and \\\\wsl$\\<this distro>\\dir (also
written \\\\wsl.localhost\\...). A relative path with backslashes is left alone, because on Linux a backslash is a
legal file name character and the client's working folder is not the server's.
"""
from __future__ import annotations  # hints such as "str | None" stay unevaluated strings, as in the other modules

import configparser  # reads /etc/wsl.conf, which is an INI file
import os  # environment variables, the platform name and os.fspath
import re  # recognises the Windows forms and rewrites the Linux ones
import shutil  # which() finds wslpath, WSL's own translator
import subprocess  # runs wslpath
from pathlib import Path  # the two small files read below

MODE_VAR = "SPEECHKIT_WINPATHS"  # environment variable that picks auto, on or off
ROOT_VAR = "SPEECHKIT_WINPATHS_ROOT"  # environment variable that moves the folder holding the drive folders
WSL_CONF = Path("/etc/wsl.conf")  # WSL's per-distribution settings, which may move the drives
PROC_VERSION = Path("/proc/version")  # the kernel banner, which names Microsoft inside WSL 1 and WSL 2
WSLPATH = "wslpath"  # WSL's own path translator, looked up on PATH

# C:\ , C:/ , a bare C: , or the extended form \\?\C:\ ; "drive" is the letter, "rest" what follows the separators.
# A drive-relative form such as C:file has no separator after the colon, so it does not match.
DRIVE = re.compile(r"^(?:\\\\\?\\)?(?P<drive>[A-Za-z]):(?:[\\/]+(?P<rest>.*))?$", re.DOTALL)
# \\wsl$\Ubuntu\home\me and \\wsl.localhost\Ubuntu\home\me, with either slash; "distro" is the distribution's name
UNC = re.compile(r"^[\\/]{2}(?:wsl\$|wsl\.localhost)[\\/]+(?P<distro>[^\\/]+)(?:[\\/]+(?P<rest>.*))?$",
                 re.DOTALL | re.IGNORECASE)  # the host name and the distribution compare without regard to case

_seen = {"windows": False}  # becomes True once a client has sent a Windows path (switches the results to that form)
_cache: dict[str, str | None] = {}  # wslpath's answers (None for a failure), so the process runs once per distinct path


# The chosen mode: "auto", "on" or "off". Reads SPEECHKIT_WINPATHS; 1, true, yes and on all mean on, 0, false, no and off
# all mean off, and anything else (including unset) means auto.
def mode() -> str:
    raw = os.environ.get(MODE_VAR, "").strip().lower()  # the variable's text, trimmed and lower-cased
    if raw in ("1", "on", "true", "yes"):  # the spellings of "always"
        return "on"  # translate everywhere
    if raw in ("0", "off", "false", "no"):  # the spellings of "never"
        return "off"  # leave every path alone
    return "auto"  # unset or unrecognised: decide from where the toolkit runs


# True when this process runs inside WSL. WSL sets WSL_DISTRO_NAME (and WSL_INTEROP) for the processes it starts, and
# the kernel banner is the second signal for a process whose environment was cleaned. Reads at most one small file.
def in_wsl() -> bool:
    if os.name == "nt":  # a Windows Python is not inside WSL, whatever its environment says
        return False  # so it never translates
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):  # variables WSL sets for its processes
        return True  # the environment says WSL
    try:  # the kernel banner exists on Linux only
        # WSL 1 reports "Microsoft", WSL 2 "microsoft-standard-WSL2"; ordinary Linux kernels do not mention either
        return "microsoft" in PROC_VERSION.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:  # no /proc (macOS) or not readable
        return False  # not WSL as far as can be told


# True when Windows paths are translated on the way in: forced on, or automatic and inside WSL.
def active() -> bool:
    chosen = mode()  # auto, on or off
    return chosen == "on" or (chosen == "auto" and in_wsl())  # "on" always; "auto" only inside WSL


# True when results should show drive paths in the Windows form: forced on, or automatic, inside WSL, and the client has
# already sent a Windows path (so it is known to read that form).
def reporting() -> bool:
    chosen = mode()  # auto, on or off
    if chosen == "on":  # forced on: results always use the Windows form
        return True  # report in the Windows form
    return chosen == "auto" and in_wsl() and _seen["windows"]  # automatic: only after the client used a Windows path


# The folder that holds the drive folders, without a trailing slash: SPEECHKIT_WINPATHS_ROOT, else /etc/wsl.conf's
# [automount] root, else /mnt. "/" gives "" (the drives sit directly under the root, as /c). Reads wsl.conf if it
# exists.
def mount_root() -> str:
    given = os.environ.get(ROOT_VAR, "").strip()  # an explicit folder wins
    if not given:  # nothing explicit: look at WSL's own configuration
        conf = configparser.ConfigParser(interpolation=None, strict=False)  # plain INI; tolerate repeated keys
        try:  # a missing or damaged file just means the default
            conf.read(WSL_CONF, encoding="utf-8")  # a missing file is skipped without an error
            given = conf.get("automount", "root", fallback="").strip().strip("\"'")  # [automount] root = /mnt/
        except (configparser.Error, UnicodeDecodeError, OSError):  # damaged or unreadable
            given = ""  # fall back to the default below
    if not given:  # still nothing: WSL's default
        return "/mnt"  # drives appear as /mnt/c, /mnt/d, ...
    if not given.startswith("/"):  # a root written without its leading slash
        given = "/" + given  # make it absolute
    return given.rstrip("/")  # no trailing slash, so "/" becomes ""


# Ask WSL itself to translate a Windows path (`wslpath -u`), which also knows drives mounted somewhere unusual.
# text: an absolute Windows path. Returns the Linux path, or None when wslpath is missing, fails or prints nothing.
def _wslpath(text: str) -> str | None:
    exe = shutil.which(WSLPATH)  # only exists inside WSL
    if not exe or not in_wsl():  # no translator here
        return None  # the caller falls back to the pure rewrite
    try:  # starting a process can fail or hang
        # -u: Windows to Unix. The path is one list item, so no shell ever re-parses its backslashes.
        run = subprocess.run([exe, "-u", text], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=10, check=False)  # at most ten seconds; a non-zero exit is checked below
    except (OSError, subprocess.SubprocessError):  # could not start, or timed out
        return None  # fall back to the pure rewrite
    out = run.stdout.strip()  # the translated path without its newline
    return out if run.returncode == 0 and out.startswith("/") else None  # accept only a real absolute answer


# The pure rewrite of a Windows path to its Linux form, with no process started. text: a path with its quotes already
# removed. Returns the Linux path, or None for a form that cannot be mapped (a path in another distribution, or a path
# that is not an absolute Windows path at all).
def _rewrite(text: str) -> str | None:
    found = DRIVE.match(text)  # C:\dir, C:/dir, \\?\C:\dir or a bare C:
    if found:  # a drive path
        rest = (found.group("rest") or "").replace("\\", "/")  # what follows the drive, with forward slashes
        path = f"{mount_root()}/{found.group('drive').lower()}" + (f"/{rest}" if rest else "")  # /mnt/c/dir
        return re.sub(r"/{2,}", "/", path)  # C:\\dir (doubled slashes) and C:\dir\ both collapse to one slash
    found = UNC.match(text)  # \\wsl$\Ubuntu\dir or \\wsl.localhost\Ubuntu\dir
    if found:  # a path in a WSL distribution's own file system
        here = os.environ.get("WSL_DISTRO_NAME", "")  # the distribution this process runs in, if known
        if here and found.group("distro").lower() != here.lower():  # it names a different distribution
            return None  # cannot be reached by an ordinary Linux path from here
        rest = (found.group("rest") or "").replace("\\", "/")  # the part inside the distribution
        return re.sub(r"/{2,}", "/", "/" + rest)  # an absolute Linux path, which is "/" for the distribution's root
    return None  # neither form


# Turn a Windows path into the path this process can open. text: any string (a path argument from the client).
# Returns the Linux path when translation is active and text is an absolute Windows path (surrounding spaces and one
# pair of double quotes, as Explorer's "Copy as path" adds, are ignored); otherwise returns text unchanged. A successful
# translation also records that the client speaks Windows paths, which switches results to that form.
def to_local(text: str) -> str:
    if not isinstance(text, str) or not active():  # not text, or translation is off here
        return text  # nothing to do
    clean = text.strip()  # leading and trailing spaces are never part of a path the client means
    if len(clean) > 1 and clean[0] == clean[-1] == '"':  # wrapped in a pair of double quotes
        clean = clean[1:-1].strip()  # take the quotes off
    if not (DRIVE.match(clean) or UNC.match(clean)):  # not an absolute Windows path
        return text  # leave it exactly as given
    if clean not in _cache:  # first time this path is seen
        _cache[clean] = _wslpath(clean)  # WSL's own answer, or None; the process runs once per distinct path
    # wslpath knows unusual mounts; the rewrite covers the standard ones and any machine without wslpath
    local = _cache[clean] or _rewrite(clean)
    if local is None:  # a form that cannot be mapped (another distribution's file system)
        return text  # leave it, so the usual "does not exist" error names what the client sent
    _seen["windows"] = True  # the client uses Windows paths: show results that way too
    return local  # the path this process can open


# Turn one Linux path under the drive folders into its Windows form. text: any string; root: the drive folders' folder
# (default mount_root()). Returns C:\dir for /mnt/c/dir, C:\ for /mnt/c, and text unchanged when it is not exactly such
# a path. Only whole strings are matched, so a sentence that mentions a path is never rewritten.
def to_windows(text: str, root: str | None = None) -> str:
    base = mount_root() if root is None else root  # the folder that holds the drive folders
    found = re.match(re.escape(base) + r"/(?P<drive>[a-z])(?:/(?P<rest>.*))?$", text, re.DOTALL)  # /mnt/c or /mnt/c/dir
    if not found:  # an ordinary string or a path outside the drives
        return text  # unchanged
    rest = (found.group("rest") or "").replace("/", "\\")  # the part below the drive, with backslashes
    return found.group("drive").upper() + ":\\" + rest  # C:\ followed by the rest


# Rewrite every whole-string Linux drive path inside a result. value: any JSON-like value (dict, list, string,
# path object, number ...); root: the drive folders' folder. Returns a new value of the same shape; the input is
# never changed, because job records and caches share it.
def _map(value, root: str):
    if isinstance(value, str):  # a string may be a drive path
        return to_windows(value, root)  # rewritten when it is exactly such a path
    if isinstance(value, os.PathLike):  # a Path object, which JSON output prints as text
        return to_windows(os.fspath(value), root)  # handled like the string it prints as
    if isinstance(value, dict):  # an object: keys stay, values are rewritten
        return {k: _map(v, root) for k, v in value.items()}  # a new dict
    if isinstance(value, (list, tuple)):  # an array: every item is rewritten
        return [_map(v, root) for v in value]  # a new list
    return value  # numbers, booleans and None have no path inside


# The result as the client should see it: drive paths in the Windows form when reporting() says so, else unchanged.
# value: the result (usually a dict). Returns a new value, or the very same object when nothing is rewritten.
def outbound(value):
    if not reporting():  # the client does not use Windows paths (or translation is off)
        return value  # hand the result back as it is
    return _map(value, mount_root())  # a rewritten copy


# One path for a message to the client, in the form the client uses (the Windows form when reporting() says so).
# text: a Linux path. Returns text or its Windows form.
def present(text: str) -> str:
    return to_windows(text) if reporting() else text  # only rewritten when the client reads that form


# What the server knows, for narrate_describe: the mode, whether translation is active, whether this is WSL, the
# folder holding the drives and the distribution's name (None when unknown). Returns a dict of plain values.
def status() -> dict:
    # the facts the server describes itself with; mount_root falls back to "/" when there is no drive folder
    return {"mode": mode(), "active": active(), "wsl": in_wsl(), "mount_root": mount_root() or "/",
            "distro": os.environ.get("WSL_DISTRO_NAME") or None}  # the name is None outside WSL


# One sentence for the server instructions, or "" when translation is off here. It tells the model that Windows paths
# work.
def note() -> str:
    if not active():  # nothing to tell
        return ""  # no sentence
    # the sentence itself, in two parts that Python joins into one string
    return (" Windows paths such as C:\\Users\\me\\Book are accepted too, and results show drive paths in that form "
            "once a Windows path has been sent.")  # a leading space joins it to the sentence before


# Forget what was learned (the cached translations and that a Windows path was seen). The tests call this between cases.
def reset() -> None:
    _cache.clear()  # drop the translations
    _seen["windows"] = False  # the client has not yet used a Windows path
