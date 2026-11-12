# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
#Requires -Version 5.1
# SpeechKitVoiceTtsOfflinePipeline: PowerShell front end for speechkit-voice-tts-offline-pipeline (python -m speechkit_narrate --json ...).

# the folder holding this file; Core.ps1 derives the toolkit root (two levels up) from it
$script:ModuleRoot = $PSScriptRoot

# Dot-source every helper and command file into this module's scope. Private\ is loaded before Public\ and each folder
# alphabetically, so the order is fixed; definitions only, since none of the files run anything at load time.
foreach ($folder in @('Private', 'Public')) {
    $dir = Join-Path $PSScriptRoot $folder  # absolute path of the Private or Public subfolder
    # @() keeps a single file as an array; Sort-Object Name makes the load order the same on every machine
    foreach ($file in @(Get-ChildItem -LiteralPath $dir -Filter '*.ps1' | Sort-Object Name)) {
        # dot-sourcing (the leading period) defines the file's functions inside this module instead of a child scope
        . $file.FullName
    }
}

# Build $script:Config from the SPEECHKIT_* environment variables (backend, WSL distribution, toolkit root) and
# create the empty caches. Must run before anything that resolves Python or WSL.
Initialize-NarrationConfig
Register-NarrationCompleter  # attach tab completion for stage and tool names (see Private\Completion.ps1)

# The commands the user may call. Everything else defined above stays private to the module; keep this list in step
# with FunctionsToExport in SpeechKitVoiceTtsOfflinePipeline.psd1.
$script:PublicFunctions = @(
    'Get-NarrationBackend'
    'Set-NarrationBackend'
    'Get-NarrationStage'
    'Invoke-NarrationStage'
    'Get-NarrationTool'
    'Invoke-NarrationTool'
    'Update-NarrationCommand'
    'New-NarrationProject'
    'Get-NarrationProject'
    'Get-NarrationPlan'
    'Invoke-NarrationPipeline'
    'Get-NarrationSetting'
    'Set-NarrationSetting'
    'Test-NarrationEnvironment'
    'Install-NarrationDependency'
    'Start-NarrationMcpServer'
    'Register-NarrationMcpServer'
)

# Invoke-Narration is the short form used in the README examples
Set-Alias -Name 'Invoke-Narration' -Value 'Invoke-NarrationPipeline'

# The per-tool commands (Invoke-NarrationSpeak, Invoke-NarrationQuick, ...) live in the dynamic module
# SpeechKitVoiceTtsOfflinePipeline.Tools so that Update-NarrationCommand can regenerate them at any time (e.g. after adding plug-ins).
# Generate one command per standalone tool by asking Python for the tool list. A broken Python setup must not stop
# the module from loading, so a failure only warns and the core commands remain usable.
try {
    # -ErrorAction Stop turns any error into an exception for the catch; WhatIf and Confirm are forced off so a caller's
    # preference cannot suppress or prompt for this
    Update-NarrationCommand -ErrorAction Stop -WhatIf:$false -Confirm:$false
}
catch {  # reached when Python could not be found or `tools` failed
    # tells the user what failed and the commands that fix the setup; the text continues on the next line
    Write-Warning ("SpeechKitVoiceTtsOfflinePipeline: the per-tool commands (Invoke-NarrationSpeak, ...) were not generated: $($_.Exception.Message) " +
        "Fix the Python setup (Test-NarrationEnvironment, Install-NarrationDependency, Set-NarrationBackend) and run Update-NarrationCommand.")
}

# When the user runs Remove-Module SpeechKitVoiceTtsOfflinePipeline, also take the generated per-tool commands out of the global scope;
# they were imported globally by Update-NarrationCommand and would otherwise outlive the module.
$ExecutionContext.SessionState.Module.OnRemove = {
    Remove-NarrationToolModule  # defined in Private\Schema.ps1; deletes the SpeechKitVoiceTtsOfflinePipeline.Tools functions and module
}

# publish only the listed functions and the alias; helpers stay hidden
Export-ModuleMember -Function $script:PublicFunctions -Alias 'Invoke-Narration'
