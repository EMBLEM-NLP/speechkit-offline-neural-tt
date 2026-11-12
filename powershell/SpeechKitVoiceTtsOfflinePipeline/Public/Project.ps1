# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# Public command: creates a project folder for one text with `python -m speechkit_narrate new`: project.json plus a copy
# of the input in input\. -Path is the new folder, -InputFile the .txt, .md or SpeechKit request .json, -Title,
# -Subtitle and -Author fill the settings, -Set adds any other project.json settings (hashtable or JSON text, passed
# as --set). Supports -WhatIf. Writes files on disk and returns a SpeechKitVoiceTtsOfflinePipeline.ProjectInfo (Project, Title,
# Settings), which can be piped on to Invoke-NarrationPipeline.
function New-NarrationProject {
    <#
    .SYNOPSIS
    Makes a project folder for one text from a .txt, .md or SpeechKit request .json file.

    .DESCRIPTION
    Creates <Path>\project.json and copies the input into <Path>\input\. Then run Invoke-NarrationPipeline: the
    first run extracts the text and stops so that work\book.md can be reviewed (or pass -AcceptDraft to go straight
    on).

    .PARAMETER Path
    The new project folder.

    .PARAMETER InputFile
    The text: a .txt, .md or SpeechKit request .json file.

    .PARAMETER Title
    The title (default: the input file's name).

    .PARAMETER Subtitle
    The subtitle.

    .PARAMETER Author
    The author.

    .PARAMETER Set
    More project.json settings, merged in when the project is made: a hashtable such as
    @{ spoken = @{ code_names = $true } }, or the same as JSON text.

    .EXAMPLE
    New-NarrationProject -Path .\projects\my-text -InputFile .\MyText.md -Title 'My Text' -Subtitle 'A subtitle'

    .EXAMPLE
    New-NarrationProject .\projects\guide .\guide.md -Set @{ spoken = @{ code_names = $true }; export = @{ format = 'mp3' } } | Invoke-NarrationPipeline -Target script -AcceptDraft

    Makes the project with code names read aloud and mp3 export, and prepares the narration script in one go.

    .OUTPUTS
    SpeechKitVoiceTtsOfflinePipeline.ProjectInfo
    #>
    # -WhatIf and -Confirm; Low impact, so it only asks when -Confirm is given
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
    # tells Get-Help and tab completion that this command emits SpeechKitVoiceTtsOfflinePipeline.ProjectInfo objects
    [OutputType('SpeechKitVoiceTtsOfflinePipeline.ProjectInfo')]
    param(  # parameters: the folder and the input file are required, everything else is optional
        [Parameter(Mandatory = $true, Position = 0)]  # required, and may be given first without its name
        [string]$Path,  # folder to create (relative paths are fine)

        [Parameter(Mandatory = $true, Position = 1)]  # required, second positional argument
        [Alias('Input')]  # -Input also works
        [string]$InputFile,  # path of the .txt, .md or request .json; it is copied into the project's input folder

        [string]$Title,  # title; Python uses the file name when it is not given
        [string]$Subtitle,  # subtitle saved in project.json
        [string]$Author,  # author name saved in project.json
        [object]$Set  # more settings: a hashtable (or PSCustomObject) or JSON text, sent as --set
    )
    try {  # any failure becomes this command's terminating error
        # absolute path for the new folder, resolved from the current location even before it exists
        $dir = Resolve-NarrationPath $Path
        # the `new` subcommand with the folder in the backend's form
        $cliArgs = @('new', (ConvertTo-NarrationBackendPath -Path $dir))
        $f = Resolve-NarrationPath $InputFile  # the input made absolute against the current location
        if (-not (Test-Path -LiteralPath $f -PathType Leaf)) {  # it must be an existing file
            # stop with a not-found error naming the missing file, before Python is started
            $PSCmdlet.ThrowTerminatingError((New-NarrationErrorRecord -Message "The input file '$f' does not exist." -Type 'FileNotFoundError' -Target $f))
        }
        $cliArgs += "--input=$(ConvertTo-NarrationBackendPath -Path $f)"  # translated for the Wsl backend
        foreach ($k in @('Title', 'Subtitle', 'Author')) {  # the three text settings
            # add --title= (and so on) only when the caller passed it; the key is lower-cased to match the CLI option
            if ($PSBoundParameters.ContainsKey($k)) { $cliArgs += "--$($k.ToLowerInvariant())=$($PSBoundParameters[$k])" }
        }
        if ($PSBoundParameters.ContainsKey('Set') -and $null -ne $Set) {  # extra settings were given
            if ($Set -is [string]) {  # JSON text: checked here so a typo fails before Python runs
                try { $parsed = ConvertFrom-NarrationJson -Json $Set } catch { $parsed = $null }
                if ($parsed -isnot [System.Management.Automation.PSCustomObject]) {  # not a JSON object
                    $PSCmdlet.ThrowTerminatingError((New-NarrationErrorRecord -Message "-Set must be a hashtable or a JSON object, not: $Set" -Type 'ValueError' -Target $Set))
                }
                $json = ConvertTo-NarrationJson $parsed  # re-serialised compactly so the argument is one clean line
            }
            else { $json = ConvertTo-NarrationJson $Set }  # a hashtable or object becomes compact JSON
            $cliArgs += "--set=$json"  # merged into project.json by Python
        }
        # -WhatIf ends here; checks above have already run
        if (-not $PSCmdlet.ShouldProcess($dir, 'Create speechkit-voice-tts-offline-pipeline project')) { return }
        # runs python -m speechkit_narrate new, which writes project.json and copies the input, then returns JSON
        $r = Invoke-NarrationCli -Arguments $cliArgs -Target $dir
        New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.ProjectInfo' -Property ([ordered]@{  # describe the new project
                Project  = [string]$r.project  # its folder as Python reports it
                Title    = [string]$r.settings.title  # the title stored in project.json
                Settings = $r.settings  # the full settings Python wrote
            })
    }
    catch { $PSCmdlet.ThrowTerminatingError($_) }  # rethrow as the command's own error
}

