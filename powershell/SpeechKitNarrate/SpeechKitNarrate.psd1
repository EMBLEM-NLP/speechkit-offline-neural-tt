# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
@{
    RootModule = 'SpeechKitNarrate.psm1'
    ModuleVersion = '1.1.0'
    GUID = 'b9e458bd-4657-504e-97c9-f720720e2f14'
    Author = 'EMBLEM-NLP'
    CompanyName = 'EMBLEM-NLP'
    Description = 'Compatibility entry point for SpeechKitVoiceTtsOfflinePipeline. Existing Narration cmdlets are preserved.'
    PowerShellVersion = '5.1'
    CompatiblePSEditions = @('Desktop', 'Core')
    FunctionsToExport = @(
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
    CmdletsToExport = @()
    VariablesToExport = @()
    AliasesToExport = @('Invoke-Narration')
    PrivateData = @{ PSData = @{ ProjectUri = 'https://github.com/EMBLEM-NLP/speechkit-voice-tts-offline-pipeline'; LicenseUri = 'https://www.gnu.org/licenses/gpl-3.0.html' } }
}
