# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# Module manifest for SpeechKitVoiceTtsOfflinePipeline: a PowerShell data file (a hashtable literal) that Import-Module reads before it
# loads any code.
# It names the script module to load, the PowerShell versions it supports, and exactly which commands, aliases and
# format files are made public.
# Import it from the checkout with: Import-Module .\powershell\SpeechKitVoiceTtsOfflinePipeline (see powershell\README.md).
@{
    # the script module that holds the code; loaded relative to this manifest's folder
    RootModule           = 'SpeechKitVoiceTtsOfflinePipeline.psm1'
    ModuleVersion        = '1.1.0'  # semantic version reported by Get-Module; bump it when the commands change
    # fixed identity of the module so PowerShell can tell it apart from others with the same name
    GUID                 = '999ed64e-7cf3-4379-bbac-9d0be28a6b49'
    Author               = 'EMBLEM-NLP'  # author shown by Get-Module -ListAvailable
    CompanyName          = 'EMBLEM-NLP'  # publisher shown in module listings
    # copyright line kept in the module metadata and shown by Get-Module; the license is GPL-3.0-or-later
    Copyright            = '(C) 2026 EMBLEM-NLP. Licensed under GPL-3.0-or-later.'
    # summary that Get-Module displays (wrapped here, one paragraph)
    Description          = @'
PowerShell front end for speechkit-voice-tts-offline-pipeline (python -m speechkit_narrate): turn a .txt, .md or SpeechKit
request .json into narrated audio with word timings, using the offline speechkit neural voice, with a
measured pronunciation loop. Runs Python natively, inside WSL, or natively with the Linux-only voice
stages delegated to WSL. Licensed under GPL-3.0-or-later.
'@
    PowerShellVersion    = '5.1'  # oldest engine allowed: Windows PowerShell 5.1, which ships with Windows 10 and 11
    # runs on both Windows PowerShell (Desktop edition) and PowerShell 7 (Core edition)
    CompatiblePSEditions = @('Desktop', 'Core')
    # The exported command list is spelled out (no wildcard) so PowerShell can auto-load the module and offer tab
    # completion for these names before the module is imported. It must match $script:PublicFunctions in
    # SpeechKitVoiceTtsOfflinePipeline.psm1. The per-tool commands (Invoke-NarrationSpeak and so on) are not listed: they live in the
    # generated SpeechKitVoiceTtsOfflinePipeline.Tools module.
    FunctionsToExport    = @(
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
    CmdletsToExport      = @()  # no compiled C# cmdlets; every command is a script function
    VariablesToExport    = @()  # no variables leak into the caller's session
    # the short name for Invoke-NarrationPipeline, created by Set-Alias in SpeechKitVoiceTtsOfflinePipeline.psm1
    AliasesToExport      = @('Invoke-Narration')
    # table and list views for the SpeechKitVoiceTtsOfflinePipeline.* object types, applied when the module loads
    FormatsToProcess     = @('SpeechKitVoiceTtsOfflinePipeline.Format.ps1xml')
    PrivateData          = @{  # module metadata block (read by PowerShellGet and module listings)
        PSData = @{  # PSData is the conventional key PowerShellGet looks for
            # search keywords; Windows and Linux mark the supported platforms, and the two PSEdition tags repeat
            # CompatiblePSEditions
            Tags         = @(
                'speechkit'
                'narration'
                'tts'
                'audiobook'
                'wsl'
                'mcp'
                'python'
                'PSEdition_Desktop'
                'PSEdition_Core'
                'Windows'
                'Linux'
            )
            ProjectUri   = 'https://github.com/EMBLEM-NLP/speechkit-voice-tts-offline-pipeline'  # the repository the module lives in
            # text shown for this version
            ReleaseNotes = @'
1.1.0: canonical module name SpeechKitVoiceTtsOfflinePipeline; SpeechKitNarrate remains a forwarding module.
Canonical and legacy object type names are both supported; existing Narration cmdlets are unchanged.

1.0.0: first release of SpeechKitNarrate, a front end for python -m speechkit_narrate (stages extract,
check, script, prevoice, narrate, autofix, audit, report, export; tools speak and quick). Backends
Auto/Native/Wsl, pipeline and stage cmdlets, generated tool commands, MCP registration, dependency
installation for Windows, Linux and WSL.
'@
        }
    }
}