# Public command: shows a project's state with `python -m speechkit_narrate status`: which stages are done, any errors, the
# narration progress and the exported files. -Path is the project folder; it also binds from piped objects with a
# Project or FullName property (Get-ChildItem folders, earlier results). Returns one SpeechKitVoiceTtsOfflinePipeline.Project per folder.
function Get-NarrationProject {
    <#
    .SYNOPSIS
    Shows what is done in a project: every stage's state, the narration progress and the exported files.

    .PARAMETER Path
    The project folder.

    .EXAMPLE
    Get-NarrationProject .\projects\my-book

    .EXAMPLE
    (Get-NarrationProject .\projects\my-book).Stages | Where-Object { -not $_.Done }

    The stages that have not run yet.

    .EXAMPLE
    Get-ChildItem .\projects -Directory | Get-NarrationProject | Select-Object Title, Done

    .OUTPUTS
    SpeechKitVoiceTtsOfflinePipeline.Project
    #>
    [CmdletBinding()]
    [OutputType('SpeechKitVoiceTtsOfflinePipeline.Project')]  # declares the SpeechKitVoiceTtsOfflinePipeline.Project type so help and IntelliSense know what comes out
    param(  # a single parameter: the project folder, which may also arrive through the pipeline
        # required, positional and taken from piped objects by property name
        [Parameter(Mandatory = $true, Position = 0, ValueFromPipelineByPropertyName = $true)]
        [Alias('Project', 'FullName')]  # piped objects may carry the folder as Project or FullName
        [string]$Path  # folder of the project to report on, absolute or relative to the current location
    )
    process {  # runs once per piped folder
        try {  # errors stop this command with its own error record
            $proj = Resolve-NarrationPath $Path  # resolve the folder against the current location
            # run `status` for the folder
            $r = Invoke-NarrationCli -Arguments @('status', (ConvertTo-NarrationBackendPath -Path $proj)) -Target $proj
            $project = [string]$r.project  # the folder as Python reports it
            # one typed row per stage, skipping null entries
            $stages = @(foreach ($s in @($r.stages)) { if ($null -ne $s) { ConvertTo-NarrationStageStatus -Status $s -Project $project } })
            # build the typed status object; SpeechKitVoiceTtsOfflinePipeline.Format.ps1xml supplies its table view
            New-NarrationObject -TypeName 'SpeechKitVoiceTtsOfflinePipeline.Project' -Property ([ordered]@{
                    Title     = [string]$r.title  # title from project.json
                    # named Project so the object can be piped straight into other SpeechKitVoiceTtsOfflinePipeline commands
                    Project   = $project
                    # names of the stages that are done
                    Done      = [string[]]@($stages | Where-Object { $_.Done } | ForEach-Object { $_.Stage })
                    # "stage: message" for each stage whose last run failed
                    Errors    = [string[]]@($stages | Where-Object { $_.Error } | ForEach-Object { "$($_.Stage): $($_.Error)" })
                    # narration progress from the voice module (present once a narration request exists)
                    Narration = $r.narration
                    Exported  = ConvertTo-NarrationStringArray $r.exported  # file names in out\narration (audio, timings, manifest)
                    Stages    = $stages  # the full per-stage rows
                })
        }
        catch { $PSCmdlet.ThrowTerminatingError($_) }  # rethrown as a terminating error of Get-NarrationProject
    }
}

