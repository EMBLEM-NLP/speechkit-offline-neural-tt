# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# What Install-NarrationDependency runs, as a list of steps (so -WhatIf and the tests can show it).

# Windows programs installed through winget: package id (exact) mapped to the description shown in -WhatIf and the
# step objects. Ordered so the installs always run in the same sequence.
$script:WingetPackages = [ordered]@{
    'Python.Python.3.13' = 'Python 3.13'  # the interpreter that runs the toolkit natively
    # audio encoding for the export stage's mp3 and m4a formats (wav needs nothing)
    'Gyan.FFmpeg'        = 'FFmpeg (ffmpeg, for mp3 and m4a export)'
}
# Python packages come from the checkout's requirements files: requirements.txt (every platform) and
# requirements-voice.txt (requirements.txt plus the Linux-only voice/audit packages, used inside WSL). Installed mode
# has no checkout, so it has nothing to install them from; the package and its requirements are then installed with
# pip by hand.
# placeholder in WSL steps, replaced at run time with the toolkit folder as WSL sees it
$script:WslToolkitToken = '<toolkit-in-wsl>'
# Ubuntu packages for the voice runtime inside WSL: Python 3.13 with venv and headers (the runtime is CPython 3.13
# only), ffmpeg for mp3/m4a export, plus git and curl for the setup scripts.
$script:AptPackages = @('python3.13', 'python3.13-venv', 'python3.13-dev', 'ffmpeg', 'git', 'curl')
# the folder inside WSL that holds the virtual environment with the voice packages; Wsl.ps1 probes for its python
$script:WslHome = '/opt/speechkit-narrate'
# winget's exit code when the package is already installed, written as a signed 32-bit number; it counts as success
# so running the installer twice is not reported as a failure.
$script:WingetAlreadyInstalled = -1978335189   # 0x8A15002B APPINSTALLER_CLI_ERROR_PACKAGE_ALREADY_INSTALLED

# Describes one install step without running it. Parameters: -Name (short label), -Component (which group it
# belongs to), -Description, -FilePath and -ArgumentList (the program and its arguments), -Environment (extra
# variables), -SuccessCodes (exit codes that mean success), -Script (a script block run in this session instead of a
# program), -UsesPython (FilePath is replaced by the real Python at run time) and -Distribution (the WSL
# distribution). Returns a [pscustomobject] that also carries the display command line.
function New-NarrationInstallStep {
    # PSScriptAnalyzer asks for -WhatIf on New-* functions; this one only describes a step and runs nothing (the
    # public Install-NarrationDependency supports -WhatIf), so it is exempt
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Describes a step in memory; Install-NarrationDependency supports -WhatIf')]  # why it is waived
    param(  # named parameters describing one step; every one is optional
        # label, group, description, program and arguments
        [string]$Name, [string]$Component, [string]$Description, [string]$FilePath, [string[]]$ArgumentList = @(),
        # environment, accepted exit codes, an in-session script block and the Python flag
        [hashtable]$Environment = @{}, [int[]]$SuccessCodes = @(0), [scriptblock]$Script, [switch]$UsesPython,
        [string]$Distribution  # the WSL distribution name, empty for Windows steps
    )
    [pscustomobject]@{  # build the step object that Install-NarrationDependency later runs, or shows under -WhatIf
        Name         = $Name  # label shown in the results table
        Component    = $Component  # component name such as Winget, Pip or WslSetup
        Description  = $Description  # what the step installs, for ShouldProcess messages
        FilePath     = $FilePath  # the program to run, e.g. winget, wsl.exe or the Python placeholder
        ArgumentList = $ArgumentList  # the program's arguments, each kept as a separate string
        Environment  = $Environment  # variables set for that program only
        SuccessCodes = $SuccessCodes  # exit codes treated as success
        Script       = $Script  # script block run inside PowerShell, used for the final Set-NarrationBackend step
        UsesPython   = [bool]$UsesPython  # true when FilePath should be swapped for the resolved Python
        Distribution = $Distribution  # WSL distribution the step works on
        # the command line shown to the user: the script's source, or the program and its quoted arguments
        CommandLine  = $(if ($Script) { $Script.ToString().Trim() } else { ConvertTo-NarrationArgumentString -ArgumentList (@($FilePath) + $ArgumentList) })
    }
}

