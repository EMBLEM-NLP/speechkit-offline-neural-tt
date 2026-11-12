# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# Python JSON -> typed PowerShell objects (PSTypeName SpeechKitVoiceTtsOfflinePipeline.*), formatted by SpeechKitVoiceTtsOfflinePipeline.Format.ps1xml.

# Creates a PowerShell object with the given properties and a type name such as SpeechKitVoiceTtsOfflinePipeline.Stage. -TypeName is the
# name, -Property an ordered dictionary of property names and values. Returns the PSObject. The type name is what
# makes SpeechKitVoiceTtsOfflinePipeline.Format.ps1xml pick its table or list view, and what [OutputType()] declarations refer to.
function New-NarrationObject {
    # PSScriptAnalyzer asks for -WhatIf on New-* functions; this one only builds an in-memory object, so it is exempt
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Builds an in-memory object only')]  # why the rule is waived
    # both inputs; an ordered dictionary keeps the properties in the order written
    param([Parameter(Mandatory = $true)][string]$TypeName, [System.Collections.IDictionary]$Property)
    $o = New-Object -TypeName PSObject -Property $Property  # a property bag built from the dictionary
    # put the custom type name first so formatting and type checks find it before the generic names
    $o.PSObject.TypeNames.Insert(0, $TypeName)
    # Existing scripts and serialized objects can still recognize the original module's type names.
    if ($TypeName.StartsWith('SpeechKitVoiceTtsOfflinePipeline.')) {
        $legacy = 'SpeechKitNarrate.' + $TypeName.Substring('SpeechKitVoiceTtsOfflinePipeline.'.Length)
        $o.PSObject.TypeNames.Insert(1, $legacy)
    }
    $o  # write the object to the pipeline, which is how a PowerShell function returns it
}

# Normalises a JSON list (or $null, or a single value) into a [string[]]. -Value is the input. Returns a string array,
# empty when there was nothing, so properties such as Needs or Reasons are always arrays and can be joined safely.
function ConvertTo-NarrationStringArray {
    param($Value)  # a list, a single value or $null
    # nothing: an empty string array; the comma stops PowerShell from turning it into $null on return
    if ($null -eq $Value) { return , [string[]]@() }
    , [string[]]@($Value | ForEach-Object { [string]$_ })  # every element as text, returned as one array object
}

# Derives the generated command name for a tool: 'speak' becomes Invoke-NarrationSpeak, a plug-in tool 'word_count'
# Invoke-NarrationWordCount. -Tool is the tool name. Returns the command name string. Names that would collide with
# the module's own commands (Invoke-NarrationTool, Invoke-NarrationStage, Invoke-NarrationPipeline) get a 'Tool' prefix.
function Get-NarrationToolCommandName {
    param([Parameter(Mandatory = $true)][string]$Tool)  # the tool's name from the registry
    $pascal = ConvertTo-NarrationPascalCase $Tool  # snake_case to PascalCase, e.g. word_count becomes WordCount
    # prefix 'Tool' for clashing names; note an empty result also becomes plain 'Tool', i.e. Invoke-NarrationTool
    if (-not $pascal -or @('Tool', 'Stage', 'Pipeline', 'Command') -contains $pascal) { $pascal = 'Tool' + $pascal }
    "Invoke-Narration$pascal"  # the verb-noun command name
}

