# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Where the external pieces live, and `doctor` / `setup` for them.

Override any location with an environment variable:
  SPEECHKIT_HOME          base folder for downloads and caches (default: %LOCALAPPDATA%\\speechkit-narrate on Windows,
                          ~/.local/share/speechkit-narrate elsewhere)
  SPEECHKIT_CHECKOUT      speechkit-offline-neural-tt checkout (has speechkit/src, speechkit/profiles, bootstrap.py);
                          default: the checkout this package sits in
  SPEECHKIT_RUNTIME       installed speechkit runtime (has .venv/bin/python, voices/); default: <checkout>/installed/runtime
  SPEECHKIT_ASR_MODELS    folder with the two sherpa-onnx recognizer models for the pronunciation audit
  SPEECHKIT_CACHE         derived files (aligned voice model, scratch projects, MCP job logs)

The voice runtime is Linux x86-64 / CPython 3.13 only. On Windows the stages that need it (prevoice, narrate, autofix,
audit, the speak tool) run in WSL through SPEECHKIT_DELEGATE (see registry.py and the PowerShell module).
"""
# Keeps annotations as plain strings (PEP 563) so `list[tuple[str, bool, str]]` hints cost nothing at import.
from __future__ import annotations

import hashlib  # SHA-256 of model files, used to name the cached aligned voice model
import json  # reads voice profile JSON files
import platform
import tempfile
import os  # environment variables (SPEECHKIT_*, LOCALAPPDATA, PATH, ...)
import shutil  # shutil.which finds programs on PATH
import subprocess  # runs pip, Playwright's installer and the speechkit bootstrap
import sys  # sys.platform and sys.executable (the Python running speechkit-narrate)
import tarfile  # unpacks the downloaded .tar.bz2 recognizer models
import urllib.request  # downloads the recognizer models from GitHub
from pathlib import Path  # Path joins folders with the right separator on each platform

# Folder of the speechkit-narrate package itself (speechkit_narrate/), resolved to an absolute path.
PKG = Path(__file__).resolve().parent
# repository root, the folder that contains speechkit_narrate/ and emblem_rsi/; used for PYTHONPATH and delegation
REPO = PKG.parent
WINDOWS = sys.platform == "win32"  # True on native Windows Python (not WSL, where sys.platform is "linux")


# Tells whether this copy of speechkit-narrate was installed with pip (a speechkit-narrate-<version>.dist-info folder sits beside the
# package, which is then inside a site-packages folder) instead of being run from a toolkit folder or checkout.
# Takes no parameters and returns True or False. Callers use it wherever a toolkit folder would be put on PYTHONPATH:
# for an installed copy that folder is site-packages, which holds every other package too and would shadow the
# packages of a different Python that receives it.
def installed() -> bool:
    """True for a pip-installed copy of speechkit-narrate, False for a toolkit folder or checkout."""
    try:  # listing a folder can fail on an odd file system, and a yes-or-no question must not raise
        return any(REPO.glob("speechkit[-_]narrate-*.dist-info"))  # pip writes this folder beside every package it installs
    except OSError:  # the folder could not be listed
        return False  # answer "toolkit folder", which is what every caller assumed before this function existed


# Base folder for everything speechkit-narrate installs or caches; returns a Path and creates nothing.
# Precedence: SPEECHKIT_HOME, then %LOCALAPPDATA%\speechkit-narrate on Windows, then $XDG_DATA_HOME/speechkit-narrate or
# ~/.local/share/speechkit_narrate.
def _home() -> Path:
    if os.environ.get("SPEECHKIT_HOME"):  # an explicit override always wins
        return Path(os.environ["SPEECHKIT_HOME"])  # use it as given, even if the folder does not exist yet
    if WINDOWS:  # native Windows: per-user, non-roaming application data
        # LOCALAPPDATA is normally set; the fallback rebuilds the usual C:\Users\<name>\AppData\Local path
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "speechkit-narrate"
    # Linux and macOS follow the XDG base-directory convention
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "speechkit-narrate"


# Picks one location for an external component.
# env_key is the SPEECHKIT_* variable that overrides it; candidates are paths tried in order.
# Returns the override, else the first candidate that exists, else the last candidate (the normal install place).
def _first(env_key: str, *candidates) -> Path:
    """The env override, else the first candidate that exists, else the last candidate."""
    if os.environ.get(env_key):  # the environment variable is set and not empty
        return Path(os.environ[env_key])  # trust it without checking that it exists
    for c in candidates:  # try each candidate path in order
        if Path(c).exists():  # the first one present on this machine is used
            return Path(c)  # as a Path even if a string was given
    return Path(candidates[-1])  # nothing found: report the standard location so messages and setup point there


HOME = _home()  # resolved once per process
# The speechkit-offline-neural-tt checkout (has bootstrap.py and the voice profiles): normally the one this package
# sits in, so a fresh clone needs no setting at all.
SPEECHKIT = _first("SPEECHKIT_CHECKOUT", *([REPO] if (REPO / "speechkit" / "src" / "voice.py").exists() else []),
                   HOME / "speechkit-offline-neural-tt")
# the installed voice runtime (its own Python venv and voices/ folder); bootstrap.py install --out <checkout>/installed
# puts it in <checkout>/installed/runtime
RUNTIME = _first("SPEECHKIT_RUNTIME", SPEECHKIT / "installed" / "runtime", HOME / "runtime")
# the two sherpa-onnx speech recognizer models used by the pronunciation audit
ASR_MODELS = _first("SPEECHKIT_ASR_MODELS", HOME / "models")
# derived files; default ~/.cache/speechkit-narrate on Linux/macOS and HOME\cache on Windows
CACHE = Path(os.environ.get("SPEECHKIT_CACHE",
                            Path.home() / ".cache" / "speechkit-narrate" if not WINDOWS else HOME / "cache"))

# Download URLs of the two recognizer models, keyed by the folder name each archive unpacks to.
# setup() verifies complete archives and model members against the packaged lock.
ASR_URLS = {
    "sherpa-onnx-nemo-ctc-en-conformer-small":
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-ctc-en-conformer-small.tar.bz2",
    "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8":
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2",
}


# Path of the Python interpreter inside the voice runtime's virtual environment.
# Returns a Path; it is used to start the voice workers and the espeak helper with the runtime's own packages.
def runtime_python() -> Path:
    if WINDOWS:  # a Windows venv keeps python.exe in Scripts
        # shown for completeness; the runtime itself only installs on Linux
        return RUNTIME / ".venv" / "Scripts" / "python.exe"
    return RUNTIME / ".venv" / "bin" / "python"  # Linux and macOS layout


# The speechkit source folder handed to the voice worker as --speechkit-src.
def speechkit_src() -> Path:
    return SPEECHKIT / "speechkit" / "src"  # e.g. <checkout>/speechkit/src (contains voice.py)


# Finds a voice profile JSON file. name is either a profile name ("ljspeech-high") or a path to a .json file.
# Returns a Path; it is not checked for existence here, so a typo surfaces when the file is read.
def profile_path(name: str) -> Path:
    p = Path(name)  # treat the argument as a possible path first
    if p.suffix == ".json" and p.exists():  # an existing .json file is used directly (a custom profile)
        return p  # the speechkit checkout's profiles folder is not consulted at all
    return SPEECHKIT / "speechkit" / "profiles" / f"{name}.json"  # otherwise look in the checkout's profiles folder


# Reads a voice profile: model and config file names (relative to the runtime), sample rate, pacing and pins.
# name as for profile_path; returns the parsed dict.
def profile(name: str) -> dict:
    # UTF-8 explicitly, since Windows would otherwise use the ANSI code page
    return json.loads(profile_path(name).read_text(encoding="utf-8"))


# Absolute path of the voice's ONNX model inside the runtime, e.g. RUNTIME/voices/en_US-ljspeech-high.onnx.
def voice_model(name: str) -> Path:
    return RUNTIME / profile(name)["model"]  # the profile stores it relative to the runtime folder


# Absolute path of the voice's JSON config (phoneme map, sample rate) inside the runtime.
def voice_config(name: str) -> Path:
    return RUNTIME / profile(name)["config"]  # also relative to the runtime in the profile


# SHA-256 hex digest of a file, read in 1 MiB chunks so large models do not need to fit in memory.
# p is any path-like; returns a 64-character lowercase hex string.
def sha256(p) -> str:
    h = hashlib.sha256()  # a running SHA-256 state that is fed one block at a time
    with open(p, "rb") as f:  # binary mode: hash the exact bytes
        # iter(callable, sentinel) keeps reading 1 MiB blocks until read() returns b"" at end of file
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)  # feed each block into the hash
    return h.hexdigest()  # the finished digest as hex text


# Returns the "aligned" copy of a voice model, building it in the cache on first use. name is a profile name.
# The copy has the same weights but exposes one extra output, so the voice worker gets word timings for free.
# Side effects: may read and write a model file of the voice's size in CACHE/voices; needs the onnx package.
def aligned_model(name: str) -> Path:
    """Expose durations in a verified derived graph; never adopt an unchecked cache."""
    prof = profile(name)
    src = voice_model(name)
    source_hash = sha256(src)
    if source_hash != prof["modelSha256"]:
        raise RuntimeError("voice model hash differs from the selected profile")
    patch_version = "ceil-duration-output-v2"
    out = CACHE / "voices" / f"{src.stem}.{source_hash[:12]}.{patch_version}.aligned.onnx"
    receipt = out.with_suffix(out.suffix + ".json")
    if out.exists() and receipt.exists():
        try:
            record = json.loads(receipt.read_text(encoding="utf-8"))
            if (record["sourceSha256"] == source_hash and record["patchVersion"] == patch_version
                    and record["derivedSha256"] == sha256(out)):
                return out
        except (OSError, ValueError, KeyError):
            pass
    import onnx
    m = onnx.load(str(src))
    ceil = {o for n in m.graph.node if n.op_type == "Ceil" for o in n.output}
    if len(ceil) != 1:
        raise RuntimeError(f"expected one Ceil tensor in {src}, found {sorted(ceil)}")
    output_name = next(iter(ceil))
    if not any(o.name == output_name for o in m.graph.output):
        vi = onnx.helper.ValueInfoProto()
        vi.name = output_name
        m.graph.output.append(vi)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aligned-", dir=out.parent) as temp:
        tmp = Path(temp) / out.name
        onnx.save(m, str(tmp))
        record = {"sourceSha256": source_hash, "derivedSha256": sha256(tmp),
                  "patchVersion": patch_version, "durationOutput": output_name,
                  "onnxVersion": onnx.__version__}
        rec = Path(temp) / receipt.name
        rec.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(out)
        rec.replace(receipt)
    return out


def asr_lock() -> dict:
    return json.loads((PKG / "asr-models.lock.json").read_text(encoding="utf-8"))["models"]


def verify_asr_model(name: str, directory: Path | None = None) -> list[str]:
    """Return missing or changed pinned members; a tokens file alone is insufficient."""
    root = Path(directory) if directory is not None else ASR_MODELS / name
    issues = []
    spec = asr_lock()[name]
    members = spec.get("members")
    if members is None:
        receipt = root / ".speechkit-install.json"
        try:
            record = json.loads(receipt.read_text(encoding="utf-8"))
            members = record["members"]
            if (record["schemaVersion"] != "speechkit-asr-install-v1" or record["modelName"] != name
                    or record["archiveSha256"] != spec["sha256"] or record["archiveBytes"] != spec["bytes"]
                    or set(members) != set(spec["requiredMembers"]) or
                    any(not isinstance(v.get("bytes"), int) or v["bytes"] <= 0
                        or len(v.get("sha256", "")) != 64 for v in members.values())):
                raise ValueError("receipt does not match the pinned archive")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return ["missing or invalid verified-install receipt"]
    for rel, spec in members.items():
        member = root / rel
        if not member.is_file():
            issues.append(f"missing {rel}")
        elif member.stat().st_size != spec["bytes"] or sha256(member) != spec["sha256"]:
            issues.append(f"changed {rel}")
    return issues


def install_asr_model(name: str, log=print) -> None:
    """Verify the download and every required member before publishing a model folder."""
    spec = asr_lock()[name]
    target = ASR_MODELS / name
    if target.exists():
        issues = verify_asr_model(name)
        if not issues:
            return
        raise RuntimeError(f"invalid ASR model {target}: {issues}; move it aside before setup")
    ASR_MODELS.mkdir(parents=True, exist_ok=True)
    archive_cache = CACHE / "downloads"
    archive_cache.mkdir(parents=True, exist_ok=True)
    archive = archive_cache / f"{name}.{spec['sha256'][:12]}.tar.bz2"
    if archive.exists() and (archive.stat().st_size != spec["bytes"] or sha256(archive) != spec["sha256"]):
        raise RuntimeError(f"ASR archive hash mismatch: {archive}")
    if not archive.exists():
        with tempfile.TemporaryDirectory(prefix="download-", dir=archive_cache) as temp:
            tmp = Path(temp) / "archive.tar.bz2"
            log(f"downloading pinned ASR model {name}")
            with urllib.request.urlopen(spec["url"], timeout=60) as response, tmp.open("wb") as dest:
                shutil.copyfileobj(response, dest)
            if tmp.stat().st_size != spec["bytes"] or sha256(tmp) != spec["sha256"]:
                raise RuntimeError(f"downloaded ASR archive hash mismatch: {name}")
            tmp.replace(archive)
    with tempfile.TemporaryDirectory(prefix="unpack-", dir=ASR_MODELS) as temp:
        stage = Path(temp)
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                relative = Path(member.name)
                if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                        or relative.parts[0] != name or not (member.isfile() or member.isdir())):
                    raise RuntimeError(f"unsafe ASR archive member: {member.name}")
            bundle.extractall(stage, filter="data")
        if "members" not in spec:
            members = {}
            for rel in spec["requiredMembers"]:
                file = stage / name / rel
                if not file.is_file() or file.stat().st_size <= 0:
                    raise RuntimeError(f"required ASR archive member missing: {name}/{rel}")
                members[rel] = {"bytes": file.stat().st_size, "sha256": sha256(file)}
            receipt = {"schemaVersion": "speechkit-asr-install-v1", "modelName": name, "archiveSha256": spec["sha256"],
                       "archiveBytes": spec["bytes"], "members": members}
            (stage / name / ".speechkit-install.json").write_text(
                json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        issues = verify_asr_model(name, stage / name)
        if issues:
            raise RuntimeError(f"ASR member verification failed: {name}: {issues}")
        (stage / name).replace(target)


# Python modules only the Linux voice/audit side needs: speech recognition, Praat acoustics, ONNX editing
VOICE_MODULES = ("sherpa_onnx", "parselmouth", "onnx", "scipy")
# Python modules every machine needs: audio arithmetic and the word tools of the script builder; mcp is optional
# (only `mcp` needs it) and wordfreq improves the glued-word checks of the text reader
CORE_MODULES = ("numpy", "cmudict", "num2words")
OPTIONAL_MODULES = ("wordfreq", "mcp")
# Maps an import name to its pip package name where they differ and pins the minimum MCP SDK version.
PIP_NAMES = {"cmudict": "cmudict", "num2words": "num2words", "sherpa_onnx": "sherpa-onnx",
             "parselmouth": "praat-parselmouth", "onnx": "onnx", "wordfreq": "wordfreq", "mcp": "mcp>=2.3",
             "numpy": "numpy", "scipy": "scipy"}


# Reports whether the voice runtime can run in this process: only on Linux (including inside WSL).
# Returns a bool; SPEECHKIT_PLATFORM can force the answer through registry.current_platform().
def voice_here() -> bool:
    """Can the Linux-only voice runtime run on this machine?"""
    from .registry import current_platform  # imported here to avoid a circular import at module load
    return current_platform() == "linux"  # the runtime ships Linux x86-64 wheels only


# One (name, ok, detail) row per Python module in mods; optional modules are reported as ok with a note.
def _module_rows(mods, optional=False) -> list[tuple[str, bool, str]]:
    out = []
    for mod in mods:
        try:  # importing is the only reliable check (a module may be installed but broken)
            __import__(mod)
            out.append((f"python: {mod}", True, "importable"))
        except Exception as e:  # noqa: BLE001
            msg = str(e).splitlines()[0][:100] if str(e) else type(e).__name__
            out.append((f"python: {mod}", optional, ("optional, not installed: " if optional else "") + msg))
    return out


# The `doctor` checks: one (name, ok, detail) tuple per thing the narration needs on this machine.
# Covers Python, the Python modules, ffmpeg (needed only for mp3/m4a export), and the voice runtime or its WSL
# delegation. Imports each module, so it can take a second or two.
def checks() -> list[tuple[str, bool, str]]:
    """(name, ok, detail) for everything the narration needs on this machine."""
    out = [("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])]
    ff = shutil.which("ffmpeg")  # only the mp3 and m4a export formats need it
    out.append(("ffmpeg", True, ff or "not on PATH (optional: needed only for export.format mp3 or m4a)"))
    out += _module_rows(CORE_MODULES + (VOICE_MODULES if voice_here() else ()))
    out += _module_rows(OPTIONAL_MODULES, optional=True)
    delegate = os.environ.get("SPEECHKIT_DELEGATE")  # how Windows reaches the Linux voice side, if set
    if voice_here():  # the voice side is only checked where it can run
        # the speechkit checkout is recognised by its voice.py adapter
        out.append(("speechkit checkout", (SPEECHKIT / "speechkit" / "src" / "voice.py").exists(), str(SPEECHKIT)))
        # the runtime is recognised by its venv's Python
        out.append(("speechkit runtime", runtime_python().exists(), str(RUNTIME)))
        try:  # reading the profile can fail if the checkout is missing
            vm = voice_model("ljspeech-high")  # the default voice profile's model file
            out.append(("voice model", vm.exists(), str(vm)))
        except Exception as e:  # noqa: BLE001
            out.append(("voice model", False, str(e)[:80]))
        for name in ASR_URLS:
            issues = verify_asr_model(name)
            out.append((f"asr model {name}", not issues, "; ".join(issues) or "all pinned members verified"))
        machine = platform.machine().lower()
        out.append(("voice runtime architecture", machine in ("x86_64", "amd64"), machine))
        if runtime_python().exists():
            try:
                probe = subprocess.run([str(runtime_python()), "-I", "-c",
                                        "import sys;print('.'.join(map(str,sys.version_info[:2])))"],
                                       capture_output=True, text=True, encoding="utf-8", timeout=15)
                out.append(("voice runtime Python", probe.returncode == 0 and probe.stdout.strip() == "3.13",
                            probe.stdout.strip() or probe.stderr.strip()))
            except (OSError, subprocess.TimeoutExpired) as exc:
                out.append(("voice runtime Python", False, str(exc)))
    else:  # not Linux (normally Windows)
        out.append(("voice runtime (Linux-only)", bool(delegate),  # ok when delegation to WSL is configured
                    f"delegated: prevoice/narrate/autofix/audit/speak run through SPEECHKIT_DELEGATE={delegate!r}"
                    if delegate else "not available here; set SPEECHKIT_DELEGATE to \"wsl -d Ubuntu-24.04 --exec\" "
                                     "(PowerShell: Set-NarrationBackend Auto)"))
    return out


# The `setup` command: installs what is missing on this machine and logs each step through log (a print-like function).
# Python packages everywhere; on Linux also the speechkit runtime (through the checkout's own verified bootstrap), the
# two recognizer models and the aligned copy of the default voice. Network access needed.
def setup(log=print) -> None:
    """Install what is missing: Python packages, and on Linux the speechkit runtime via its own verified bootstrap
    plus the recognizer models. On Windows the voice side is installed inside WSL instead
    (PowerShell: Install-NarrationDependency -Wsl)."""
    missing = []  # pip package names to install
    for mod in CORE_MODULES + OPTIONAL_MODULES + (VOICE_MODULES if voice_here() else ()):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            missing.append(PIP_NAMES.get(mod, mod))  # map to the pip name (parselmouth to praat-parselmouth, ...)
    if missing:  # only call pip when something is missing
        log("pip install " + " ".join(missing))
        if sys.prefix == sys.base_prefix:
            raise RuntimeError("setup needs missing packages; create a project virtual environment and install requirements-voice.txt")
        constraints = REPO / "constraints-voice.txt"
        args = ["-c", str(constraints)] if constraints.exists() else []
        subprocess.run([sys.executable, "-m", "pip", "install", *args, *missing], check=True)
    if not shutil.which("ffmpeg"):  # not installed automatically: it is a system program
        log("ffmpeg: not on PATH; install it for mp3 or m4a export (Windows: winget install Gyan.FFmpeg)")
    if not voice_here():  # the voice runtime cannot be installed outside Linux
        log("voice runtime: Linux-only; on Windows install it inside WSL (Install-NarrationDependency -Wsl) and "
            "set SPEECHKIT_DELEGATE (Set-NarrationBackend Auto)")
        return
    if platform.machine().lower() not in ("x86_64", "amd64"):
        raise RuntimeError("the supplied voice runtime requires Linux x86_64")
    if not runtime_python().exists():  # install the runtime only when its venv Python is missing
        boot = SPEECHKIT / "bootstrap.py"  # the checkout's verified installer
        if not boot.exists():
            raise RuntimeError(f"speechkit checkout not found at {SPEECHKIT}; clone "
                               "https://github.com/EMBLEM-NLP/speechkit-offline-neural-tt there or set "
                               "SPEECHKIT_CHECKOUT")
        py = shutil.which("python3.13") or sys.executable  # the runtime needs CPython 3.13
        # fetch the pinned archive, verify all hashes, install into a NEW folder (the installer creates <out>/runtime)
        for step in (["fetch"], ["verify"], ["install", "--out", str(RUNTIME.parent)]):
            log("speechkit bootstrap " + " ".join(step))
            subprocess.run([py, str(boot), *step], cwd=SPEECHKIT, check=True)
    for name in ASR_URLS:
        install_asr_model(name, log)
    # build the aligned copy of the default voice now, so the first narration does not pay for it
    log(f"aligned voice model: {aligned_model('ljspeech-high')}")
