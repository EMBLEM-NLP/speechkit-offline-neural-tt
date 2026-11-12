# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
#Requires -Version 5.1
# Import the canonical module once; its functions keep their original module scope and configuration.
$canonical = Join-Path $PSScriptRoot '../SpeechKitVoiceTtsOfflinePipeline/SpeechKitVoiceTtsOfflinePipeline.psd1'
$implementation = Import-Module -Name $canonical -PassThru -ErrorAction Stop
Export-ModuleMember -Function @($implementation.ExportedFunctions.Keys) -Alias @($implementation.ExportedAliases.Keys)
