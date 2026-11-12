# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# Public command: lists the pipeline stages from the registry (`python -m speechkit_narrate stages`): what each needs, reads and
# writes, its platforms, whether it runs here, is delegated or is blocked, and its options. -Name filters by name
# (wildcards allowed), -Project includes a project's own plug-ins, -Plugin loads extra plug-ins for this call.
# Returns SpeechKitVoiceTtsOfflinePipeline.Stage objects in run order. Read-only.
function Get-NarrationStage {
    <#
    .SYNOPSIS
    Lists the pipeline stages: what each needs, reads, writes, where it can run, and its options.

    .DESCRIPTION
    Reads the stage registry (python -m speechkit_narrate stages). With -Project the project's own plug-ins (project.json
    "plugins" and its plugins\ folder) are included; plug-ins in $env:SPEECHKIT_PLUGINS always are.

    .PARAMETER Name
    Only these stages (wildcards allowed). Tab completion offers the registry's stage names.

    .PARAMETER Project
    A project folder whose plug-ins should be included.

    .PARAMETER Plugin
    Extra plug-ins to load for this call: .py files or Python module names.

    .EXAMPLE
    Get-NarrationStage

    The nine built-in stages (extract, check, script, prevoice, narrate, autofix, audit, report, export) in run
    order, with what each needs.

    .EXAMPLE
    Get-NarrationStage -Project .\projects\my-book | Where-Object Source -ne 'speechkit-narrate'

    Only the stages that come from plug-ins.

    .EXAMPLE
    (Get-NarrationStage narrate).Options

    The options the narrate stage takes (they are also parameters of Invoke-NarrationStage -Name narrate).

    .OUTPUTS
    SpeechKitVoiceTtsOfflinePipeline.Stage
    #>
    [CmdletBinding()]
    [OutputType('SpeechKitVoiceTtsOfflinePipeline.Stage')]  # declares the output type, so Get-Help and property completion know what comes back
    param(  # the command's parameters: -Name, -Project and -Plugin
        [Parameter(Position = 0)]  # -Name may be given first without its name
        [SupportsWildcards()]  # tells Get-Help and tab completion that wildcards work here
        [string[]]$Name,  # stage names or patterns such as 'nar*'

        [Parameter(ValueFromPipelineByPropertyName = $true)]  # the project can come from piped objects
        [Alias('Path', 'FullName')]  # -Path, or a piped object's FullName, also fill it
        [string]$Project,  # project folder whose plug-ins should be included

        [string[]]$Plugin  # .py files or module names to load for this call only
    )
    process {  # once per piped project (or once in total)
        try {  # failures become this command's terminating error
            $proj = $null  # no project unless one is given
            # absolute folder path, resolved against the current PowerShell location (~ allowed)
            if ($Project) { $proj = Resolve-NarrationPath $Project }
            # a fresh registry read without -Cached, so newly added plug-ins show up at once
            $reg = Get-NarrationRegistry -Project $proj -Plugin $Plugin
            foreach ($s in @($reg.stages)) {  # every stage, in run order
                if ($Name) {  # a name filter was given
                    $hit = $false  # assume no pattern matches this stage until one does
                    # keep the stage when any pattern matches (case-insensitive -like)
                    foreach ($n in $Name) { if ([string]$s.name -like $n) { $hit = $true } }
                    if (-not $hit) { continue }  # no pattern matched: leave it out
                }
                # typed SpeechKitVoiceTtsOfflinePipeline.Stage with Where computed from the registry's delegate
                ConvertTo-NarrationStage -Stage $s -Registry $reg -Project $proj
            }
        }
        catch { $PSCmdlet.ThrowTerminatingError($_) }  # rethrow as this command's error
    }
}

