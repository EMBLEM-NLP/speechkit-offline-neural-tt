# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
# Public command: lists the standalone tools (`python -m speechkit_narrate tools`), which work on plain files without a project,
# with their parameters and generated command names. -Name filters (wildcards allowed), -Plugin loads extra plug-ins
# for this call. Returns SpeechKitNarrate.Tool objects and refreshes the module's tool cache.
function Get-NarrationTool {
    <#
    .SYNOPSIS
    Lists the standalone tools (they work on plain files, no project folder) and their parameters.

    .DESCRIPTION
    Each tool also has a generated command (the Command property), e.g. Invoke-NarrationSpeak for speak.
    Plug-in tools from $env:SPEECHKIT_PLUGINS are included; run Update-NarrationCommand to get commands for them.

    .PARAMETER Name
    Only these tools (wildcards allowed).

    .PARAMETER Plugin
    Extra plug-ins to load for this call: .py files or Python module names.

    .EXAMPLE
    Get-NarrationTool

    .EXAMPLE
    (Get-NarrationTool speak).Parameters

    The JSON-schema description of each of the speak tool's parameters.

    .EXAMPLE
    Get-NarrationTool | ForEach-Object { Get-Help $_.Command }

    .OUTPUTS
    SpeechKitNarrate.Tool
    #>
    [CmdletBinding()]
    [OutputType('SpeechKitNarrate.Tool')]  # declares SpeechKitNarrate.Tool as the output type, for help and property completion
    param(  # the parameters: -Name patterns and extra -Plugin files
        [Parameter(Position = 0)]  # -Name may come first without its name
        [SupportsWildcards()]  # wildcard patterns are accepted
        [string[]]$Name,  # tool names or patterns

        [string[]]$Plugin  # extra plug-ins for this call
    )
    try {  # failures become this command's terminating error
        # always ask Python again, so new plug-ins show up
        foreach ($t in @(Get-NarrationToolList -Plugin $Plugin -Refresh)) {
            if ($Name) {  # a filter was given
                $hit = $false  # assume the tool matches no pattern until one does
                # keep the tool when any pattern matches
                foreach ($n in $Name) { if ([string]$t.name -like $n) { $hit = $true } }
                if (-not $hit) { continue }  # skip tools that match none
            }
            ConvertTo-NarrationTool -Tool $t  # typed SpeechKitNarrate.Tool with its generated command name
        }
    }
    catch { $PSCmdlet.ThrowTerminatingError($_) }  # rethrow as this command's error
}

# Public command: runs one standalone tool with `python -m speechkit_narrate tool NAME --params JSON`. -Name is the tool,
# -Parameters a hashtable using the tool's own snake_case names (PascalCase works too), -Plugin extra plug-ins.
# Path parameters are made absolute against the current location. Returns a SpeechKitNarrate.ToolResult whose Files lists
# the files written. The generated Invoke-Narration<Tool> commands all call this.
function Invoke-NarrationTool {
    <#
    .SYNOPSIS
    Runs a standalone tool on plain files (python -m speechkit_narrate tool NAME --params JSON).

    .DESCRIPTION
    Parameters are given as a hashtable with the tool's own (snake_case) names; PascalCase names as used by the
    generated commands work too. Parameters marked as paths are resolved against the current PowerShell location
    (and converted for WSL). Tools that cannot run on this machine (speak, on Windows) run in WSL in Auto mode.
    The generated commands (Invoke-NarrationSpeak, Invoke-NarrationQuick, ...) call this function.

    .PARAMETER Name
    The tool, e.g. speak, quick or a plug-in tool.

    .PARAMETER Parameters
    The tool's parameters, e.g. @{ text_file = '.\chapter.md'; out = '.\chapter.wav'; workers = 4 }.

    .PARAMETER Plugin
    Extra plug-ins to load for this call: .py files or Python module names.

    .EXAMPLE
    Invoke-NarrationTool -Name speak -Parameters @{ text = 'Hello there.'; out = '.\hello.wav' }

    .EXAMPLE
    $r = Invoke-NarrationTool quick @{ input = '.\MyText.md'; out = '.\projects\my-text'; targets = 'script' }
    $r.project

    .OUTPUTS
    SpeechKitNarrate.ToolResult
    #>
    [CmdletBinding()]
    [OutputType('SpeechKitNarrate.ToolResult')]  # declares SpeechKitNarrate.ToolResult as the output type for Get-Help and completion
    param(  # the parameters: the tool -Name, its -Parameters dictionary and extra -Plugin files
        [Parameter(Mandatory = $true, Position = 0)]  # the tool name is required and may be given first without -Name
        [string]$Name,  # tool name, e.g. speak

        [Parameter(Position = 1)]  # the parameters dictionary may follow the name without -Parameters
        # the tool's parameters; any dictionary type works (hashtable or ordered)
        [System.Collections.IDictionary]$Parameters = @{},

        [string[]]$Plugin  # extra plug-ins for this call
    )
    try {  # failures are rethrown below as this command's error
        # look the tool up in the cached list
        $info = @(Get-NarrationToolList -Plugin $Plugin | Where-Object { $_.name -eq $Name }) | Select-Object -First 1
        # not found: refresh once, in case a plug-in was added since
        if (-not $info) { $info = @(Get-NarrationToolList -Plugin $Plugin -Refresh | Where-Object { $_.name -eq $Name }) | Select-Object -First 1 }
        # A tool that cannot run here (speak on Windows) is delegated by the toolkit itself through
        # SPEECHKIT_DELEGATE, which the Auto backend sets; paths are translated there and back by the toolkit.
        $backend = $script:Config.Backend
        $props = $null  # the tool's parameter schema, if known
        # taken from the tool's params
        if ($info -and $info.params) { $props = Get-NarrationSchemaProperty $info.params 'properties' }
        # map the keys onto the schema's names and convert values (paths, switches, numbers)
        $params = ConvertTo-NarrationParameterSet -Parameters $Parameters -Properties $props -Backend $backend
        # tool NAME --params=<compact JSON>, plus any --plugin flags
        $cliArgs = @('tool', $Name, "--params=$(ConvertTo-NarrationJson $params)") + @(Get-NarrationPluginArgument -Plugin $Plugin -Backend $backend)
        # run it; the progress bar is named after the tool, errors name the tool
        $r = Invoke-NarrationCli -Arguments $cliArgs -Backend $backend -Activity "speechkit-narrate tool $Name" -Target $Name
        ConvertTo-NarrationToolResult -Result $r  # typed result with Tool, Files and the tool's other keys
    }
    catch { $PSCmdlet.ThrowTerminatingError($_) }  # rethrow as an Invoke-NarrationTool error
}