# Turns one stage description from `speechkit-voice-tts-offline-pipeline stages` into a SpeechKitVoiceTtsOfflinePipeline.Stage object for Get-NarrationStage.
# -Stage is the parsed stage, -Registry the whole registry result (for its delegate setting), -Project the project
# folder the registry was read for. Returns the object; Where says whether the stage runs here, is delegated or is
# blocked.
function ConvertTo-NarrationStage {
    param($Stage, $Registry, [string]$Project)  # stage, registry and project folder
    $runsHere = [bool]$Stage.runs_here  # true when the stage's platforms include this machine's
    $where = 'here'  # assume it runs locally
    # otherwise it is delegated when a SPEECHKIT_DELEGATE command exists, else it cannot run at all
    if (-not $runsHere) { if ($Registry.delegate) { $where = 'delegate' } else { $where = 'blocked' } }
    New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.Stage' -Property ([ordered]@{  # build the typed object from these properties
            Name        = [string]$Stage.name  # stage name, such as extract or narrate
            Needs       = ConvertTo-NarrationStringArray $Stage.needs  # stages that must be up to date first
            Description = [string]$Stage.description  # one-line summary of the stage
            Inputs      = ConvertTo-NarrationStringArray $Stage.inputs  # files the stage reads, relative to the project
            Outputs     = ConvertTo-NarrationStringArray $Stage.outputs  # files the stage writes
            # project.json sections whose changes make the stage stale
            Settings    = ConvertTo-NarrationStringArray $Stage.settings
            # platforms it can run on (any, or linux for the voice stages)
            Platforms   = ConvertTo-NarrationStringArray $Stage.platforms
            # JSON schema of its options; these also become Invoke-NarrationStage parameters
            Options     = $Stage.options
            Default     = [bool]$Stage.default  # part of the default pipeline when true
            Heavy       = [bool]$Stage.heavy  # marks long-running stages such as narrate
            # 'speechkit-voice-tts-offline-pipeline' for built-in stages, otherwise the plug-in that supplied it
            Source      = [string]$Stage.source
            RunsHere    = $runsHere  # whether it can run on this machine
            Where       = $where  # here, delegate or blocked
            Project     = $Project  # the folder the registry was read for, or $null
        })
}

# Turns one tool description from `speechkit-voice-tts-offline-pipeline tools` into a SpeechKitVoiceTtsOfflinePipeline.Tool object for Get-NarrationTool.
# -Tool is the parsed description. Returns the object, including the name of its generated command.
function ConvertTo-NarrationTool {
    param($Tool)  # the parsed tool description
    $required = @()  # required parameter names, empty unless the schema lists some
    # the schema's own 'required' list, when present
    if ($Tool.params -and $Tool.params.required) { $required = $Tool.params.required }
    $props = $null  # the schema's properties object, if any
    if ($Tool.params) { $props = $Tool.params.properties }  # taken from the params schema
    New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.Tool' -Property ([ordered]@{  # build the SpeechKitVoiceTtsOfflinePipeline.Tool object
            Name        = [string]$Tool.name  # tool name as the CLI knows it
            Command     = Get-NarrationToolCommandName -Tool ([string]$Tool.name)  # the generated command that runs it
            Description = [string]$Tool.description  # one-line summary of what the tool does, from its registration
            Parameters  = $props  # per-parameter JSON schema (type, description, default, format)
            Required    = ConvertTo-NarrationStringArray $required  # names that must be supplied
            Platforms   = ConvertTo-NarrationStringArray $Tool.platforms  # where the tool can run natively
            Heavy       = [bool]$Tool.heavy  # long-running tools are flagged
            Source      = [string]$Tool.source  # built-in or the plug-in that added it
            RunsHere    = [bool]$Tool.runs_here  # false means it needs delegation (speak on Windows)
        })
}

# Turns one step of `speechkit-voice-tts-offline-pipeline plan` into a SpeechKitVoiceTtsOfflinePipeline.PlanStep. -Step is the parsed step, -Project the project folder.
# Returns the object (Stage, Action, Where, Reasons, Project) that Get-NarrationPlan outputs.
function ConvertTo-NarrationPlanStep {
    param($Step, [string]$Project)  # plan step and project folder
    New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.PlanStep' -Property ([ordered]@{  # build the plan row
            Stage   = [string]$Step.stage  # the stage this plan row is about
            Action  = [string]$Step.action  # run, skip, adopt or blocked
            # where it would run: here, or delegate when another command (WSL on Windows) runs it
            Where   = [string]$Step.where
            # why the planner chose the action, such as "never run" or "setting video changed"
            Reasons = ConvertTo-NarrationStringArray $Step.reasons
            Project = $Project  # project folder, so piped rows still know where they belong
        })
}

