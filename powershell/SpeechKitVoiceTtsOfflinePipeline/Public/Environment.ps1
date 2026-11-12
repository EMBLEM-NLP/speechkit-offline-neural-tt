# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# Public command: checks everything the narration needs by running `python -m speechkit_narrate doctor` on the native
# side and, in Auto mode on Windows, inside WSL as well. -Quiet returns a single $true/$false; -SkipWsl leaves out the
# WSL side. Returns one SpeechKitVoiceTtsOfflinePipeline.Check object per check (Ok, Where, Name, Detail). Starts Python and possibly
# WSL; changes nothing.
function Test-NarrationEnvironment {
    <#
    .SYNOPSIS
    Checks every Python package, program, voice runtime and model the narration needs (python -m speechkit_narrate doctor).

    .DESCRIPTION
    Returns one object per check. In Auto mode on Windows the WSL side (where narration and the audit run) is
    checked too; those rows have Where = 'wsl'. Missing items can be installed with Install-NarrationDependency.

    .PARAMETER Quiet
    Return only $true (everything is there) or $false.

    .PARAMETER SkipWsl
    In Auto mode on Windows, do not check the WSL side.

    .EXAMPLE
    Test-NarrationEnvironment | Where-Object { -not $_.Ok }

    What is missing.

    .EXAMPLE
    if (-not (Test-NarrationEnvironment -Quiet)) { Install-NarrationDependency -WhatIf }

    .OUTPUTS
    SpeechKitVoiceTtsOfflinePipeline.Check
    #>
    [CmdletBinding()]  # advanced function with the common parameters
    [OutputType('SpeechKitVoiceTtsOfflinePipeline.Check', [bool])]  # normally SpeechKitVoiceTtsOfflinePipeline.Check rows, a boolean with -Quiet
    # -Quiet collapses the rows into one boolean; -SkipWsl leaves out the WSL-side doctor run
    param([switch]$Quiet, [switch]$SkipWsl)
    try {  # any unexpected failure is rethrown once at the bottom
        # growable list of SpeechKitVoiceTtsOfflinePipeline.Check rows from every side that is checked
        $rows = New-Object System.Collections.Generic.List[object]
        $effective = Get-NarrationEffectiveBackend  # Native or Wsl for the current setting
        $here = 'native'  # label for the side the main checks run on
        if ($effective -eq 'Wsl') { $here = 'wsl' }  # everything runs inside WSL
        if ($effective -eq 'Native') {  # a local Python is involved, so check that it can be found
            try {  # finding Python throws when there is none
                $py = Resolve-NarrationPython  # locate the native Python
                # a passing row with its path and version
                $rows.Add((New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.Check' -Property ([ordered]@{ Ok = $true; Where = $here; Name = 'python'; Detail = "$($py.Executable) ($($py.Version))" })))
            }
            catch {  # no Python: record the failure instead of stopping
                # a failing row carrying the reason
                $rows.Add((New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.Check' -Property ([ordered]@{ Ok = $false; Where = $here; Name = 'python'; Detail = $_.Exception.Message })))
            }
        }
        # pairs of (row label, backend to run doctor with); the leading comma keeps the pair as one element
        $sets = @(, @($here, $script:Config.Backend))
        # Auto mode on Windows delegates to WSL, so the WSL side needs checking too, unless -SkipWsl
        if (-not $SkipWsl -and $script:Config.Backend -eq 'Auto' -and $script:OnWindows) {
            if (Test-NarrationWslAvailable) { $sets += , @('wsl', 'Wsl') }  # WSL exists: run doctor there as well
            else {  # no WSL at all
                # one failing row explains that the Linux-only stages cannot run
                $rows.Add((New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.Check' -Property ([ordered]@{ Ok = $false; Where = 'wsl'; Name = 'wsl.exe'; Detail = 'WSL is not installed (Install-NarrationDependency -Wsl); narration and the audit cannot run' })))
            }
        }
        foreach ($set in $sets) {  # run doctor for each side
            # without a working native Python the native doctor cannot start, so skip it
            if ($set[0] -eq 'native' -and -not ($rows | Where-Object { $_.Name -eq 'python' -and $_.Ok })) { continue }
            try {  # a broken side must not hide the other side's rows
                # -AllowFailure because doctor reports ok = false whenever anything is missing, and those rows are the
                # point
                $r = Invoke-NarrationCli -Arguments @('doctor') -Backend $set[1] -AllowFailure -Activity 'speechkit-voice-tts-offline-pipeline doctor'
                foreach ($c in @($r.checks)) {  # each check Python reported
                    # @() turns a missing checks list into one $null element, which must not become a row
                    if ($null -eq $c) { continue }
                    # one row per check, labelled with the side it ran on
                    $rows.Add((New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.Check' -Property ([ordered]@{ Ok = [bool]$c.ok; Where = $set[0]; Name = [string]$c.name; Detail = [string]$c.detail })))
                }
            }
            catch {  # doctor itself could not run (for example WSL failed to start)
                # one failing row with the reason
                $rows.Add((New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.Check' -Property ([ordered]@{ Ok = $false; Where = $set[0]; Name = 'speechkit-voice-tts-offline-pipeline doctor'; Detail = $_.Exception.Message })))
            }
        }
        if ($Quiet) { return (-not ($rows | Where-Object { -not $_.Ok })) }  # -Quiet: true only when no row failed
        $rows  # otherwise the rows themselves
    }
    catch { $PSCmdlet.ThrowTerminatingError($_) }  # rethrow as this command's terminating error
}

# Public command: installs what speechkit-voice-tts-offline-pipeline needs, one step at a time, each step asking ShouldProcess so -WhatIf
# lists every command without running it. -Component limits the groups, -Wsl adds the WSL groups, -Distribution names
# the WSL distribution (default Ubuntu-24.04). Programs run attached to this console (winget, pip, wsl.exe).
# Outputs one SpeechKitVoiceTtsOfflinePipeline.InstallStep per planned step (Step, Status, ExitCode, Command). Status is ok, present
# (already installed), failed, or skipped (declined under -Confirm, or not run because of -WhatIf). A failed step
# writes a non-terminating error and the remaining steps still run.
function Install-NarrationDependency {
    <#
    .SYNOPSIS
    Installs what speechkit-voice-tts-offline-pipeline needs: Python, ffmpeg, the Python packages, and optionally WSL with the voice runtime.

    .DESCRIPTION
    Never runs on import; every step asks ShouldProcess, so -WhatIf lists the exact commands and runs nothing (each
    step is still returned, with Status 'skipped').

    On Windows the default components are:
      Winget    winget install Python.Python.3.13 and Gyan.FFmpeg (ffmpeg is only needed for mp3/m4a export)
      Pip       python -m pip install -r requirements.txt (the toolkit's Python packages, including mcp)
    With -Wsl also (narration and the pronunciation audit are Linux-only):
      WslInstall   wsl --install -d Ubuntu-24.04 (skipped when that distribution exists)
      WslPackages  as root: Python 3.13 from the deadsnakes PPA, ffmpeg, git, curl, and a venv in
                   /opt/speechkit-narrate/venv with requirements-voice.txt
      WslSetup     /opt/speechkit-narrate/venv/bin/python -m speechkit_narrate setup inside WSL (voice runtime and
                   recognizer models), then Set-NarrationBackend -Distribution <distro>
                   -WslPython /opt/speechkit-narrate/venv/bin/python
    On Linux and macOS the default is Setup (python -m speechkit_narrate setup), which installs the missing Python
    packages and, on Linux only, the voice runtime and the recognizer models.

    Pip and WslPackages read the checkout's requirements files, so they need Source mode (Get-NarrationBackend shows
    the mode). In Installed mode install the package into your Python yourself (python -m pip install <checkout>).

    Programs run attached to this console, so their output and prompts show directly. Open a new PowerShell
    window afterwards so PATH changes take effect, then run Test-NarrationEnvironment.

    .PARAMETER Component
    Only these components (see the description).

    .PARAMETER Wsl
    Also set up WSL for the Linux-only stages.

    .PARAMETER Distribution
    The WSL distribution to install and use (default Ubuntu-24.04).

    .EXAMPLE
    Install-NarrationDependency -Wsl -WhatIf

    Lists every command it would run, without running anything.

    .EXAMPLE
    Install-NarrationDependency -Wsl

    Installs everything, asking for confirmation where Windows asks (winget, UAC, the WSL user name).

    .EXAMPLE
    Install-NarrationDependency -Component Pip -Confirm

    Only the Python packages, confirming each step.

    .OUTPUTS
    SpeechKitVoiceTtsOfflinePipeline.InstallStep
    #>
    # -WhatIf and -Confirm; Medium impact, so -Confirm is needed to be asked
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
    [OutputType('SpeechKitVoiceTtsOfflinePipeline.InstallStep')]  # type of the per-step result objects
    param(  # Component, Wsl and Distribution, all optional; ShouldProcess adds -WhatIf and -Confirm
        # only known component names, with tab completion
        [ValidateSet('Winget', 'Pip', 'Setup', 'WslInstall', 'WslPackages', 'WslSetup')]
        [string[]]$Component,  # which groups to run; empty means the platform's defaults

        [switch]$Wsl,  # also set up WSL for narration and the audit

        [string]$Distribution = 'Ubuntu-24.04'  # the WSL distribution to install and use
    )
    $pyDisplay = 'python'  # the Python shown in command lines when none is installed yet
    if ($script:OnWindows) { $pyDisplay = 'py -3.13' }  # on Windows the py launcher is the likely way in
    # show the real Python when there already is one (no Python yet is normal here, so the reason is only verbose
    # output)
    try { $pyDisplay = (Resolve-NarrationPython).Executable }  # the Python a real run would use, if there is one
    catch { Write-Verbose "speechkit-voice-tts-offline-pipeline: no Python found yet: $($_.Exception.Message)" }  # none yet is normal here
    # the steps to run, in order
    $steps = Get-NarrationInstallStep -Component $Component -Wsl:$Wsl -Distribution $Distribution -PythonCommand $pyDisplay
    $wslPaths = @{}  # placeholder -> path as WSL sees it, filled by Expand-NarrationWslToken when first needed
    foreach ($step in $steps) {  # work through the steps
        $status = 'skipped'  # stays 'skipped' unless the step runs (declined under -Confirm, or -WhatIf)
        $exit = $null  # exit code, when a program ran
        $commandLine = $step.CommandLine  # the display command line
        # -WhatIf prints the step and skips it; a declined -Confirm skips it too. Either way the step is still
        # reported below, with Status 'skipped'.
        if ($PSCmdlet.ShouldProcess($step.Description, "$($step.Name): $commandLine")) {
            try {  # a failing step is reported and the loop moves on
                if ($step.Script) {  # a step implemented as a PowerShell script block
                    & $step.Script  # run it in this session
                    $status = 'ok'  # it finished without throwing
                }
                else {  # a step that starts a program
                    $file = $step.FilePath  # the program to start
                    $argList = @($step.ArgumentList)  # its arguments, as a fresh array that can be edited
                    # Python steps use the newest interpreter, found again because an earlier step may have just
                    # installed it
                    if ($step.UsesPython) { $file = (Resolve-NarrationPython -Refresh).Executable }
                    # before installing a distribution, ask wsl.exe whether it already exists
                    if ($step.Component -eq 'WslInstall') {
                        # list the installed distributions quietly (names only), as UTF-8
                        $have = Invoke-NarrationProcess -FilePath $file -ArgumentList @('-l', '-q') -Environment @{ WSL_UTF8 = '1' } -TimeoutSeconds 120
                        # the distribution is in the list
                        if ($have.ExitCode -eq 0 -and (@($have.StdOut -split "`r?`n" | ForEach-Object { $_.Trim() }) -contains $step.Distribution)) {
                            # say why nothing runs
                            Write-Verbose "speechkit-voice-tts-offline-pipeline: WSL distribution $($step.Distribution) is already installed"
                            $status = 'present'  # record that it was already there
                            $file = $null  # no program to run
                        }
                    }
                    if ($file) {  # there is a program to run
                        # replace <toolkit-in-wsl> by the checkout as WSL sees it (a no-op without the placeholder)
                        $argList = @(Expand-NarrationWslToken -ArgumentList $argList -FilePath $file `
                                -Distribution $step.Distribution -Cache $wslPaths)  # the arguments with the path filled in
                        # fail early with advice when the program does not exist (a new window refreshes PATH)
                        if (-not (Test-NarrationCommand $file)) { throw "'$file' was not found. Install it first (or open a new PowerShell window so PATH is refreshed)." }
                        # run it attached to this console and wait
                        $exit = Invoke-NarrationNative -FilePath $file -ArgumentList $argList -Environment $step.Environment
                        if ($step.SuccessCodes -contains $exit) { $status = 'ok' }  # an accepted exit code means success
                        # anything else fails the step
                        else { $status = 'failed'; throw "$($step.Name) exited with code $exit" }
                    }
                }
            }
            catch {  # any failure of this step
                $status = 'failed'  # record the failure in this step's result object
                # non-terminating error, so the remaining steps still run
                Write-Error -Message "speechkit-voice-tts-offline-pipeline install step '$($step.Name)' failed: $($_.Exception.Message)" -Category InvalidResult -TargetObject $step
            }
        }
        # one result per planned step, including the skipped ones
        New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.InstallStep' -Property ([ordered]@{
                Step     = $step.Name  # the step's short name, such as 'winget Gyan.FFmpeg' or 'pip packages'
                Status   = $status  # ok, present, failed or skipped
                ExitCode = $exit  # program exit code, if any
                Command  = $commandLine  # the command line that was (or would have been) run
            })
    }
    # a dry run changed nothing, so the reminders and the cache reset follow only real runs
    if (-not $WhatIfPreference) {
        Reset-NarrationCache  # a new Python or environment may now exist, so earlier answers about it are stale
        # remind the user how to pick up the changes
        Write-Verbose 'speechkit-voice-tts-offline-pipeline: open a new PowerShell window so PATH changes take effect, then run Test-NarrationEnvironment.'
    }
}