# Public command: runs one stage of a project now, even if it is up to date, after anything stale it needs (like
# make); -Only runs it alone. -Path is the project, -Name the stage (tab-completes), -Option the stage's options as a
# hashtable, -AcceptDraft skips the review stop. Once -Name is known, each stage option also becomes a parameter of
# its own (dynamic parameters such as -Workers or -Format). Supports -WhatIf. Returns a SpeechKitVoiceTtsOfflinePipeline.StageResult.
function Invoke-NarrationStage {
    <#
    .SYNOPSIS
    Runs one stage of a project now, even if it is up to date.

    .DESCRIPTION
    Like python -m speechkit_narrate <stage> <project>, and like make: whatever the stage needs that is stale runs first,
    then the stage itself runs even if it is up to date. -Only runs just the stage, without what it needs.
    Stage names tab-complete from the registry, including the project's plug-ins. The stage's own options become
    parameters once -Name is given (for example -Workers for narrate, -Takes and -Retakes for autofix); -Option
    takes them as a hashtable instead.

    Stages that cannot run on this machine (prevoice, narrate, autofix, audit on Windows) are delegated to WSL in Auto mode.
    Log lines stream to the verbose stream: add -Verbose to watch.

    .PARAMETER Path
    The project folder.

    .PARAMETER Name
    The stage to run.

    .PARAMETER Option
    The stage's options as a hashtable, e.g. @{ workers = 4 }.

    .PARAMETER AcceptDraft
    Narrate the extracted draft without stopping for review (check stage).

    .PARAMETER Only
    Run just this stage, not the stale stages it needs.

    .EXAMPLE
    Invoke-NarrationStage -Path .\projects\my-book -Name script

    Builds the narration script again (after extracting and checking the text if those are stale).

    .EXAMPLE
    Invoke-NarrationStage .\projects\my-book autofix -Takes 5 -Retakes 2 -Verbose

    Runs the pronunciation loop with five takes per candidate fix, showing the log.

    .EXAMPLE
    Invoke-NarrationStage .\projects\my-book narrate -Option @{ workers = 4 }

    .OUTPUTS
    SpeechKitVoiceTtsOfflinePipeline.StageResult
    #>
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
    # declares SpeechKitVoiceTtsOfflinePipeline.StageResult as the output type for help and property completion
    [OutputType('SpeechKitVoiceTtsOfflinePipeline.StageResult')]
    param(  # the static parameters; the stage's own options are added by dynamicparam below
        # required project folder, first position, bindable from piped objects
        [Parameter(Mandatory = $true, Position = 0, ValueFromPipelineByPropertyName = $true)]
        [Alias('Project', 'FullName')]  # Project or FullName on piped objects also bind
        [string]$Path,  # the project folder, relative to the current location or absolute

        [Parameter(Mandatory = $true, Position = 1)]  # required stage name, second position
        [string]$Name,  # the stage to run

        [hashtable]$Option,  # stage options as a hashtable, e.g. @{ workers = 4 }

        [switch]$AcceptDraft,  # carry on past the review stop

        [switch]$Only  # skip the stale stages it needs
    )
    # PowerShell runs this block while binding, during tab completion and for Get-Help, once the static parameters
    # typed so far are known. It returns the stage's options as runtime parameters, or nothing.
    dynamicparam {
        # without a stage name there is nothing to add
        if (-not $PSBoundParameters.ContainsKey('Name') -or -not $PSBoundParameters['Name']) { return }
        try {  # a failure here must never break binding
            $proj = $null  # project folder, if one can be found
            if ($PSBoundParameters['Path']) {  # a path was typed
                # absolute path of what was typed so far, which need not exist yet
                $candidate = Resolve-NarrationPath ([string]$PSBoundParameters['Path'])
                # use it only if it is really a project, so its plug-in stages count
                if (Test-Path -LiteralPath (Join-Path $candidate 'project.json')) { $proj = $candidate }
            }
            # the registry, reused if it is less than a minute old because this block runs often
            $reg = Get-NarrationRegistry -Project $proj -Cached
            # the stage being asked for
            $stage = @($reg.stages | Where-Object { $_.name -eq $PSBoundParameters['Name'] }) | Select-Object -First 1
            if (-not $stage -or -not $stage.options) { return }  # unknown stage, or a stage without options
            # the collection PowerShell expects from a dynamicparam block
            $dict = New-Object System.Management.Automation.RuntimeDefinedParameterDictionary
            # one spec per option; names this command already uses (and their aliases) are reserved so options get
            # renamed
            foreach ($spec in (Get-NarrationParameterSpec -Properties $stage.options -Taken @('Path', 'Name', 'Option', 'AcceptDraft', 'Only', 'Project', 'FullName'))) {
                # register the option as a runtime parameter, typed and validated from its schema
                $dict.Add($spec.Name, (New-NarrationRuntimeParameter -Spec $spec))
            }
            $dict  # hand the parameters to PowerShell
        }
        # no dynamic parameters this time; explain why under -Debug
        catch { Write-Debug "speechkit-voice-tts-offline-pipeline: no dynamic parameters for stage $($PSBoundParameters['Name']): $_" }
    }
    process {  # once per piped project
        try {  # any failure becomes this command's terminating error
            $proj = Resolve-NarrationPath $Path  # absolute project folder, resolved against the current location
            $reg = Get-NarrationRegistry -Project $proj -Cached  # registry, with the one-minute cache
            # the stage's description, if the name is known
            $stage = @($reg.stages | Where-Object { $_.name -eq $Name }) | Select-Object -First 1
            $props = $null  # the stage's options schema, unknown until the stage is found
            if ($stage) { $props = $stage.options }  # available when the stage was found
            $opts = [ordered]@{}  # the options to send, keyed by the schema's names
            if ($Option) {  # options given as a hashtable
                # map snake_case or PascalCase keys onto the schema and convert the values
                $converted = ConvertTo-NarrationParameterSet -Parameters $Option -Properties $props
                # copy the converted -Option entries into the options to send
                foreach ($k in $converted.Keys) { $opts[$k] = $converted[$k] }
            }
            if ($props) {  # the stage declares options
                # the same specs as in dynamicparam, so the parameter names match
                foreach ($spec in (Get-NarrationParameterSpec -Properties $props -Taken @('Path', 'Name', 'Option', 'AcceptDraft', 'Only', 'Project', 'FullName'))) {
                    if ($PSBoundParameters.ContainsKey($spec.Name)) {  # this option was given as a parameter
                        # convert it and store it under the schema's key; it wins over the same key in -Option
                        $opts[$spec.Key] = ConvertTo-NarrationSchemaValue -Value $PSBoundParameters[$spec.Name] -Schema $spec.Schema
                    }
                }
            }
            # -WhatIf only reports what would run and stops here; -Confirm asks first
            if (-not $PSCmdlet.ShouldProcess("$Name in $proj", 'Run stage')) { return }
            # run DIR NAME --force=NAME is what `python -m speechkit_narrate <stage> DIR` does, and it also sees the stages
            # that come from the project's own plug-ins.
            $cliArgs = @('run', (ConvertTo-NarrationBackendPath -Path $proj))  # the run command for this project
            # --only=NAME runs it alone; otherwise NAME is a target and its stale needs run first
            if ($Only) { $cliArgs += "--only=$Name" } else { $cliArgs += $Name }
            $cliArgs += "--force=$Name"  # force it even when it is up to date
            if ($AcceptDraft) { $cliArgs += '--accept-draft' }  # skip the review stop if asked
            # options as JSON keyed by this stage's name
            if ($opts.Count) { $cliArgs += "--options=$(ConvertTo-NarrationJson @{ $Name = $opts })" }
            # run, with the progress bar named after the stage
            $r = Invoke-NarrationCli -Arguments $cliArgs -Activity "speechkit-voice-tts-offline-pipeline $Name" -Target $proj
            # warn when the run stopped early (for example at the review stop before this stage)
            if ($r.stopped) { Write-Warning "speechkit-voice-tts-offline-pipeline stopped after $($r.stopped.stage): $($r.stopped.why)" }
            # the step for the requested stage
            $step = @($r.steps | Where-Object { $_.stage -eq $Name }) | Select-Object -First 1
            $props = [ordered]@{  # the result properties, filled in below
                Project = [string]$r.project  # folder as Python reports it
                Stage   = $Name  # the stage that was asked for, as typed
                Action  = $null  # what happened to it, once known
                Reasons = [string[]]@()  # the planner's reasons for running or skipping it, empty until known
                Seconds = $null  # time the stage took
                Result  = $null  # data the stage returned
                Stopped = $r.stopped  # stage and reason if the run stopped early
                Ran     = ConvertTo-NarrationStringArray $r.ran  # every stage that ran, needs included
            }
            if ($step) {  # the stage appears in the run's steps
                $props.Action = [string]$step.action  # ran, skip, adopt or failed
                $props.Reasons = ConvertTo-NarrationStringArray $step.reasons  # the planner's reasons as a string array
                $props.Seconds = $step.seconds  # how many seconds the stage took, or $null when it did not run
                $props.Result = $step.result  # the stage's own result data
            }
            # emit the result as a typed SpeechKitVoiceTtsOfflinePipeline.StageResult object
            New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.StageResult' -Property $props
        }
        catch { $PSCmdlet.ThrowTerminatingError($_) }  # rethrow as this command's error
    }
}
