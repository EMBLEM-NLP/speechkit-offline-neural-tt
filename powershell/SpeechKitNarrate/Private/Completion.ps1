# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# Tab completion for stage and tool names, read from the registry (including the project's own plug-ins).

# Finds the project folder the user has already typed on the command line being completed, so its own plug-in
# stages can be offered too. -BoundParameters is the "fake" bound-parameter dictionary PowerShell passes to a
# completer. Returns the absolute folder when it contains project.json, else $null. Never throws.
function Get-NarrationCompletionProject {
    param([System.Collections.IDictionary]$BoundParameters)  # parameters the user has typed so far
    foreach ($k in @('Path', 'Project')) {  # the project may arrive as -Path or as -Project depending on the command
        if ($BoundParameters.Contains($k) -and $BoundParameters[$k]) {  # only a parameter that is present and not empty
            try {  # a half-typed path must not break completion
                # make it absolute against the current location
                $p = Resolve-NarrationPath ([string]$BoundParameters[$k])
                # accept it only if it really is a project folder
                if (Test-Path -LiteralPath (Join-Path $p 'project.json')) { return $p }
            }
            # ignore anything that cannot be resolved (the reason is only visible with -Verbose)
            catch { Write-Verbose "speechkit-narrate: the typed project path could not be resolved: $($_.Exception.Message)" }
        }
    }
    $null  # no project typed yet, so completion offers stages without project plug-ins
}

# Builds one tab-completion entry. -Text is what gets inserted (and listed), -Tip the tooltip shown in menus
# (defaults to the text). Returns a System.Management.Automation.CompletionResult.
function New-NarrationCompletion {
    # PSScriptAnalyzer asks for -WhatIf on New-* functions; this one only builds an in-memory object, so it is exempt
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Builds an in-memory object only')]  # why the rule is waived
    param([string]$Text, [string]$Tip)  # inserted text and optional tooltip
    if (-not $Tip) { $Tip = $Text }  # without a tip, show the text itself
    # completion text, list text, result type (a parameter value) and tooltip
    New-Object System.Management.Automation.CompletionResult($Text, $Text, 'ParameterValue', $Tip)
}

# Argument completer for stage names (Invoke-NarrationStage -Name, Get-NarrationStage -Name and the -Target, -Force,
# -Only, -Skip and -From parameters of the pipeline commands). PowerShell calls it on Tab with the five standard
# arguments and shows whatever CompletionResult objects it outputs. Stage names come from the registry (cached for a
# minute), including the plug-in stages of a project already named on the line.
$script:StageNameCompleter = {
    # the standard argument-completer parameters; the last one holds the parameters typed so far
    param($commandName, $parameterName, $wordToComplete, $commandAst, $fakeBoundParameters)
    $null = $commandName, $commandAst  # PowerShell always passes these two; this completer has no use for them
    $VerbosePreference = 'SilentlyContinue'  # no verbose output may leak into the prompt while completing
    $ProgressPreference = 'SilentlyContinue'  # nor a progress bar from a Python call
    $word = ([string]$wordToComplete).Trim([char[]]"'`"")  # the partial word without any quote the user typed
    try {  # any failure simply yields no suggestions
        # the project folder, if one has been typed
        $project = Get-NarrationCompletionProject -BoundParameters $fakeBoundParameters
        # stage list from the registry, reusing an answer less than a minute old
        $reg = Get-NarrationRegistry -Project $project -Cached
        # name and description pairs; the comma keeps each pair as one element
        $items = @($reg.stages | ForEach-Object { , @([string]$_.name, [string]$_.description) })
        if ($parameterName -eq 'Force') { $items += , @('all', 'every stage') }  # -Force also accepts 'all'
        foreach ($i in $items) {  # test each stage name (and 'all' for -Force) against the typed prefix
            # keep those starting with what was typed (case-insensitive), with the description as tooltip
            if ($i[0] -like "$word*") { New-NarrationCompletion -Text $i[0] -Tip $i[1] }
        }
    }
    catch { Write-Verbose "speechkit-narrate: stage completion failed: $($_.Exception.Message)" }  # never an error at the prompt
}

# Argument completer for tool names (Get-NarrationTool -Name and Invoke-NarrationTool -Name). Same five parameters
# as above; outputs one CompletionResult per tool whose name starts with the typed text.
$script:ToolNameCompleter = {
    # the standard completer parameters
    param($commandName, $parameterName, $wordToComplete, $commandAst, $fakeBoundParameters)
    $null = $commandName, $parameterName, $commandAst, $fakeBoundParameters  # passed in, not needed by this completer
    $VerbosePreference = 'SilentlyContinue'  # keep verbose messages out of the prompt
    $ProgressPreference = 'SilentlyContinue'  # keep progress bars away as well
    $word = ([string]$wordToComplete).Trim([char[]]"'`"")  # the partial word with surrounding quotes removed
    try {  # errors are swallowed below
        foreach ($t in @(Get-NarrationToolList)) {  # the cached tool list, read once per session and backend
            # suggest matching tools, with their descriptions as tooltips
            if ([string]$t.name -like "$word*") { New-NarrationCompletion -Text ([string]$t.name) -Tip ([string]$t.description) }
        }
    }
    # nothing at the prompt when Python is out of reach
    catch { Write-Verbose "speechkit-narrate: tool completion failed: $($_.Exception.Message)" }
}

# Attaches the completers to the module's commands. Called once by SpeechKitNarrate.psm1 at import; takes no parameters and
# returns nothing. Registration is by command name, so it works before or after the commands are defined.
function Register-NarrationCompleter {
    # stage names for the one-stage command
    Register-ArgumentCompleter -CommandName 'Invoke-NarrationStage' -ParameterName 'Name' -ScriptBlock $script:StageNameCompleter
    # and for the stage listing
    Register-ArgumentCompleter -CommandName 'Get-NarrationStage' -ParameterName 'Name' -ScriptBlock $script:StageNameCompleter
    foreach ($p in @('Target', 'Force', 'Only', 'Skip', 'From')) {  # each stage-name parameter of the pipeline commands
        # Register for the pipeline command, its Invoke-Narration alias and the plan command (continued on the next
        # line).
        Register-ArgumentCompleter -CommandName 'Invoke-NarrationPipeline', 'Invoke-Narration', 'Get-NarrationPlan' `
            -ParameterName $p -ScriptBlock $script:StageNameCompleter  # same completer for every stage-name parameter
    }
    # tool names for the tool listing and the generic tool runner
    Register-ArgumentCompleter -CommandName 'Get-NarrationTool', 'Invoke-NarrationTool' -ParameterName 'Name' -ScriptBlock $script:ToolNameCompleter
}