# Public command: shows what Invoke-NarrationPipeline would do with `python -m speechkit_narrate plan`: one row per stage with
# its action (run, skip, adopt or blocked), where it runs and why. -Path is the project, -Target the stages to bring up
# to date, -Force, -Only, -Skip, -From and -NoAdopt the planner's flags. Returns SpeechKitVoiceTtsOfflinePipeline.PlanStep objects; it never
# changes the project.
function Get-NarrationPlan {
    <#
    .SYNOPSIS
    Shows what Invoke-NarrationPipeline would do, stage by stage, and why.

    .DESCRIPTION
    Each step's Action is run (never ran, an output is missing, its settings/inputs/implementation changed, a
    stage it needs runs, or forced), skip (up to date, or skipped on request), adopt (outputs made before
    tracking began are recorded as current) or blocked (cannot run on this machine and nothing to delegate to).

    .PARAMETER Path
    The project folder.

    .PARAMETER Target
    Stages to bring up to date (with what they need). Default: the whole pipeline.

    .PARAMETER Force
    Run these stages even if they are up to date ('all' for every one).

    .PARAMETER Only
    Run just these stages, without what they need.

    .PARAMETER Skip
    Leave these stages out.

    .PARAMETER From
    Force this stage and everything after it.

    .PARAMETER NoAdopt
    Do not adopt outputs made before tracking began.

    .EXAMPLE
    Get-NarrationPlan .\projects\my-book

    .EXAMPLE
    Get-NarrationPlan .\projects\my-book -Target script | Where-Object Action -eq 'run'

    .OUTPUTS
    SpeechKitVoiceTtsOfflinePipeline.PlanStep
    #>
    [CmdletBinding()]
    [OutputType('SpeechKitVoiceTtsOfflinePipeline.PlanStep')]  # declares that one SpeechKitVoiceTtsOfflinePipeline.PlanStep object is emitted per pipeline stage
    param(  # the same stage selection switches as Invoke-NarrationPipeline, minus the run-only ones
        # the folder: mandatory, first, and bindable from the pipeline
        [Parameter(Mandatory = $true, Position = 0, ValueFromPipelineByPropertyName = $true)]
        [Alias('Project', 'FullName')]  # also accepts Project or FullName from piped objects
        [string]$Path,  # folder of the project to plan; relative paths resolve against the current location

        [Parameter(Position = 1)]  # the second positional argument
        [string[]]$Target,  # stages to bring up to date; empty means the whole pipeline

        [string[]]$Force,  # stages to run even when up to date ('all' for every stage)
        [string[]]$Only,  # stages to run alone, without what they need
        [string[]]$Skip,  # stages to leave out
        [string]$From,  # force this stage and all later ones
        [switch]$NoAdopt  # do not adopt outputs made before tracking began
    )
    process {  # once per piped folder
        try {  # wrap failures as this command's error
            $proj = Resolve-NarrationPath $Path  # full path of the project
            # `plan`, the folder, the targets (empty ones dropped) and (on the next line) the planner flags
            $cliArgs = @('plan', (ConvertTo-NarrationBackendPath -Path $proj)) + @($Target | Where-Object { $_ }) +
                # --force, --only, --skip, --from and --no-adopt as given
                @(Get-NarrationRunArgument -Force $Force -Only $Only -Skip $Skip -From $From -NoAdopt:$NoAdopt)
            $r = Invoke-NarrationCli -Arguments $cliArgs -Target $proj  # ask Python for the plan
            # one SpeechKitVoiceTtsOfflinePipeline.PlanStep per step, labelled with the project folder
            foreach ($s in @($r.steps)) { if ($null -ne $s) { ConvertTo-NarrationPlanStep -Step $s -Project ([string]$r.project) } }
        }
        catch { $PSCmdlet.ThrowTerminatingError($_) }  # rethrown so the error names Get-NarrationPlan
    }
}