# Returns the path of the real wsl.exe (System32 on Windows) or just 'wsl.exe'. Unlike the WSL launcher used for
# running the toolkit, it ignores $env:SPEECHKIT_WSL, because installing must talk to WSL itself. No parameters.
function Get-NarrationWslExe {
    if ($script:OnWindows -and $env:SystemRoot) {  # only Windows has a System32 folder
        $full = Join-Path $env:SystemRoot 'System32\wsl.exe'  # the copy Windows installs
        if (Test-Path -LiteralPath $full) { return $full }  # use it when present
    }
    'wsl.exe'  # otherwise rely on PATH
}

# Lists the install steps for a platform and set of components, without running anything, so -WhatIf and the tests
# can show them. -Platform (Windows, Linux or MacOS; detected when omitted), -Component (which groups; per-platform
# defaults when omitted), -Wsl (adds the three WSL groups, but only to those defaults), -Distribution (WSL
# distribution, default Ubuntu-24.04), -PythonCommand (Python shown in the command lines). Returns an array of step
# objects in run order. Throws when Pip or WslPackages is asked for in Installed mode, which has no checkout and so no
# requirements files to install from.
function Get-NarrationInstallStep {
    param(  # options choosing the platform, the component groups and the WSL details
        # the target platform; ValidateSet rejects anything else
        [ValidateSet('Windows', 'Linux', 'MacOS')][string]$Platform,
        [string[]]$Component,  # which component groups to include
        [switch]$Wsl,  # also prepare WSL for the Linux-only stages
        [string]$Distribution = 'Ubuntu-24.04',  # the distribution to install and configure
        [string]$PythonCommand = '<python>'  # only for display; the real Python is resolved when the step runs
    )
    if (-not $Platform) {  # detect the platform when not given
        if ($script:OnWindows) { $Platform = 'Windows' }  # Windows by the module's own check
        # $IsMacOS exists only in PowerShell 6 and later, so the version is checked first
        elseif ($PSVersionTable.PSVersion.Major -ge 6 -and $IsMacOS) { $Platform = 'MacOS' }
        else { $Platform = 'Linux' }  # anything else is treated as Linux
    }
    $root = $script:Config.ToolkitRoot  # the checkout in Source mode, $null in Installed mode
    if (-not $Component) {  # no explicit list: choose the defaults
        # Windows: Python and ffmpeg, then the Python packages (the voice runtime cannot run on Windows itself)
        if ($Platform -eq 'Windows') { $Component = @('Winget', 'Pip') }
        # Linux and macOS: the toolkit's own setup command, which installs the Python packages itself (and, on Linux,
        # the voice runtime and the recognizer models)
        else { $Component = @('Setup') }
        # with -Wsl add WSL itself, its packages and the toolkit setup there
        if ($Wsl) { $Component += @('WslInstall', 'WslPackages', 'WslSetup') }
    }
    # Pip and WslPackages install the checkout's requirements files, so Installed mode cannot plan them.
    if (-not $root -and @($Component | Where-Object { @('Pip', 'WslPackages') -contains $_ }).Count) {
        throw (@(  # the message is made of the lines below
            'This module has no toolkit folder (Installed mode), so there are no requirements files to install the'
            'Python packages from. Point it at the speechkit-voice-tts-offline-pipeline checkout with'
            "Set-NarrationBackend -ToolkitRoot 'C:\path\to\speechkit-voice-tts-offline-pipeline', or install the package"
            'with pip yourself (python -m pip install <checkout>) and choose that Python with Set-NarrationBackend -Python.'
        ) -join ' ')  # the pieces make one sentence, joined by single spaces
    }
    $steps = New-Object System.Collections.Generic.List[object]  # the steps in run order
    # the Winget group was chosen: add one install step per Windows program in the table
    if ($Component -contains 'Winget') {
        foreach ($id in $script:WingetPackages.Keys) {  # one step per package, in the table's order
            # winget install with the exact id from the winget source, accepting the license prompts up front, and
            # treating "already installed" as success (the argument list continues on the next lines).
            $steps.Add((New-NarrationInstallStep -Name "winget $id" -Component 'Winget' -Description $script:WingetPackages[$id] `
                        -FilePath 'winget' -ArgumentList @('install', '--id', $id, '--exact', '--source', 'winget',  # exact id match, and only from the winget source (not the Microsoft Store)
                        '--accept-package-agreements', '--accept-source-agreements') `
                        -SuccessCodes @(0, $script:WingetAlreadyInstalled)))  # already installed counts as success
        }
    }
    if ($Component -contains 'Pip') {  # Python packages through pip (Source mode only, checked above)
        # Upgrade pip first, since old versions mis-handle some packages (continued on the next line).
        $steps.Add((New-NarrationInstallStep -Name 'pip upgrade' -Component 'Pip' -Description 'pip itself' -UsesPython `
                    -FilePath $PythonCommand -ArgumentList @('-m', 'pip', 'install', '--upgrade', 'pip')))  # python -m pip runs the pip of exactly this Python
        $req = 'requirements.txt'  # on Windows the regular requirements
        # elsewhere the voice requirements, which include the regular ones
        if ($Platform -ne 'Windows') { $req = 'requirements-voice.txt' }
        $reqPath = Join-Path $root $req  # the requirements file's full path, which pip is told to read
        # Then the toolkit's requirements (continued on the next line).
        $steps.Add((New-NarrationInstallStep -Name 'pip packages' -Component 'Pip' -UsesPython `
                    -Description "the Python packages speechkit-voice-tts-offline-pipeline uses ($req)" `
                    -FilePath $PythonCommand `
                    -ArgumentList @('-m', 'pip', 'install', '--upgrade', '-r', $reqPath)))  # install what it lists
    }
    if ($Component -contains 'Setup') {  # the toolkit's own setup command, the default on Linux and macOS
        $setupEnv = @{ PYTHONUTF8 = '1' }  # avoids locale encoding trouble
        if ($root) { $setupEnv['PYTHONPATH'] = $root }  # Source mode: makes the speechkit_narrate package importable
        # setup installs the missing Python packages everywhere; the voice runtime and the recognizer models only on
        # Linux, because the runtime is Linux-only (on macOS it says so and stops after the packages)
        $steps.Add((New-NarrationInstallStep -Name 'speechkit-voice-tts-offline-pipeline setup' -Component 'Setup' `
                    -Description 'the Python packages, and on Linux the voice runtime and the recognizer models' -UsesPython `
                    -FilePath $PythonCommand -ArgumentList @('-m', 'speechkit_narrate', 'setup') `
                    -Environment $setupEnv))  # the variables built above
    }
    $wslExe = Get-NarrationWslExe  # the real wsl.exe for all WSL steps
    $venv = "$($script:WslHome)/venv"  # the virtual environment inside WSL that holds the voice packages
    $vpy = "$venv/bin/python"  # the venv's interpreter, which runs pip and speechkit_narrate inside WSL
    if ($Component -contains 'WslInstall') {  # install WSL and the distribution
        # wsl --install -d <distro>; Install-NarrationDependency skips it when the distribution already exists
        # (continued below).
        $steps.Add((New-NarrationInstallStep -Name "wsl install $Distribution" -Component 'WslInstall' -Distribution $Distribution `
                    -Description "WSL with $Distribution (skipped when it is already installed; may ask for a restart and a Linux user name)" `
                    -FilePath $wslExe -ArgumentList @('--install', '-d', $Distribution)))  # the install command itself
    }
    if ($Component -contains 'WslPackages') {  # packages inside WSL, run as root
        # The root shell script, one command per line: stop at the first error (set -e), keep apt from asking questions,
        # add the deadsnakes PPA because Ubuntu 24.04 does not ship Python 3.13, install the apt packages, create the
        # venv only if missing, install the checkout's voice requirements into it (the token is replaced later by the
        # checkout as WSL sees it), and finally give the folder to uid 1000 (the first WSL user) so later pip updates
        # need no sudo.
        $rootScript = @(
            'set -e'
            'export DEBIAN_FRONTEND=noninteractive'
            'apt-get update'
            'apt-get install -y software-properties-common'
            'add-apt-repository -y ppa:deadsnakes/ppa'
            'apt-get update'
            "apt-get install -y $($script:AptPackages -join ' ')"
            "mkdir -p $($script:WslHome)"
            "[ -x $vpy ] || python3.13 -m venv $venv"
            "$vpy -m pip install --upgrade pip"
            "$vpy -m pip install --upgrade -r '$($script:WslToolkitToken)/requirements-voice.txt'"
            "chown -R 1000:1000 $($script:WslHome)"
        ) -join "`n"  # lines joined with LF, the only line end bash accepts
        # what the step does, as the list of steps shows it
        $wslDescription = "as root in WSL: Python 3.13 (deadsnakes), ffmpeg, a venv in $venv with requirements-voice.txt"
        # the step itself: bash runs the script above as root inside WSL
        $steps.Add((New-NarrationInstallStep -Name 'wsl packages' -Component 'WslPackages' -Distribution $Distribution `
                    -Description $wslDescription `
                    -FilePath $wslExe -ArgumentList @('-d', $Distribution, '-u', 'root', '--exec', 'bash', '-c', $rootScript)))  # -u root runs as the superuser; --exec bash -c hands the whole script to bash without another shell in between
    }
    if ($Component -contains 'WslSetup') {  # the toolkit setup inside WSL
        # env sets PYTHONUTF8 and, in Source mode, PYTHONPATH to the toolkit's WSL path (replaced at run time) before
        # starting the venv python; in Installed mode that python must already have the package.
        $setupArgs = @('-d', $Distribution, '--exec', 'env')  # the launcher words and the env program
        if ($root) { $setupArgs += "PYTHONPATH=$($script:WslToolkitToken)" }  # Source mode only
        $setupArgs += @('PYTHONUTF8=1', $vpy, '-m', 'speechkit_narrate', 'setup')  # then the command itself
        # Downloads the voice runtime and the recognizer models; the description names the python that really runs.
        $steps.Add((New-NarrationInstallStep -Name 'wsl speechkit-voice-tts-offline-pipeline setup' -Component 'WslSetup' -Distribution $Distribution `
                    -Description "the voice runtime and recognizer models inside WSL ($vpy -m speechkit_narrate setup)" `
                    -FilePath $wslExe -ArgumentList $setupArgs))  # the arguments built above
        # Last step: point this session at the new WSL setup by running Set-NarrationBackend in PowerShell itself
        # (continued on the next line).
        $steps.Add((New-NarrationInstallStep -Name 'use wsl' -Component 'WslSetup' -Distribution $Distribution `
                    -Description "this PowerShell session (add the same line to `$PROFILE to keep it)" `
                    -Script ([scriptblock]::Create("Set-NarrationBackend -Distribution '$Distribution' -WslPython '$vpy'"))))  # a script block with the distribution and venv python written into it
    }
    , $steps.ToArray()  # return the steps as one array
}

# Replaces the <toolkit-in-wsl> placeholder in a WSL step's arguments with the toolkit folder as WSL sees it.
# -ArgumentList is the step's arguments, -FilePath the program that reaches WSL (wsl.exe or its stand-in),
# -Distribution the distribution whose wslpath translates the path and -Cache a hashtable that remembers the answer
# from step to step. Returns the arguments with the placeholder replaced; arguments without it come back unchanged,
# and no process is started when it does not occur. Inside a `bash -c` script the path is made safe for single
# quotes. Throws when there is no toolkit folder or wslpath cannot translate it.
function Expand-NarrationWslToken {
    # the arguments to rewrite, the program that will run them, the distribution and the answers kept so far
    param([string[]]$ArgumentList, [string]$FilePath, [string]$Distribution, [hashtable]$Cache)
    $token = $script:WslToolkitToken  # the one placeholder
    # nothing to do when no argument mentions the placeholder
    if (-not @($ArgumentList | Where-Object { ([string]$_).Contains($token) }).Count) { return $ArgumentList }
    if (-not $Cache.ContainsKey($token)) {  # the WSL path is not known yet
        $windows = $script:Config.ToolkitRoot  # the Windows path to translate
        if (-not $windows) { throw "speechkit-voice-tts-offline-pipeline: the path for $token is not known in this session." }  # Installed mode
        # ask wslpath inside the target distribution for the Linux path
        $w = Invoke-NarrationProcess -FilePath $FilePath -TimeoutSeconds 120 -Environment @{ WSL_UTF8 = '1' } `
            -ArgumentList @('-d', $Distribution, '--exec', 'wslpath', '-a', $windows)  # absolute Linux path wanted
        # the first non-blank output line
        $first = @($w.StdOut -split "`r?`n" | Where-Object { $_.Trim() }) | Select-Object -First 1
        # a failed translation stops this step
        if ($w.ExitCode -ne 0 -or -not $first) {
            throw "wslpath could not translate $($windows): $($w.StdErr.Trim())"  # with wslpath's own complaint
        }
        $Cache[$token] = $first.Trim()  # remember it for later steps
    }
    $value = $Cache[$token]  # the Linux path for the placeholder
    # is the placeholder inside a bash -c script? There the path sits in single quotes, so any quote in it becomes '\''
    # (close, escaped quote, reopen)
    if (($ArgumentList -contains 'bash') -and ($ArgumentList -contains '-c')) { $value = $value.Replace("'", "'\''") }
    @($ArgumentList | ForEach-Object { ([string]$_).Replace($token, $value) })  # substitute in every argument
}
