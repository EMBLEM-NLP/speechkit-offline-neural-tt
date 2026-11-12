# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# Public command: reports how the module will run the toolkit (configured and effective backend, platform, mode,
# toolkit folder, Python path and version, WSL distribution, launcher, WSL python and the delegation command).
# -NoProbe skips starting Python and WSL, leaving the resolved fields empty. Returns one SpeechKitVoiceTtsOfflinePipeline.Backend
# object. Read-only apart from filling the module's caches.
function Get-NarrationBackend {
    <#
    .SYNOPSIS
    Shows how the module runs speechkit-voice-tts-offline-pipeline: backend, Python, WSL distribution and delegation.

    .DESCRIPTION
    Backends:
      Native  python on this machine (from $env:SPEECHKIT_PYTHON, else py -3.13, py -3 or python.exe on Windows,
              python3 or python elsewhere).
      Wsl     everything inside WSL: wsl.exe -d <distro> --exec env PYTHONPATH=<checkout> python3 -m speechkit_narrate
              ... with every path converted by wslpath.
      Auto    (default) native, and on Windows the Linux-only stages (prevoice, narrate, autofix, audit) and the speak
              tool are delegated to WSL: the module sets SPEECHKIT_DELEGATE for the Python side.

    Modes (where the speechkit_narrate Python package comes from):
      Source     the module sits in the speechkit-voice-tts-offline-pipeline checkout, which is put on PYTHONPATH
                 (Mode = Source, ToolkitRoot set).
      Installed  no checkout is configured; the chosen Python must have the speechkit-voice-tts-offline-pipeline package installed.

    .PARAMETER NoProbe
    Do not start Python or WSL to fill in the resolved paths (quick; those fields stay empty).

    .EXAMPLE
    Get-NarrationBackend

    Shows the current backend, the Python it uses and the WSL delegation command.

    .EXAMPLE
    (Get-NarrationBackend).Python

    The full path of the Python that runs the toolkit.

    .OUTPUTS
    SpeechKitVoiceTtsOfflinePipeline.Backend
    #>
    [CmdletBinding()]  # advanced function: gains -Verbose, -Debug and the other common parameters
    # declares the object type for Get-Help and tab completion of the result's properties
    [OutputType('SpeechKitVoiceTtsOfflinePipeline.Backend')]
    param([switch]$NoProbe)  # -NoProbe reports the settings without starting Python or WSL to look things up

    $py = $null; $pyVersion = $null; $pyError = $null  # resolved Python path, its version and any error from finding it
    $wslPy = $null; $delegate = $null; $wslCommand = $null  # WSL python, delegation command and WSL launcher text
    $effective = Get-NarrationEffectiveBackend  # Native or Wsl: how Python is started for the current setting
    $prefix = Get-NarrationWslPrefix  # the launcher wsl.exe [-d distro] --exec, or the SPEECHKIT_WSL override
    # shown as one command line even when WSL is not used, so the setting can be checked
    $wslCommand = ConvertTo-NarrationArgumentString -ArgumentList (@($prefix.FilePath) + @($prefix.Arguments))
    if (-not $NoProbe) {  # without -NoProbe, locate the interpreters, which may start Python or WSL once
        if ($effective -eq 'Native') {  # only a native or Auto setup runs a local Python
            # find it (may start Python once) and keep its path and version
            try { $r = Resolve-NarrationPython; $py = $r.Executable; $pyVersion = $r.Version }
            catch { $pyError = $_.Exception.Message }  # report the reason instead of failing the whole command
        }
        $delegate = Get-NarrationDelegateCommand  # what Auto mode would put in SPEECHKIT_DELEGATE, or $null
        # the WSL python matters when everything runs in WSL or when stages are delegated there
        if ($effective -eq 'Wsl' -or $delegate) {
            # may start WSL to look for the venv; any failure just leaves the field empty
            try { $wslPy = Resolve-NarrationWslPython } catch { $wslPy = $null }
        }
    }
    $platform = 'Linux'  # assume Linux unless the checks on the next line say Windows or macOS
    # Windows from the module's own check; $IsMacOS exists only in PowerShell 6 and later
    if ($script:OnWindows) { $platform = 'Windows' } elseif ($PSVersionTable.PSVersion.Major -ge 6 -and $IsMacOS) { $platform = 'MacOS' }
    New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.Backend' -Property ([ordered]@{  # build the Backend object
            Backend        = $script:Config.Backend  # the configured choice: Auto, Native or Wsl
            Effective      = $effective  # how Python is actually launched
            Platform       = $platform  # Windows, Linux or MacOS
            Mode           = Get-NarrationMode  # Source: run from the checkout; Installed: a pip-installed package
            # folder that holds the speechkit_narrate package (empty in Installed mode)
            ToolkitRoot    = $script:Config.ToolkitRoot
            Python         = $py  # full path of the native Python, if probed
            # the native Python's version string, such as 3.13.1; empty with -NoProbe or when none was found
            PythonVersion  = $pyVersion
            PythonError    = $pyError  # why no Python was found, if that happened
            Distribution   = $script:Config.Distribution  # WSL distribution, empty for WSL's default
            WslCommand     = $wslCommand  # the WSL launcher as a command line
            WslPython      = $wslPy  # python used inside WSL
            WslAvailable   = (Test-NarrationWslAvailable)  # whether wsl.exe (or an override) exists
            Delegate       = $delegate  # the SPEECHKIT_DELEGATE value Python will receive
            WslEnvironment = $script:Config.WslEnvironment  # extra variables passed to WSL commands
        })
}