# Public command (alias Invoke-Narration): brings a project up to date with `python -m speechkit_narrate run`; only stages whose
# outputs are missing or whose inputs, settings or code changed run. It first asks for the plan so -WhatIf can show
# every stage and -Confirm can ask per stage (declined stages are passed as --skip). -Target, -Force, -Only, -Skip,
# -From and -NoAdopt shape the plan, -AcceptDraft skips the review stop, -Option passes stage options. The log streams
# to -Verbose and stage starts to the progress bar. Returns a SpeechKitVoiceTtsOfflinePipeline.RunResult. Writes the project's outputs.
function Invoke-NarrationPipeline {
    <#
    .SYNOPSIS
    Brings a project's stages up to date: only what is missing or changed runs.

    .DESCRIPTION
    Runs python -m speechkit_narrate run. The first run of a new project stops after the check stage so that
    work\book.md can be reviewed; -AcceptDraft goes straight on. Log lines stream to the verbose stream
    (-Verbose), and each stage start shows in the progress bar and the information stream.

    -WhatIf shows the plan (each stage with the action and its reasons) and runs nothing. -Confirm asks per
    stage; stages you decline are skipped.

    On Windows in Auto mode, narrate/autofix/audit run in WSL (see Set-NarrationBackend).

    .PARAMETER Path
    The project folder.

    .PARAMETER Target
    Stages to bring up to date (with what they need). Default: the whole pipeline.

    .PARAMETER Force
    Run these stages even if they are up to date ('all' for every one).

    .PARAMETER Only
    Run just these stages, without what they need.

    .PARAMETER Skip
    Leave these stages out.

    .PARAMETER From
    Force this stage and everything after it.

    .PARAMETER AcceptDraft
    Do not stop for the review of work\book.md.

    .PARAMETER Option
    Stage options: a hashtable of hashtables keyed by stage, e.g. @{ narrate = @{ workers = 4 } }.

    .PARAMETER NoAdopt
    Do not adopt outputs made before tracking began.

    .EXAMPLE
    Invoke-NarrationPipeline .\projects\my-book -Target script -AcceptDraft

    Extracts and checks the text and builds the narration script, nothing else.

    .EXAMPLE
    Invoke-Narration .\projects\my-book -WhatIf

    Shows what a full run would do.

    .EXAMPLE
    Invoke-Narration .\projects\my-book -From script -Option @{ narrate = @{ workers = 4 } } -Verbose

    Redoes everything from the script stage on, narrating with four workers, with the log on screen.

    .OUTPUTS
    SpeechKitVoiceTtsOfflinePipeline.RunResult
    #>
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
    [OutputType('SpeechKitVoiceTtsOfflinePipeline.RunResult')]  # declares one SpeechKitVoiceTtsOfflinePipeline.RunResult summary object per project run
    param(  # the plan selection parameters plus -AcceptDraft and -Option for the actual run
        # mandatory folder, first position, bindable by property name from the pipeline
        [Parameter(Mandatory = $true, Position = 0, ValueFromPipelineByPropertyName = $true)]
        [Alias('Project', 'FullName')]  # piped objects may name the folder as Project or FullName
        [string]$Path,  # folder of the project to run, typed on the command line or taken from a piped object

        [Parameter(Position = 1)]  # lets the stage list be given as the second unnamed argument after the folder
        [string[]]$Target,  # stages to bring up to date (with what they need); empty means everything

        [string[]]$Force,  # run even if up to date; 'all' forces every stage
        [string[]]$Only,  # run only these, ignoring their needs
        [string[]]$Skip,  # stage names to leave out of this run even when they are out of date
        [string]$From,  # redo this stage and everything after it
        [switch]$AcceptDraft,  # do not stop after extraction for the review of work\book.md
        [hashtable]$Option,  # stage options keyed by stage name, e.g. @{ narrate = @{ workers = 4 } }
        [switch]$NoAdopt  # no adoption of outputs that existed before tracking
    )
    process {  # runs for each piped project
        try {  # any failure becomes this command's terminating error
            $proj = Resolve-NarrationPath $Path  # project path made absolute
            $dir = ConvertTo-NarrationBackendPath -Path $proj  # the folder in the backend's form
            $targets = @($Target | Where-Object { $_ })  # stage names, with empty entries removed
            # planner flags shared by plan and run
            $flags = @(Get-NarrationRunArgument -Force $Force -Only $Only -Skip $Skip -From $From -NoAdopt:$NoAdopt)
            # ask for the plan first; it drives -WhatIf and -Confirm
            $plan = Invoke-NarrationCli -Arguments (@('plan', $dir) + $targets + $flags) -Target $proj
            $declined = @()  # stages the user declined under -Confirm
            $toRun = 0  # number of stages that will run, for the progress percentage
            foreach ($s in @($plan.steps)) {  # walk the planned steps
                if ($null -eq $s) { continue }  # @() around a missing steps list yields one null item, so it is ignored
                $reasons = (@($s.reasons) -join '; ')  # the planner's reasons as one string
                $where = ''  # suffix for delegated stages
                # the stage will run in WSL through SPEECHKIT_DELEGATE
                if ($s.where -eq 'delegate') { $where = ' (delegated)' }
                # e.g. "run (delegated): never ran"; the backtick stops PowerShell reading $where: as a drive-qualified
                # variable
                $operation = "$($s.action)$where`: $reasons"
                $targetText = "stage $($s.stage) of $proj"  # what the ShouldProcess message refers to
                # steps that do something (run, adopt) or will fail (blocked) go through ShouldProcess
                if (@('run', 'adopt', 'blocked') -contains $s.action) {
                    # true on a normal run; under -Confirm the user is asked
                    if ($PSCmdlet.ShouldProcess($targetText, $operation)) {
                        if ($s.action -eq 'run') { $toRun++ }  # count the stages that will really run
                    }
                    # declined under -Confirm (not -WhatIf): remember it so the run skips that stage
                    elseif (-not $WhatIfPreference -and $s.action -eq 'run') { $declined += [string]$s.stage }
                }
                # under -WhatIf also show the steps that are already up to date, so the whole plan appears
                elseif ($WhatIfPreference) {
                    # prints "What if: ..." and returns false; the result is not needed
                    [void]$PSCmdlet.ShouldProcess($targetText, $operation)
                }
            }
            if ($WhatIfPreference) { return }  # -WhatIf: the plan has been shown, nothing runs
            # the `run` command with the same targets and flags as the plan
            $runArgs = @('run', $dir) + $targets + $flags
            foreach ($d in $declined) { $runArgs += "--skip=$d" }  # leave out every declined stage
            if ($AcceptDraft) { $runArgs += '--accept-draft' }  # go past the review stop after extraction
            # stage options as one compact JSON argument
            if ($Option -and $Option.Count) { $runArgs += "--options=$(ConvertTo-NarrationJson $Option)" }
            # run it; the progress bar is titled with the project folder's name and scaled by the stage count
            $r = Invoke-NarrationCli -Arguments $runArgs -Activity "speechkit-voice-tts-offline-pipeline run $(Split-Path -Leaf $proj)" -Total $toRun -Target $proj
            # a stage asked to stop (normally the review stop after check); say so
            if ($r.stopped) { Write-Warning "speechkit-voice-tts-offline-pipeline stopped after $($r.stopped.stage): $($r.stopped.why)" }
            ConvertTo-NarrationRunResult -Result $r  # return the typed run result
        }
        catch { $PSCmdlet.ThrowTerminatingError($_) }  # rethrown as an Invoke-NarrationPipeline error
    }
}