# Public command: regenerates the per-tool commands (Invoke-NarrationSpeak, Invoke-NarrationQuick, ...) from a fresh
# tool list and loads them as the dynamic module SpeechKitNarrate.Tools, imported globally. Runs automatically on import.
# -PassThru returns one SpeechKitNarrate.GeneratedCommand per command. Supports -WhatIf. Replaces any earlier generated
# commands.
function Update-NarrationCommand {
    <#
    .SYNOPSIS
    Regenerates the per-tool commands (Invoke-NarrationSpeak, Invoke-NarrationQuick, ...) from the registry.

    .DESCRIPTION
    Reads the standalone tools (python -m speechkit_narrate tools, which loads the plug-ins in $env:SPEECHKIT_PLUGINS) and
    generates one command per tool in the dynamic module SpeechKitNarrate.Tools: snake_case parameters become
    PascalCase, strings/integers/numbers/booleans/arrays/objects become string/int/double/switch/typed arrays/
    hashtable, enums become ValidateSet, required parameters are mandatory and path parameters are resolved
    to absolute paths. Runs automatically when the module is imported.

    .PARAMETER PassThru
    Return one object per generated command.

    .EXAMPLE
    $env:SPEECHKIT_PLUGINS = 'C:\plugins\reading_stats.py'
    Update-NarrationCommand -PassThru

    Adds Invoke-NarrationWordcount for a plug-in that registers a wordcount tool.

    .EXAMPLE
    Get-Command -Module SpeechKitNarrate.Tools

    The generated commands.

    .OUTPUTS
    SpeechKitNarrate.GeneratedCommand
    #>
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
    [OutputType('SpeechKitNarrate.GeneratedCommand')]  # type returned with -PassThru
    param([switch]$PassThru)  # -PassThru is the only parameter: it asks for one object per generated command
    try {  # failures become this command's terminating error
        $tools = @(Get-NarrationToolList -Refresh)  # ask Python for the current tools, including $env:SPEECHKIT_PLUGINS
        $gen = New-NarrationToolModuleText -Tools $tools  # generate the module source and the per-command details
        # -WhatIf reports how many commands would be generated and stops
        if (-not $PSCmdlet.ShouldProcess('SpeechKitNarrate.Tools', "Generate $(@($gen.Commands).Count) tool commands")) { return }
        Remove-NarrationToolModule  # unload the previous generation first
        # this SpeechKitNarrate module object, handed to the generated module so its commands call back into it
        $owner = $ExecutionContext.SessionState.Module
        # build the dynamic module from the generated source; -ArgumentList fills its param($NarrationOwner)
        $mod = New-Module -Name 'SpeechKitNarrate.Tools' -ScriptBlock ([scriptblock]::Create($gen.Text)) -ArgumentList $owner
        # -Global makes the commands visible in the user's session rather than only inside this module; -Force replaces
        # an older copy
        Import-Module -ModuleInfo $mod -Global -Force -DisableNameChecking
        $script:ToolsModule = $mod  # remember it for Remove-NarrationToolModule
        if ($PassThru) {  # describe the generated commands only with -PassThru; otherwise output nothing
            foreach ($c in $gen.Commands) {  # one object per generated command
                # describe the generated command
                New-NarrationObject -TypeName 'SpeechKitNarrate.GeneratedCommand' -Property ([ordered]@{
                        Name       = $c.Name  # the generated command's name, e.g. Invoke-NarrationSpeak
                        Tool       = $c.Tool  # the registry tool the command runs, e.g. speak
                        Source     = $c.Source  # where the tool came from
                        # the PowerShell parameter names the generated command accepts
                        Parameters = [string[]]@($c.Specs | ForEach-Object { $_.Name })
                    })
            }
        }
    }
    catch { $PSCmdlet.ThrowTerminatingError($_) }  # rethrow as this command's error
}
