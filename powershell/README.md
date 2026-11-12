# SpeechKitVoiceTtsOfflinePipeline PowerShell module

`SpeechKitNarrate` remains a compatibility module forwarding to `SpeechKitVoiceTtsOfflinePipeline`; the existing `*-Narration*` commands are preserved. See [the migration guide](../docs/RENAMING.md).

PowerShell commands for **speechkit-voice-tts-offline-pipeline**, the narration toolkit in this checkout: make a project from a
`.txt`, `.md` or SpeechKit request `.json`, bring its stages up to date (extract, check, script, prevoice,
narrate, autofix, audit, report, export), run the standalone tools (`speak`, `quick`), and register the MCP
server. Every command calls `python -m speechkit_narrate --json ...` and returns typed objects.

The module was refactored from the Bookcast module; only its narration part was kept.

It is written for Windows PowerShell 5.1 and PowerShell 7.2+ and tested with Pester on PowerShell 7.4 (Linux;
Windows is simulated in the tests and Windows PowerShell 5.1 is checked for syntax only, so a first run on your
machine is the real Windows check). Licensed under GPL-3.0-or-later.

## Installation

The module lives in the checkout and is imported from there. It finds the toolkit two folders up (the checkout
root, which holds `speechkit_narrate\`), so nothing has to be copied or installed.

```powershell
# 1. Get the checkout (any folder will do)
git clone https://github.com/EMBLEM-NLP/speechkit-voice-tts-offline-pipeline C:\src\speechkit-voice-tts-offline-pipeline

# 2. Allow local scripts (once per user); unblock the files if you downloaded a zip instead of cloning
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
Get-ChildItem C:\src\speechkit-voice-tts-offline-pipeline -Recurse | Unblock-File

# 3. Import the module
Import-Module C:\src\speechkit-voice-tts-offline-pipeline\powershell\SpeechKitVoiceTtsOfflinePipeline

# 4. See what it found
Get-NarrationBackend
Get-NarrationStage
```

To load it in every session:

```powershell
Add-Content -Path $PROFILE -Value 'Import-Module C:\src\speechkit-voice-tts-offline-pipeline\powershell\SpeechKitVoiceTtsOfflinePipeline'
```

`Get-NarrationBackend` shows the `Mode`. In **Source mode** (the normal case) the module sits in the checkout
and puts it on `PYTHONPATH`. If you move the module elsewhere, set `$env:SPEECHKIT_ROOT` to the checkout before
importing, or run `Set-NarrationBackend -ToolkitRoot C:\src\speechkit-voice-tts-offline-pipeline`. With no checkout at all
the module is in **Installed mode**: the Python it uses must have the package installed
(`python -m pip install C:\src\speechkit-voice-tts-offline-pipeline`).

## Windows + WSL backend setup

The voice runtime is Linux x86-64 only. On Windows the module runs Python natively and sends the Linux-only
stages (`prevoice`, `narrate`, `autofix`, `audit`) and the `speak` tool to WSL.

```powershell
# 1. Show every install step without running anything
Install-NarrationDependency -Wsl -WhatIf

# 2. Install: winget Python 3.13 and FFmpeg, the Python packages (requirements.txt), WSL Ubuntu-24.04, and inside it
#    Python 3.13, a venv in /opt/speechkit-narrate/venv with requirements-voice.txt, the voice runtime and the
#    recognizer models
Install-NarrationDependency -Wsl

# 3. Open a new PowerShell window (PATH changed), import again, and check both sides
Import-Module C:\src\speechkit-voice-tts-offline-pipeline\powershell\SpeechKitVoiceTtsOfflinePipeline
Set-NarrationBackend -Backend Auto -Distribution Ubuntu-24.04 -WslPython '/opt/speechkit-narrate/venv/bin/python'
Test-NarrationEnvironment | Where-Object { -not $_.Ok }
```

`-Wsl` gives `/opt/speechkit-narrate` to your first WSL user (uid 1000), so later `pip` updates there need no
`sudo`. The voice runtime is installed by the toolkit's own `setup` into `<checkout>\installed\runtime`
(`python bootstrap.py install --out <checkout>\installed`). To keep the backend settings, add the
`Set-NarrationBackend` line to your profile:

```powershell
Add-Content -Path $PROFILE -Value "Set-NarrationBackend -Backend Auto -Distribution Ubuntu-24.04 -WslPython '/opt/speechkit-narrate/venv/bin/python'"
```

On Linux, `Install-NarrationDependency` runs `python -m speechkit_narrate setup` (Python packages, voice runtime,
recognizer models); on macOS the same step installs the Python packages only.

### Backends

| Backend | What runs where |
|---|---|
| `Auto` (default) | Python on this machine. On Windows the Linux-only stages and the `speak` tool are delegated to WSL through `SPEECHKIT_DELEGATE`, which the module sets. |
| `Native` | Python on this machine only. Linux-only stages show as `blocked` in the plan on Windows. |
| `Wsl` | Everything inside WSL (`wsl.exe -d <distro> --exec env PYTHONPATH=... python3 -m speechkit_narrate ...`). Every path is converted with `wslpath`, and paths in the results are mapped back. |

Module settings (read when the module is imported; `Set-NarrationBackend` changes them for the session):
`$env:SPEECHKIT_BACKEND`, `$env:SPEECHKIT_PYTHON` (native Python, e.g. `'py -3.13'`; an explicit choice always
wins over discovery), `$env:SPEECHKIT_WSL_PYTHON`, `$env:SPEECHKIT_WSL_DISTRO`, `$env:SPEECHKIT_ROOT` (the
checkout) and `$env:SPEECHKIT_WSL` (replaces the whole `wsl.exe ... --exec` launcher).

Read by the Python side and passed through: `SPEECHKIT_DELEGATE`, `SPEECHKIT_DELEGATE_ROOT`,
`SPEECHKIT_DELEGATE_PYTHON`, `SPEECHKIT_DELEGATE_PATHMAP`, `SPEECHKIT_PLUGINS`, `SPEECHKIT_PLATFORM`,
`SPEECHKIT_HOME`, `SPEECHKIT_CACHE`, `SPEECHKIT_CHECKOUT`, `SPEECHKIT_RUNTIME`, `SPEECHKIT_ASR_MODELS`,
`SPEECHKIT_WINPATHS` and `SPEECHKIT_WINPATHS_ROOT`. Variables for commands that run inside WSL go through
`Set-NarrationBackend -WslEnvironment @{ SPEECHKIT_ASR_MODELS = '/home/me/models' }`.

## First project

```powershell
New-NarrationProject -Path .\projects\my-text -InputFile .\MyText.md -Title 'My Text' -Author 'A. Writer'
Invoke-Narration .\projects\my-text -WhatIf          # the plan: what would run and why
Invoke-Narration .\projects\my-text                  # extracts, then stops so you can review work\book.md
notepad .\projects\my-text\work\book.md
Invoke-Narration .\projects\my-text -Verbose         # the rest of the pipeline, with the log on screen
Get-NarrationProject .\projects\my-text              # stages done, narration progress, exported files
```

Settings at creation time (`-Set` takes a hashtable or JSON text and maps to `--set`), later changes, and single
stages with their own options:

```powershell
New-NarrationProject .\projects\guide .\guide.md -Set @{ spoken = @{ code_names = $true }; export = @{ format = 'mp3' } }
Invoke-NarrationPipeline .\projects\guide -Target script -AcceptDraft
Set-NarrationSetting .\projects\guide @{ spoken = @{ years = $true } }
Get-NarrationPlan .\projects\guide -Target script               # script now shows as "run"
Invoke-NarrationStage .\projects\guide narrate -Workers 4
Invoke-NarrationStage .\projects\guide autofix -Takes 5 -Retakes 2 -Verbose
```

Stage names tab-complete (including the project's plug-in stages), and each stage's options become parameters of
`Invoke-NarrationStage` once `-Name` is given.

## Standalone tools

Each tool has its own command, generated from the toolkit when the module is imported:

```powershell
Get-NarrationTool
Invoke-NarrationSpeak -Text 'Hello there. This is a test.' -Out .\hello.wav
Get-Item .\chapter.md | Invoke-NarrationSpeak -Out .\chapter.mp3          # -TextFile from the pipeline
Invoke-NarrationQuick -InputPath .\MyText.md -Out .\projects\my-text -Targets export
```

Plug-in tools: set `$env:SPEECHKIT_PLUGINS` (paths separated by `;`) and run `Update-NarrationCommand`.

## MCP server

```powershell
Register-NarrationMcpServer        # adds "speechkit-voice-tts-offline-pipeline" to Claude Desktop's config (backed up first)
Start-NarrationMcpServer           # or run the server on this console's stdin/stdout
Start-NarrationMcpServer -WhatIf   # shows the exact command line it would start, and starts nothing
```

Restart Claude Desktop after registering. With `Set-NarrationBackend -Backend Wsl` the whole server runs inside
WSL. Paths you give it can then be Windows paths (`C:\Users\you\text.md`, `\\wsl$\Ubuntu\home\you\text.md`): the
server converts them to Linux paths and converts the paths in its results back (`SPEECHKIT_WINPATHS` set to
`auto`, `on` or `off` chooses when; `SPEECHKIT_WINPATHS_ROOT` names the folder that holds the drive folders if it
is not `/mnt`).

## Commands

| Command | What it does |
|---|---|
| `Get-NarrationBackend` | how the toolkit is run: backend, mode, Python, WSL and delegation |
| `Set-NarrationBackend` | choose the backend, Python, WSL distribution or checkout for the session |
| `Get-NarrationStage` | the stages: needs, inputs, outputs, platforms, options, where they run |
| `Invoke-NarrationStage` | run one stage now (after anything stale it needs) |
| `Get-NarrationTool` | the standalone tools and their generated commands |
| `Invoke-NarrationTool` | run a tool with a hashtable of parameters |
| `Update-NarrationCommand` | regenerate the per-tool commands (after adding plug-ins) |
| `New-NarrationProject` | make a project folder (`-Set` for extra settings) |
| `Get-NarrationProject` | what is done in a project |
| `Get-NarrationPlan` | what a run would do, and why |
| `Invoke-NarrationPipeline` (alias `Invoke-Narration`) | bring a project's stages up to date |
| `Get-NarrationSetting` / `Set-NarrationSetting` | read or deep-merge `project.json` |
| `Test-NarrationEnvironment` | the toolkit's `doctor`, natively and in WSL |
| `Install-NarrationDependency` | install Python, ffmpeg, the packages, and optionally WSL with the voice runtime |
| `Start-NarrationMcpServer` / `Register-NarrationMcpServer` | run or register the MCP server |
| `Invoke-NarrationSpeak`, `Invoke-NarrationQuick`, ... | one generated command per tool |

`Get-Help <command> -Examples` shows examples for each.

## Tests

```powershell
Invoke-Pester .\powershell\Tests -Output Detailed
```

The tests need Pester 5 or later and a Python 3.11+ with the packages from `requirements.txt`; they do not need
the voice runtime. WSL is simulated with `tests\narration\fixtures\fake_wsl.py`.