# Public command: reads a project's settings with `python -m speechkit_narrate settings`. -Path is the project folder;
# -Effective returns every setting with its default filled in rather than only what project.json sets.
# Returns the settings object (nested objects such as spoken, voice and export) tagged as SpeechKitVoiceTtsOfflinePipeline.Settings.
function Get-NarrationSetting {
    <#
    .SYNOPSIS
    Reads a project's settings (project.json).

    .PARAMETER Path
    The project folder.

    .PARAMETER Effective
    Every setting with its default filled in, not only the ones project.json sets.

    .EXAMPLE
    Get-NarrationSetting .\projects\my-book

    .EXAMPLE
    (Get-NarrationSetting .\projects\my-book -Effective).export

    The export settings in force (format, bit rate, normalisation, lead and tail silence).

    .OUTPUTS
    SpeechKitVoiceTtsOfflinePipeline.Settings
    #>
    [CmdletBinding()]
    [OutputType('SpeechKitVoiceTtsOfflinePipeline.Settings')]  # the declared output type
    param(  # inputs: the folder and the -Effective switch
        # the folder, mandatory and positional, also from piped objects
        [Parameter(Mandatory = $true, Position = 0, ValueFromPipelineByPropertyName = $true)]
        [Alias('Project', 'FullName')]  # Project or FullName properties bind too
        [string]$Path,  # the project whose settings are read

        [switch]$Effective  # with -Effective every default is filled in, not only the keys project.json sets
    )
    process {  # once per piped project
        try {  # report failures as this command's error
            $proj = Resolve-NarrationPath $Path  # the folder as an absolute path
            # `settings` with the folder in the backend's form
            $cliArgs = @('settings', (ConvertTo-NarrationBackendPath -Path $proj))
            if ($Effective) { $cliArgs += '--effective' }  # --effective merges the defaults in
            # runs python -m speechkit_narrate settings and parses the JSON it prints
            $r = Invoke-NarrationCli -Arguments $cliArgs -Target $proj
            $s = $r.settings  # the settings object itself
            # an empty project.json still yields an object rather than nothing
            if ($null -eq $s) { $s = New-Object PSObject }
            $s.PSObject.TypeNames.Insert(0, 'SpeechKitVoiceTtsOfflinePipeline.Settings')  # tag it so it is recognisable as SpeechKitVoiceTtsOfflinePipeline.Settings
            $s  # emit the settings object to the pipeline as this command's result
        }
        catch { $PSCmdlet.ThrowTerminatingError($_) }  # rethrown under Get-NarrationSetting's name
    }
}