# Public command: changes how the module runs the toolkit for this session. Each parameter that is given replaces
# one setting: -Backend (Auto, Native, Wsl), -Distribution, -Python, -WslPython, -ToolkitRoot (checked to contain
# the speechkit_narrate package) and -WslEnvironment (hashtable). Every value is validated before any setting changes,
# so a refused call leaves the session exactly as it was; any accepted call, even one with no settings, then clears
# the caches. Supports -WhatIf and -Confirm. -PassThru returns the resulting SpeechKitVoiceTtsOfflinePipeline.Backend; otherwise
# nothing is returned.
function Set-NarrationBackend {
    <#
    .SYNOPSIS
    Chooses how the module runs speechkit-voice-tts-offline-pipeline (for this PowerShell session).

    .DESCRIPTION
    Auto (the default) runs Python natively and, on Windows, sends the Linux-only stages to WSL. Native never
    delegates. Wsl runs every command inside WSL and converts every path with wslpath.

    Settings last for the session. To make them permanent put the call in your $PROFILE, or set
    $env:SPEECHKIT_BACKEND, $env:SPEECHKIT_WSL_DISTRO, $env:SPEECHKIT_PYTHON, $env:SPEECHKIT_WSL_PYTHON and
    $env:SPEECHKIT_ROOT before importing the module. $env:SPEECHKIT_WSL replaces the WSL launcher entirely
    (default: wsl.exe [-d <distro>] --exec). Every value is checked first; if one is refused nothing changes.
    Changing anything clears the module's caches.

    .PARAMETER Backend
    Auto, Native or Wsl.

    .PARAMETER Distribution
    The WSL distribution (wsl.exe -d). Empty means WSL's default distribution.

    .PARAMETER Python
    The native Python: a path to python.exe, or a command such as 'py -3.13'. Empty restores discovery.

    .PARAMETER WslPython
    The python inside WSL (default: /opt/speechkit-narrate/venv/bin/python when Install-NarrationDependency -Wsl made
    it, else python3).

    .PARAMETER ToolkitRoot
    The speechkit-voice-tts-offline-pipeline checkout, the folder that contains the 'speechkit_narrate' Python package
    (default: two folders above the module). An empty value ('') switches to Installed mode: the chosen Python must
    then have the speechkit-voice-tts-offline-pipeline package installed, and no folder is put on PYTHONPATH.

    .PARAMETER WslEnvironment
    Extra environment variables for commands run inside WSL, e.g. @{ SPEECHKIT_ASR_MODELS = '/home/me/models' }.

    .PARAMETER PassThru
    Return the resulting SpeechKitVoiceTtsOfflinePipeline.Backend object.

    .EXAMPLE
    Set-NarrationBackend -Backend Auto -Distribution Ubuntu-24.04

    Native Python on Windows, with narration and the pronunciation audit delegated to Ubuntu 24.04 in WSL.

    .EXAMPLE
    Set-NarrationBackend Wsl -Distribution Ubuntu-24.04 -PassThru

    Runs the whole toolkit inside WSL and shows the resulting configuration.

    .EXAMPLE
    Set-NarrationBackend -Python 'C:\Python313\python.exe' -ToolkitRoot 'D:\src\speechkit-voice-tts-offline-pipeline'
    #>
    # SupportsShouldProcess adds -WhatIf and -Confirm; Low impact means no prompt unless -Confirm is given
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
    [OutputType('SpeechKitVoiceTtsOfflinePipeline.Backend')]  # type returned with -PassThru
    param(  # the parameters (each one optional)
        [Parameter(Position = 0)]  # the backend can be given without its name, as the first argument
        [ValidateSet('Auto', 'Native', 'Wsl')]  # only the three known backends are accepted, with tab completion
        [string]$Backend,  # Auto, Native or Wsl

        [string]$Distribution,  # WSL distribution name passed to wsl.exe -d; '' means the default

        [string]$Python,  # native Python path or command line; '' restores discovery

        [string]$WslPython,  # python inside WSL; '' restores the venv/python3 probe

        [string]$ToolkitRoot,  # the checkout containing the speechkit_narrate package; '' means Installed mode

        [hashtable]$WslEnvironment,  # extra environment variables for WSL commands

        [switch]$PassThru  # return the new configuration
    )
    # 1. Validate everything before anything is stored, so a refused value leaves every setting and cache as it was.
    $newRoot = $null  # the toolkit folder to store, worked out below when -ToolkitRoot was given
    if ($PSBoundParameters.ContainsKey('ToolkitRoot') -and -not [string]::IsNullOrWhiteSpace($ToolkitRoot)) {
        $newRoot = Resolve-NarrationPath $ToolkitRoot  # make it absolute against the current location
        if (-not (Test-NarrationToolkitFolder $newRoot)) {  # it must contain speechkit_narrate\__main__.py
            # the message names the folder that was given
            $notFound = "'$newRoot' does not contain the speechkit_narrate package (speechkit_narrate\__main__.py)."
            # an error record of the not-found kind, aimed at that folder
            $record = New-NarrationErrorRecord -Message $notFound -Type 'FileNotFoundError' -Target $newRoot
            $PSCmdlet.ThrowTerminatingError($record)  # stop before any setting has changed
        }
    }
    $changes = @()  # descriptions of the requested changes, for the ShouldProcess message
    # look at each setting that may be changed
    foreach ($k in @('Backend', 'Distribution', 'Python', 'WslPython', 'ToolkitRoot', 'WslEnvironment')) {
        # record NAME=value for every parameter the caller actually passed (even an empty one)
        if ($PSBoundParameters.ContainsKey($k)) { $changes += "$k=$($PSBoundParameters[$k])" }
    }
    if (-not $changes) { $changes = @('(caches only)') }  # a bare call still clears the caches, so say that
    # -WhatIf prints the message and stops here; -Confirm asks first
    if (-not $PSCmdlet.ShouldProcess('speechkit-voice-tts-offline-pipeline backend', "Set $($changes -join ', ')")) { return }
    # 2. Apply: nothing below can fail, so the settings change together.
    if ($PSBoundParameters.ContainsKey('Backend')) {  # a backend was given
        # stored with standard casing (ValidateSet already accepted any casing)
        $script:Config.Backend = (Get-Culture).TextInfo.ToTitleCase($Backend.ToLowerInvariant())
    }
    # '' is a valid value, meaning WSL's default distribution
    if ($PSBoundParameters.ContainsKey('Distribution')) { $script:Config.Distribution = $Distribution }
    # '' falls back to $env:SPEECHKIT_PYTHON or discovery
    if ($PSBoundParameters.ContainsKey('Python')) { $script:Config.Python = $Python }
    # '' falls back to $env:SPEECHKIT_WSL_PYTHON or the probe
    if ($PSBoundParameters.ContainsKey('WslPython')) { $script:Config.WslPython = $WslPython }
    if ($PSBoundParameters.ContainsKey('WslEnvironment')) {  # extra WSL variables were given
        # keep a copy so later changes to the caller's hashtable do not leak in; $null or @{} clears them
        if ($WslEnvironment) { $script:Config.WslEnvironment = $WslEnvironment.Clone() } else { $script:Config.WslEnvironment = @{} }
    }
    # the folder validated above, or $null (Installed mode) for an empty value
    if ($PSBoundParameters.ContainsKey('ToolkitRoot')) { $script:Config.ToolkitRoot = $newRoot }
    # 3. Drop every cached answer, since they belong to the old setup.
    Reset-NarrationCache
    if ($PassThru) { Get-NarrationBackend }  # show the resulting configuration (this probes Python and WSL)
}