# Turns one step of a `speechkit-voice-tts-offline-pipeline run` result into a SpeechKitVoiceTtsOfflinePipeline.RunStep. -Step is the parsed step, -Project the folder.
# Returns the object with timing, the stage's result data and any error.
function ConvertTo-NarrationRunStep {
    param($Step, [string]$Project)  # run step and project folder
    New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.RunStep' -Property ([ordered]@{  # build the run row
            Stage   = [string]$Step.stage  # the stage this row describes
            Action  = [string]$Step.action  # ran, skip, adopt or failed
            Where   = [string]$Step.where  # whether it ran locally or through delegation
            Reasons = ConvertTo-NarrationStringArray $Step.reasons  # the planner's reasons for the action, as text
            Seconds = $Step.seconds  # time the stage took in seconds (one decimal), or $null if it did not run
            Result  = $Step.result  # whatever the stage returned (files written, counts), as parsed JSON
            Error   = $Step.error  # error text when the stage failed
            Project = $Project  # project folder, so each row still knows its project when piped on
        })
}

# Turns the whole `speechkit-voice-tts-offline-pipeline run` result into a SpeechKitVoiceTtsOfflinePipeline.RunResult for Invoke-NarrationPipeline. -Result is the parsed
# JSON. Returns the object with Project, Ran (stage names), Stopped (stage and reason, or $null) and Steps.
function ConvertTo-NarrationRunResult {
    param($Result)  # the parsed run result
    $project = [string]$Result.project  # the project folder Python reports
    # convert every step, skipping null entries; @() keeps the list an array even with one step
    $steps = @(foreach ($s in @($Result.steps)) { if ($null -ne $s) { ConvertTo-NarrationRunStep -Step $s -Project $project } })
    New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.RunResult' -Property ([ordered]@{  # build the SpeechKitVoiceTtsOfflinePipeline.RunResult object
            Project = $project  # the project folder as Python reports it
            Ran     = ConvertTo-NarrationStringArray $Result.ran  # names of the stages that actually ran
            # set when a stage asked to stop (the first run stops after check so book.md can be reviewed)
            Stopped = $Result.stopped
            Steps   = $steps  # the typed per-stage rows
        })
}

# Turns one row of `speechkit-voice-tts-offline-pipeline status` into a SpeechKitVoiceTtsOfflinePipeline.StageStatus for Get-NarrationProject. -Status is the parsed row,
# -Project the folder. Returns the object. Python's 'partial' flag is not carried over.
function ConvertTo-NarrationStageStatus {
    param($Status, [string]$Project)  # status row and project folder
    New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.StageStatus' -Property ([ordered]@{  # build the status row
            Stage    = [string]$Status.stage  # name of the stage this status row describes
            Done     = [bool]$Status.done  # true when the stage has a recorded, complete run
            Finished = $Status.finished  # when it last finished (local time stamp), or $null
            Adopted  = [bool]$Status.adopted  # true when outputs that existed before tracking were accepted as current
            Missing  = ConvertTo-NarrationStringArray $Status.missing  # declared outputs that are not on disk
            Error    = $Status.error  # last error recorded for the stage, or $null
            Project  = $Project  # project folder this row describes
        })
}

# Turns the result of `speechkit-voice-tts-offline-pipeline tool NAME` into a SpeechKitVoiceTtsOfflinePipeline.ToolResult. -Result is the parsed JSON. Returns the object
# with Tool and Files first, followed by whatever other keys the tool returned (pages, sheets, warnings and so on).
function ConvertTo-NarrationToolResult {
    param($Result)  # the parsed tool result
    # start with the tool name and the files it wrote
    $props = [ordered]@{ Tool = [string]$Result.tool; Files = ConvertTo-NarrationStringArray $Result.files }
    foreach ($p in $Result.PSObject.Properties) {  # every top-level key of the result
        if (@('ok', 'tool', 'files') -contains $p.Name) { continue }  # skip the keys already handled and the ok flag
        $props[$p.Name] = $p.Value  # copy the rest under their own names
    }
    New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.ToolResult' -Property $props  # build the result object
}