# Public command: changes a project's settings by deep-merging a hashtable into project.json with
# `python -m speechkit_narrate settings --set JSON`; nested hashtables change only the keys they name. -Path is the project
# folder, -Settings the hashtable, -PassThru returns the merged settings. Supports -WhatIf. Writes project.json.
function Set-NarrationSetting {
    <#
    .SYNOPSIS
    Changes a project's settings: the hashtable is deep-merged into project.json.

    .DESCRIPTION
    Nested hashtables merge key by key, so @{ export = @{ format = 'mp3' } } changes only export.format. Stages whose
    settings changed run again on the next Invoke-NarrationPipeline (see Get-NarrationPlan).

    .PARAMETER Path
    The project folder.

    .PARAMETER Settings
    The settings to merge in.

    .PARAMETER PassThru
    Return the project's settings afterwards.

    .EXAMPLE
    Set-NarrationSetting .\projects\my-book @{ export = @{ format = 'mp3'; kbps = 128 } }

    .EXAMPLE
    Set-NarrationSetting .\projects\my-book -Settings @{ spoken = @{ years = $true }; voice = @{ workers = 4 } } -PassThru

    .OUTPUTS
    SpeechKitVoiceTtsOfflinePipeline.Settings
    #>
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
    [OutputType('SpeechKitVoiceTtsOfflinePipeline.Settings')]  # type returned with -PassThru
    param(  # inputs: folder, settings hashtable and -PassThru
        # mandatory folder, first position, can come from the pipeline
        [Parameter(Mandatory = $true, Position = 0, ValueFromPipelineByPropertyName = $true)]
        [Alias('Project', 'FullName')]  # property names Project or FullName also bind
        [string]$Path,  # the project to change

        # the settings hashtable is required and may follow the folder without naming -Settings
        [Parameter(Mandatory = $true, Position = 1)]
        [System.Collections.IDictionary]$Settings,  # settings to merge, e.g. @{ export = @{ format = 'mp3' } }

        [switch]$PassThru  # return the settings afterwards
    )
    process {  # once per piped project
        try {  # failures become this command's terminating error
            $proj = Resolve-NarrationPath $Path  # absolute path of the project to change
            $json = ConvertTo-NarrationJson $Settings  # the hashtable as compact JSON, identical on 5.1 and 7
            # -WhatIf shows the JSON that would be merged and stops
            if (-not $PSCmdlet.ShouldProcess($proj, "Merge settings $json")) { return }
            # merge it into project.json
            $r = Invoke-NarrationCli -Arguments @('settings', (ConvertTo-NarrationBackendPath -Path $proj), "--set=$json") -Target $proj
            if ($PassThru) {  # nothing is output unless -PassThru asks for the merged settings
                $s = $r.settings  # the merged settings (note: no check for an empty result, unlike Get-NarrationSetting)
                $s.PSObject.TypeNames.Insert(0, 'SpeechKitVoiceTtsOfflinePipeline.Settings')  # tag the object type
                $s  # write the merged settings to the pipeline for the caller to inspect
            }
        }
        catch { $PSCmdlet.ThrowTerminatingError($_) }  # rethrown as a Set-NarrationSetting error
    }
}
